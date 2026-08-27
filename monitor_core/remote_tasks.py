"""Persistent, authenticated task queue for hybrid local/server collection."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BEIJING = timezone(timedelta(hours=8))
ALLOWED_MODELS = ("doubao", "yuanbao", "wenxin", "deepseek", "quark")
FINAL_STATES = {"completed", "failed", "cancelled"}
_RESULT_LOCK = threading.Lock()


def now_text() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def secure_token_matches(provided: str, configured: str) -> bool:
    return bool(configured) and hmac.compare_digest(
        str(provided or "").encode("utf-8"), str(configured).encode("utf-8")
    )


def configured_token(name: str) -> str:
    return os.environ.get(name, "").strip()


class RemoteTaskQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS remote_tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    models_json TEXT NOT NULL,
                    questions_json TEXT NOT NULL,
                    rounds INTEGER NOT NULL,
                    question_mode TEXT NOT NULL,
                    total_steps INTEGER NOT NULL,
                    completed_steps INTEGER NOT NULL DEFAULT 0,
                    current_model TEXT NOT NULL DEFAULT '',
                    current_question TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires REAL NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS remote_tasks_status_created
                    ON remote_tasks(status, created_at);
                CREATE TABLE IF NOT EXISTS remote_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS remote_task_events_task
                    ON remote_task_events(task_id, id);
                CREATE TABLE IF NOT EXISTS remote_task_results (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row | None, *, include_private: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["models"] = json.loads(value.pop("models_json"))
        value["questions"] = json.loads(value.pop("questions_json"))
        value["cancel_requested"] = bool(value["cancel_requested"])
        if not include_private:
            value.pop("lease_token", None)
            value.pop("lease_expires", None)
        return value

    @staticmethod
    def validate_payload(payload: dict[str, Any]) -> tuple[list[str], list[str], int, str]:
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("models 必须是数组")
        models = list(dict.fromkeys(str(item or "").strip().lower() for item in raw_models))
        if not models or any(item not in ALLOWED_MODELS for item in models):
            raise ValueError("至少选择一个有效模型")
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, list):
            raise ValueError("questions 必须是数组")
        questions = []
        for item in raw_questions[:10]:
            text = " ".join(str(item or "").split()).strip()
            if text and text not in questions:
                questions.append(text[:500])
        if not questions:
            raise ValueError("至少填写一个有效问题")
        rounds = max(1, min(int(payload.get("rounds") or 1), 3))
        mode = str(payload.get("question_mode") or "interleaved").strip().lower()
        if mode not in {"interleaved", "sequential"}:
            raise ValueError("question_mode 无效")
        return models, questions, rounds, mode

    def _event(self, connection: sqlite3.Connection, task_id: str, message: str,
               level: str = "info") -> None:
        connection.execute(
            "INSERT INTO remote_task_events(task_id,created_at,level,message) VALUES(?,?,?,?)",
            (task_id, now_text(), level[:20], str(message or "")[:500]),
        )

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        models, questions, rounds, mode = self.validate_payload(payload)
        task_id = uuid.uuid4().hex
        created = now_text()
        total = len(models) * len(questions) * rounds
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO remote_tasks(
                    id,status,models_json,questions_json,rounds,question_mode,total_steps,
                    created_at,updated_at,message
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "queued", json.dumps(models), json.dumps(questions, ensure_ascii=False),
                 rounds, mode, total, created, created, "等待本机采集器领取"),
            )
            self._event(connection, task_id, "任务已创建，等待本机采集器领取")
        return self.get(task_id) or {}

    def get(self, task_id: str, *, events: bool = True) -> dict[str, Any] | None:
        with self._connection() as connection:
            task = self._decode(connection.execute(
                "SELECT * FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone())
            if task and events:
                task["events"] = [dict(row) for row in connection.execute(
                    "SELECT created_at,level,message FROM remote_task_events WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 60", (task_id,)
                ).fetchall()]
            return task

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_tasks ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] in FINAL_STATES:
                connection.commit()
                return self._decode(row)
            finished = now_text() if row["status"] == "queued" else ""
            status = "cancelled" if row["status"] == "queued" else row["status"]
            message = "任务已取消" if status == "cancelled" else "正在等待本机安全停止"
            connection.execute(
                "UPDATE remote_tasks SET status=?,cancel_requested=1,message=?,updated_at=?,finished_at=? WHERE id=?",
                (status, message, now_text(), finished, task_id),
            )
            self._event(connection, task_id, message, "warning")
            connection.commit()
        return self.get(task_id)

    def claim(self, worker_id: str, lease_seconds: int = 900) -> dict[str, Any] | None:
        worker_id = str(worker_id or "local-worker")[:100]
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE remote_tasks SET status='cancelled',message='任务已取消',"
                "lease_token='',lease_expires=0,updated_at=?,finished_at=? "
                "WHERE status='running' AND cancel_requested=1 AND lease_expires>0 AND lease_expires<?",
                (now_text(), now_text(), now),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='queued',worker_id='',lease_token='',lease_expires=0,"
                "message='本机连接中断，等待重新领取',updated_at=? "
                "WHERE status='running' AND cancel_requested=0 AND lease_expires>0 AND lease_expires<?",
                (now_text(), now),
            )
            row = connection.execute(
                "SELECT * FROM remote_tasks WHERE status='queued' AND cancel_requested=0 "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            token = secrets.token_urlsafe(32)
            connection.execute(
                "UPDATE remote_tasks SET status='running',worker_id=?,lease_token=?,lease_expires=?,"
                "message='本机已领取，正在准备',updated_at=? WHERE id=?",
                (worker_id, token, now + max(120, lease_seconds), now_text(), row["id"]),
            )
            self._event(connection, row["id"], f"本机采集器 {worker_id} 已领取任务")
            connection.commit()
            claimed = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (row["id"],)).fetchone()
        return self._decode(claimed, include_private=True)

    def _leased_row(self, connection: sqlite3.Connection, task_id: str,
                    lease_token: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("任务不存在")
        if not secure_token_matches(lease_token, row["lease_token"]):
            raise PermissionError("任务租约无效")
        return row

    def heartbeat(self, task_id: str, lease_token: str, payload: dict[str, Any],
                  lease_seconds: int = 900) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, task_id, lease_token)
            if row["status"] not in {"running"}:
                connection.rollback()
                raise ValueError("任务已经结束")
            model = str(payload.get("model") or row["current_model"] or "")[:40]
            question = str(payload.get("question") or row["current_question"] or "")[:500]
            message = str(payload.get("message") or row["message"] or "运行中")[:500]
            connection.execute(
                "UPDATE remote_tasks SET current_model=?,current_question=?,message=?,lease_expires=?,updated_at=? WHERE id=?",
                (model, question, message, time.time() + max(120, lease_seconds), now_text(), task_id),
            )
            if message and message != row["message"]:
                self._event(connection, task_id, message)
            connection.commit()
            cancel_requested = bool(row["cancel_requested"])
        return {"ok": True, "cancel_requested": cancel_requested}

    def accept_result(self, task_id: str, lease_token: str, model_id: str,
                      request_id: str, record: dict[str, Any], results_root: Path) -> dict[str, Any]:
        if model_id not in ALLOWED_MODELS:
            raise ValueError("未知模型")
        if not isinstance(record, dict):
            raise ValueError("record 必须是对象")
        request_id = str(request_id or "")[:160]
        if not request_id:
            raise ValueError("request_id 不能为空")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, task_id, lease_token)
            existing = connection.execute(
                "SELECT request_id FROM remote_task_results WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                connection.commit()
                return {"ok": True, "duplicate": True,
                        "completed_steps": int(row["completed_steps"])}
            record = dict(record)
            record.update({"remote_task_id": task_id, "remote_request_id": request_id})
            results_root.mkdir(parents=True, exist_ok=True)
            target = results_root / f"{model_id}_results.jsonl"
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with _RESULT_LOCK:
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            connection.execute(
                "INSERT INTO remote_task_results(request_id,task_id,model_id,created_at) VALUES(?,?,?,?)",
                (request_id, task_id, model_id, now_text()),
            )
            completed = min(int(row["total_steps"]), int(row["completed_steps"]) + 1)
            message = f"已完成 {completed}/{row['total_steps']} 轮"
            connection.execute(
                "UPDATE remote_tasks SET completed_steps=?,current_model=?,message=?,lease_expires=?,updated_at=? WHERE id=?",
                (completed, model_id, message, time.time() + 900, now_text(), task_id),
            )
            self._event(connection, task_id, message)
            connection.commit()
        return {"ok": True, "duplicate": False, "completed_steps": completed}

    def finish(self, task_id: str, lease_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("status") or "completed").lower()
        if requested not in {"completed", "failed", "cancelled"}:
            raise ValueError("结束状态无效")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, task_id, lease_token)
            if row["cancel_requested"]:
                requested = "cancelled"
            error = str(payload.get("error") or "")[:2000]
            messages = {
                "completed": "任务已完成",
                "failed": "任务执行失败",
                "cancelled": "任务已取消",
            }
            connection.execute(
                "UPDATE remote_tasks SET status=?,message=?,error=?,lease_token='',lease_expires=0,"
                "updated_at=?,finished_at=? WHERE id=?",
                (requested, messages[requested], error, now_text(), now_text(), task_id),
            )
            self._event(connection, task_id, messages[requested],
                        "error" if requested == "failed" else "info")
            connection.commit()
        return self.get(task_id) or {}


def task_page_html() -> str:
    """Small standalone page; the main analytics UI remains untouched."""
    return r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>采集任务中心</title>
<style>body{margin:0;background:#f4f7fb;color:#172033;font:14px system-ui,-apple-system,"Microsoft YaHei",sans-serif}.wrap{max-width:1050px;margin:28px auto;padding:0 18px}.card{background:#fff;border:1px solid #dde5f0;border-radius:14px;padding:20px;margin-bottom:18px;box-shadow:0 8px 28px #2030500d}h1{font-size:25px}h2{font-size:17px}.row{display:flex;gap:14px;flex-wrap:wrap;align-items:center}label{display:block;margin:8px 0}textarea,input,select,button{font:inherit;border:1px solid #cbd5e1;border-radius:8px;padding:9px 11px}textarea{width:100%;box-sizing:border-box;min-height:120px}input[type=password]{min-width:280px}button{background:#1769e0;color:white;border:0;cursor:pointer}button.secondary{background:#64748b}.task{padding:14px 0;border-top:1px solid #edf1f6}.muted{color:#68758b}.bar{height:8px;background:#e8edf5;border-radius:8px;overflow:hidden}.bar i{display:block;height:100%;background:#2d7bf0}.error{color:#b42318}.ok{color:#067647}.badge{padding:3px 8px;background:#eef4ff;border-radius:10px}</style></head><body><div class="wrap">
<h1>采集任务中心</h1><p><a href="/">返回数据面板</a></p>
<section class="card"><h2>创建本机采集任务</h2><label>任务访问密钥（仅保存在当前浏览器标签页）<br><input id="key" type="password" autocomplete="off"></label>
<label>模型</label><div class="row" id="models"></div><label>问题（每行一个，最多10个）</label><textarea id="questions"></textarea>
<div class="row"><label>循环次数 <select id="rounds"><option>1</option><option>2</option><option>3</option></select></label><label>顺序 <select id="mode"><option value="interleaved">按轮交错</option><option value="sequential">逐题完成</option></select></label><button id="create">提交任务</button><span id="notice"></span></div></section>
<section class="card"><div class="row"><h2 style="flex:1">最近任务</h2><button class="secondary" id="reload">刷新</button></div><div id="tasks" class="muted">输入访问密钥后加载</div></section></div>
<script>
const names={doubao:"豆包",yuanbao:"腾讯元宝",wenxin:"文心一言",deepseek:"DeepSeek",quark:"夸克"},$=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
$("models").innerHTML=Object.entries(names).map(([k,v])=>`<label><input type="checkbox" value="${k}" checked> ${v}</label>`).join("");
function headers(){return{"Content-Type":"application/json","X-Monitor-Task-Token":$("key").value}}
async function api(path,options={}){const r=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})},cache:"no-store"});const d=await r.json();if(!r.ok)throw Error(d.error||`HTTP ${r.status}`);return d}
function statusName(s){return({queued:"排队中",running:"执行中",completed:"已完成",failed:"失败",cancelled:"已取消"})[s]||s}
async function load(){if(!$("key").value)return;try{const d=await api('/api/tasks');$("tasks").innerHTML=d.tasks.length?d.tasks.map(t=>{const pct=t.total_steps?Math.round(t.completed_steps*100/t.total_steps):0;return `<div class="task"><div class="row"><b>${esc(t.id.slice(0,10))}</b><span class="badge">${statusName(t.status)}</span><span>${esc(t.models.map(x=>names[x]||x).join("、"))}</span><span class="muted">${esc(t.created_at)}</span></div><p>${esc(t.message)} ${t.current_question?" · "+esc(t.current_question):""}</p><div class="bar"><i style="width:${pct}%"></i></div><p class="muted">${t.completed_steps}/${t.total_steps} 轮${t.error?` · <span class="error">${esc(t.error)}</span>`:""} ${["queued","running"].includes(t.status)?`<button class="secondary" onclick="cancelTask('${t.id}')">取消</button>`:""}</p></div>`}).join(""):"暂无任务"}catch(e){$("tasks").innerHTML=`<span class="error">${esc(e.message)}</span>`}}
async function cancelTask(id){if(!confirm('确定取消该任务？'))return;try{await api(`/api/tasks/${id}/cancel`,{method:'POST',body:'{}'});load()}catch(e){alert(e.message)}}
$("key").oninput=()=>{sessionStorage.setItem('monitorTaskKey',$("key").value);load()};$("key").value=sessionStorage.getItem('monitorTaskKey')||"";
$("create").onclick=async()=>{const models=[...document.querySelectorAll('#models input:checked')].map(x=>x.value),questions=$("questions").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);try{const d=await api('/api/tasks',{method:'POST',body:JSON.stringify({models,questions,rounds:Number($("rounds").value),question_mode:$("mode").value})});$("notice").className='ok';$("notice").textContent=`已创建 ${d.task.id.slice(0,10)}`;load()}catch(e){$("notice").className='error';$("notice").textContent=e.message}};
$("reload").onclick=load;load();setInterval(load,5000);
</script></body></html>'''
