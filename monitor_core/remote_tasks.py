"""Persistent, authenticated task queue for hybrid local/server collection."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
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
DIAGNOSIS_MODELS = ("doubao", "yuanbao", "wenxin")
FINAL_STATES = {"completed", "failed", "cancelled"}
DELETABLE_STATES = FINAL_STATES | {"paused"}
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
                    pause_requested INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS remote_workers (
                    worker_id TEXT PRIMARY KEY,
                    last_seen_epoch REAL NOT NULL,
                    last_seen TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_task_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    readiness_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS remote_login_requests (
                    id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS remote_login_requests_status_created
                    ON remote_login_requests(status, created_at);
                """
            )
            task_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(remote_tasks)")
            }
            for name in ("customer_slug", "brand_name", "product_name", "task_kind"):
                if name not in task_columns:
                    connection.execute(
                        f"ALTER TABLE remote_tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            if "pause_requested" not in task_columns:
                connection.execute(
                    "ALTER TABLE remote_tasks ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0"
                )
            result_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(remote_task_results)")
            }
            if "record_json" not in result_columns:
                connection.execute(
                    "ALTER TABLE remote_task_results ADD COLUMN record_json TEXT NOT NULL DEFAULT ''"
                )
            worker_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(remote_workers)")
            }
            if "readiness_json" not in worker_columns:
                connection.execute(
                    "ALTER TABLE remote_workers ADD COLUMN readiness_json TEXT NOT NULL DEFAULT '{}'"
                )

    @staticmethod
    def _decode(row: sqlite3.Row | None, *, include_private: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["models"] = json.loads(value.pop("models_json"))
        value["questions"] = json.loads(value.pop("questions_json"))
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["pause_requested"] = bool(value.get("pause_requested"))
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
        rounds = max(1, min(int(payload.get("rounds") or 1), 20))
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
        customer_slug = str(payload.get("customer_slug") or "").strip().lower()
        if customer_slug and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", customer_slug):
            raise ValueError("客户路径只能包含小写字母、数字和连字符")
        brand_name = " ".join(str(payload.get("brand_name") or "").split())[:100]
        product_name = " ".join(str(payload.get("product_name") or "").split())[:120]
        task_kind = str(payload.get("task_kind") or "").strip().lower()[:30]
        task_id = uuid.uuid4().hex
        created = now_text()
        total = len(models) * len(questions) * rounds
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO remote_tasks(
                    id,status,models_json,questions_json,rounds,question_mode,total_steps,
                    created_at,updated_at,message,customer_slug,brand_name,product_name,task_kind
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, "queued", json.dumps(models), json.dumps(questions, ensure_ascii=False),
                 rounds, mode, total, created, created, "等待本机采集器领取",
                 customer_slug, brand_name, product_name, task_kind),
            )
            self._event(connection, task_id, "任务已创建，等待本机采集器领取")
        return self.get(task_id) or {}

    def create_diagnosis(self, customer_slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        customer_slug = str(customer_slug or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", customer_slug):
            raise ValueError("客户路径只能包含小写字母、数字和连字符")
        brand_name = " ".join(str(payload.get("brand_name") or "").split()).strip()
        product_name = " ".join(str(payload.get("product_name") or "").split()).strip()
        question = " ".join(str(payload.get("question") or "").split()).strip()
        if not brand_name:
            raise ValueError("请填写品牌名称")
        if not question:
            raise ValueError("请填写诊断问题")
        with self._connection() as connection:
            active = connection.execute(
                "SELECT id FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                "AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
                (customer_slug,),
            ).fetchone()
        if active:
            existing = self.get(str(active["id"]))
            if existing:
                return existing
        return self.create({
            "models": list(DIAGNOSIS_MODELS),
            "questions": [question[:500]],
            "rounds": 8,
            "question_mode": "sequential",
            "customer_slug": customer_slug,
            "brand_name": brand_name[:100],
            "product_name": product_name[:120],
            "task_kind": "diagnosis",
        })

    def create_public_diagnosis(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if not " ".join(str(payload.get("product_name") or "").split()).strip():
            raise ValueError("请填写需要诊断的具体产品")
        for _attempt in range(5):
            report_key = secrets.token_hex(16)
            with self._connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM remote_tasks WHERE customer_slug=? LIMIT 1",
                    (report_key,),
                ).fetchone()
            if exists is None:
                return report_key, self.create_diagnosis(report_key, payload)
        raise RuntimeError("无法生成唯一报告密钥，请稍后重试")

    def get(self, task_id: str, *, events: bool = True) -> dict[str, Any] | None:
        with self._connection() as connection:
            task = self._decode(connection.execute(
                "SELECT * FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone())
            if task:
                progress = self._model_progress(connection, task)
                task["model_progress"] = progress
                task["completed_rounds"] = {
                    model: list(value["completed_rounds"])
                    for model, value in progress.items()
                }
            if task and events:
                task["events"] = [dict(row) for row in connection.execute(
                    "SELECT created_at,level,message FROM remote_task_events WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 60", (task_id,)
                ).fetchall()]
            return task

    @staticmethod
    def _decoded_result_records(connection: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT request_id,model_id,created_at,record_json FROM remote_task_results "
            "WHERE task_id=? ORDER BY created_at,request_id",
            (task_id,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                record = json.loads(row["record_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                record = {}
            if not isinstance(record, dict):
                record = {}
            record.setdefault("collector_model", row["model_id"])
            record["request_id"] = row["request_id"]
            record["created_at"] = row["created_at"]
            output.append(record)
        return output

    @classmethod
    def _model_progress(cls, connection: sqlite3.Connection,
                        task: dict[str, Any]) -> dict[str, dict[str, Any]]:
        target = max(1, len(task.get("questions") or []) * int(task.get("rounds") or 1))
        progress = {
            model: {"completed": 0, "total": target, "completed_rounds": []}
            for model in task.get("models") or []
        }
        for record in cls._decoded_result_records(connection, str(task.get("id") or "")):
            model = str(record.get("collector_model") or record.get("model_id") or "")
            if model not in progress:
                continue
            try:
                round_number = int(record.get("round") or 0)
            except (TypeError, ValueError):
                round_number = 0
            if round_number > 0 and round_number not in progress[model]["completed_rounds"]:
                progress[model]["completed_rounds"].append(round_number)
        for value in progress.values():
            value["completed_rounds"].sort()
            value["completed"] = len(value["completed_rounds"])
        return progress

    def results(self, task_id: str) -> list[dict[str, Any]] | None:
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if exists is None:
                return None
            records = self._decoded_result_records(connection, task_id)
        output = []
        for record in records:
            body = str(record.get("web_body") or record.get("reply") or "")
            sources = record.get("sources") if isinstance(record.get("sources"), list) else []
            output.append({
                "request_id": str(record.get("request_id") or ""),
                "model_id": str(record.get("collector_model") or record.get("model_id") or ""),
                "round": int(record.get("round") or 0),
                "question": str(record.get("question") or record.get("prompt") or ""),
                "body": body,
                "body_length": len(body),
                "sources": sources,
                "body_capture_complete": bool(record.get("body_capture_complete")),
                "body_capture_origin": str(record.get("body_capture_origin") or ""),
                "expected_source_count": int(record.get("expected_source_count") or len(sources)),
                "source_capture_complete": bool(record.get("source_capture_complete")),
                "source_capture_origins": record.get("source_capture_origins") or {},
                "analysis": record.get("analysis") if isinstance(record.get("analysis"), dict) else {},
                "recommended": bool(record.get("recommended")),
                "rank": record.get("rank"),
                "page_url": str(record.get("page_url") or ""),
                "created_at": str(record.get("created_at") or record.get("finished_at") or ""),
            })
        return sorted(output, key=lambda item: (item["model_id"], item["round"], item["created_at"]))

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_tasks ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def list_customer(self, customer_slug: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_tasks WHERE customer_slug=? AND task_kind='diagnosis' "
                "ORDER BY created_at DESC LIMIT ?",
                (customer_slug, max(1, min(int(limit), 50))),
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    @staticmethod
    def _target_occurs(text: str, *targets: str) -> bool:
        compact = "".join(str(text or "").casefold().split())
        return any(
            "".join(str(target or "").casefold().split()) in compact
            for target in targets if str(target or "").strip()
        )

    @staticmethod
    def _public_message(value: Any) -> str:
        text = str(value or "")
        if "COOKIES_JSON" in text or "隐身会话需要临时设置" in text:
            return "模型隐身登录未就绪，请联系管理员完成登录"
        return re.sub(
            r"MONITOR_[A-Z0-9_]+_COOKIES_JSON", "隐身网页登录凭证", text
        )

    def _public_task(self, task: dict[str, Any]) -> dict[str, Any]:
        value = dict(task)
        value["message"] = self._public_message(value.get("message"))
        value["error"] = self._public_message(value.get("error"))
        value["events"] = [
            {**event, "message": self._public_message(event.get("message"))}
            for event in value.get("events") or []
        ]
        return value

    def diagnosis_readiness(self) -> dict[str, Any]:
        workers = self.list_workers()
        online = [worker for worker in workers if worker.get("online")]
        selected = next(
            (
                worker for worker in online
                if all(
                    bool((worker.get("readiness") or {}).get(model, {}).get("ready"))
                    for model in DIAGNOSIS_MODELS
                )
            ),
            online[0] if online else None,
        )
        model_states: dict[str, dict[str, Any]] = {}
        for model in DIAGNOSIS_MODELS:
            raw = ((selected or {}).get("readiness") or {}).get(model, {})
            ready = bool(raw.get("ready")) if isinstance(raw, dict) else False
            model_states[model] = {
                "ready": ready,
                "message": "隐身登录已就绪" if ready else "隐身登录未就绪",
            }
        all_ready = bool(selected) and all(item["ready"] for item in model_states.values())
        if not selected:
            message = "本机采集器当前离线，请联系管理员启动采集服务"
        elif not all_ready:
            missing = "、".join(
                model for model, state in model_states.items() if not state["ready"]
            )
            display = {"doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言"}
            message = "、".join(display[item] for item in missing.split("、")) + "隐身登录尚未就绪，请联系管理员"
        else:
            message = "三模型采集环境已就绪"
        return {
            "ready": all_ready,
            "online": bool(selected),
            "worker_id": str((selected or {}).get("worker_id") or ""),
            "models": model_states,
            "message": message,
        }

    def diagnosis_report(self, customer_slug: str) -> dict[str, Any]:
        tasks = self.list_customer(customer_slug, 20)
        readiness = self.diagnosis_readiness()
        if not tasks:
            return {"customer_slug": customer_slug, "task": None, "history": [], "report": None,
                    "readiness": readiness}
        task = self.get(str(tasks[0]["id"])) or tasks[0]
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT model_id,created_at,record_json FROM remote_task_results "
                "WHERE task_id=? ORDER BY created_at,request_id",
                (task["id"],),
            ).fetchall()
        records = []
        for row in rows:
            try:
                record = json.loads(row["record_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                record.setdefault("collector_model", row["model_id"])
                records.append(record)
        brand = str(task.get("brand_name") or "")
        product = str(task.get("product_name") or "")
        per_model: list[dict[str, Any]] = []
        all_sources: dict[str, dict[str, Any]] = {}
        answers: list[dict[str, Any]] = []
        recommended_total = 0
        for model in task.get("models") or DIAGNOSIS_MODELS:
            model_records = [
                record for record in records
                if str(record.get("collector_model") or record.get("model_id") or "") == model
            ]
            recommended = 0
            ranks: list[int] = []
            source_count = 0
            expected_source_total = 0
            body_complete_rounds = 0
            source_complete_rounds = 0
            analysis_complete_rounds = 0
            total_body_chars = 0
            for record in model_records:
                body = str(record.get("web_body") or record.get("reply") or "")
                total_body_chars += len(body)
                local_analysis = (
                    record.get("analysis") if isinstance(record.get("analysis"), dict) else {}
                )
                products = record.get("products") if isinstance(record.get("products"), list) else []
                structured_hit = False
                rank = None
                for index, item in enumerate(products, 1):
                    item_text = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
                    if self._target_occurs(item_text, brand, product):
                        structured_hit = True
                        rank_value = item.get("rank") if isinstance(item, dict) else None
                        try:
                            rank = int(rank_value) if rank_value else index
                        except (TypeError, ValueError):
                            rank = index
                        break
                locally_analyzed = (
                    local_analysis.get("mode") == "local_chrome_extension"
                    and isinstance(local_analysis.get("recommended"), bool)
                )
                if locally_analyzed:
                    analysis_complete_rounds += 1
                    mentioned = bool(local_analysis["recommended"])
                    try:
                        local_rank = int(local_analysis.get("rank") or 0)
                        rank = local_rank if local_rank > 0 else None
                    except (TypeError, ValueError):
                        rank = None
                else:
                    # Backward compatibility for reports collected before local analysis.
                    mentioned = structured_hit or self._target_occurs(body, brand, product)
                if mentioned:
                    recommended += 1
                    recommended_total += 1
                    if rank:
                        ranks.append(rank)
                sources = [item for item in record.get("sources") or [] if isinstance(item, dict)]
                source_count += len(sources)
                try:
                    expected_source_count = max(len(sources), int(record.get("expected_source_count") or 0))
                except (TypeError, ValueError):
                    expected_source_count = len(sources)
                expected_source_total += expected_source_count
                body_capture_complete = bool(record.get("body_capture_complete", bool(body))) and bool(body)
                source_capture_complete = bool(
                    record.get("source_capture_complete", len(sources) >= expected_source_count)
                ) and len(sources) >= expected_source_count
                if body_capture_complete:
                    body_complete_rounds += 1
                if source_capture_complete:
                    source_complete_rounds += 1
                for source in sources:
                    url = str(source.get("url") or source.get("href") or "").strip()
                    if url:
                        all_sources.setdefault(url, {
                            "url": url,
                            "title": str(source.get("title") or url),
                            "models": [],
                        })
                        if model not in all_sources[url]["models"]:
                            all_sources[url]["models"].append(model)
                answers.append({
                    "model": model,
                    "round": int(record.get("round") or len(answers) + 1),
                    "question": str(record.get("question") or ""),
                    "answer": body,
                    "recommended": mentioned,
                    "rank": rank,
                    "sources": sources,
                    "body_length": len(body),
                    "body_capture_complete": body_capture_complete,
                    "body_capture_origin": str(record.get("body_capture_origin") or "page_dom"),
                    "expected_source_count": expected_source_count,
                    "source_capture_complete": source_capture_complete,
                    "source_capture_origins": (
                        record.get("source_capture_origins")
                        if isinstance(record.get("source_capture_origins"), dict)
                        else {"dom": len(sources), "network": 0}
                    ),
                    "capture_mode": str(record.get("capture_mode") or ""),
                    "analysis": {
                        "complete": locally_analyzed,
                        "mode": str(local_analysis.get("mode") or ""),
                        "recommended": mentioned,
                        "rank": rank,
                        "matched_terms": [str(item) for item in local_analysis.get("matched_terms") or []],
                        "analyzed_at": str(local_analysis.get("analyzed_at") or ""),
                    },
                    "finished_at": str(record.get("finished_at") or ""),
                })
            completed = len(model_records)
            rate = round(recommended * 100 / completed, 1) if completed else 0
            per_model.append({
                "id": model,
                "completed": completed,
                "target_rounds": int(task.get("rounds") or 8),
                "recommended_rounds": recommended,
                "recommendation_rate": rate,
                "average_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
                "source_count": source_count,
                "expected_source_count": expected_source_total,
                "body_complete_rounds": body_complete_rounds,
                "source_complete_rounds": source_complete_rounds,
                "analysis_complete_rounds": analysis_complete_rounds,
                "total_body_chars": total_body_chars,
            })
        completed_total = len(records)
        overall_rate = round(recommended_total * 100 / completed_total, 1) if completed_total else 0
        if completed_total == 0:
            conclusion = "等待采集数据"
        elif overall_rate >= 60:
            conclusion = "推荐表现强"
        elif overall_rate >= 25:
            conclusion = "已有一定推荐，但仍有提升空间"
        else:
            conclusion = "推荐可见度偏低"
        public_task = self._public_task(task)
        public_task["model_progress"] = {
            model: {
                "completed": sum(
                    1 for record in records
                    if str(record.get("collector_model") or record.get("model_id") or "") == model
                ),
                "total": int(task.get("rounds") or 8),
            }
            for model in task.get("models") or DIAGNOSIS_MODELS
        }
        return {
            "customer_slug": customer_slug,
            "task": public_task,
            "history": [self._public_task(item) for item in tasks],
            "readiness": readiness,
            "report": {
                "brand_name": brand,
                "product_name": product,
                "question": (task.get("questions") or [""])[0],
                "overall_rate": overall_rate,
                "recommended_rounds": recommended_total,
                "completed_rounds": completed_total,
                "target_rounds": int(task.get("total_steps") or 24),
                "conclusion": conclusion,
                "models": per_model,
                "sources": sorted(all_sources.values(), key=lambda item: (-len(item["models"]), item["title"])),
                "answers": sorted(answers, key=lambda item: (item["model"], item["round"])),
                "quality": {
                    "body_complete_rounds": sum(1 for item in answers if item["body_capture_complete"]),
                    "source_complete_rounds": sum(1 for item in answers if item["source_capture_complete"]),
                    "analysis_complete_rounds": sum(1 for item in answers if item["analysis"]["complete"]),
                    "total_body_chars": sum(int(item["body_length"]) for item in answers),
                    "captured_sources": sum(len(item["sources"]) for item in answers),
                    "expected_sources": sum(int(item["expected_source_count"]) for item in answers),
                },
            },
        }

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
            if row["status"] == "running":
                message = "正在停止，当前采集动作结束后退出"
                connection.execute(
                    "UPDATE remote_tasks SET cancel_requested=1,pause_requested=0,message=?,"
                    "updated_at=? WHERE id=?",
                    (message, now_text(), task_id),
                )
            else:
                message = "任务已取消"
                connection.execute(
                    "UPDATE remote_tasks SET status='cancelled',cancel_requested=1,pause_requested=0,"
                    "message=?,lease_token='',lease_expires=0,updated_at=?,finished_at=? WHERE id=?",
                    (message, now_text(), now_text(), task_id),
                )
            self._event(connection, task_id, message, "warning")
            connection.commit()
        return self.get(task_id)

    def pause(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] == "paused":
                connection.commit()
                return self.get(task_id)
            if row["status"] in FINAL_STATES:
                connection.rollback()
                raise ValueError("已结束任务不能暂停，可选择重跑")
            if row["status"] == "running":
                message = "正在暂停，当前轮结束后保存断点"
                connection.execute(
                    "UPDATE remote_tasks SET pause_requested=1,message=?,updated_at=? WHERE id=?",
                    (message, now_text(), task_id),
                )
            else:
                message = "任务已暂停"
                connection.execute(
                    "UPDATE remote_tasks SET status='paused',pause_requested=0,message=?,"
                    "worker_id='',lease_token='',lease_expires=0,updated_at=? WHERE id=?",
                    (message, now_text(), task_id),
                )
            self._event(connection, task_id, message, "warning")
            connection.commit()
        return self.get(task_id)

    def resume(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] != "paused":
                connection.rollback()
                raise ValueError("只有已暂停任务可以继续执行")
            message = "任务已恢复，等待本机采集器从断点继续"
            connection.execute(
                "UPDATE remote_tasks SET status='queued',pause_requested=0,cancel_requested=0,"
                "worker_id='',lease_token='',lease_expires=0,message=?,error='',finished_at='',"
                "updated_at=? WHERE id=?",
                (message, now_text(), task_id),
            )
            self._event(connection, task_id, message)
            connection.commit()
        return self.get(task_id)

    def rerun(self, task_id: str) -> dict[str, Any] | None:
        source = self.get(task_id)
        if source is None:
            return None
        if source.get("status") not in DELETABLE_STATES:
            raise ValueError("请先停止当前任务，再执行重跑")
        return self.create({
            "models": list(source.get("models") or []),
            "questions": list(source.get("questions") or []),
            "rounds": int(source.get("rounds") or 1),
            "question_mode": str(source.get("question_mode") or "interleaved"),
            "customer_slug": str(source.get("customer_slug") or ""),
            "brand_name": str(source.get("brand_name") or ""),
            "product_name": str(source.get("product_name") or ""),
            "task_kind": str(source.get("task_kind") or ""),
        })

    @staticmethod
    def _purge_task_result_files(task_id: str, results_root: Path) -> None:
        if not results_root.exists():
            return
        with _RESULT_LOCK:
            for target in results_root.glob("*_results.jsonl"):
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                kept = 0
                try:
                    with target.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
                        "w", encoding="utf-8"
                    ) as destination:
                        for line in source:
                            try:
                                record = json.loads(line)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                destination.write(line)
                                kept += 1
                                continue
                            if str(record.get("remote_task_id") or "") == task_id:
                                continue
                            destination.write(line)
                            kept += 1
                    if kept:
                        os.replace(temporary, target)
                    else:
                        temporary.unlink(missing_ok=True)
                        target.unlink(missing_ok=True)
                finally:
                    temporary.unlink(missing_ok=True)

    @staticmethod
    def _purge_single_result_file(request_id: str, results_root: Path) -> None:
        if not results_root.exists():
            return
        with _RESULT_LOCK:
            for target in results_root.glob("*_results.jsonl"):
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                kept = 0
                try:
                    with target.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
                        "w", encoding="utf-8"
                    ) as destination:
                        for line in source:
                            try:
                                record = json.loads(line)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                destination.write(line)
                                kept += 1
                                continue
                            if str(record.get("remote_request_id") or "") == request_id:
                                continue
                            destination.write(line)
                            kept += 1
                    if kept:
                        os.replace(temporary, target)
                    else:
                        temporary.unlink(missing_ok=True)
                        target.unlink(missing_ok=True)
                finally:
                    temporary.unlink(missing_ok=True)

    def delete(self, task_id: str, results_root: Path) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] not in DELETABLE_STATES:
                connection.rollback()
                raise ValueError("运行中的任务不能删除，请先暂停或停止任务")
            task = self._decode(row) or {"id": task_id}
            connection.execute("DELETE FROM remote_task_results WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM remote_task_events WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM remote_tasks WHERE id=?", (task_id,))
            connection.commit()
        self._purge_task_result_files(task_id, results_root)
        return task

    def clear_results(self, task_id: str, results_root: Path) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["status"] not in DELETABLE_STATES:
                connection.rollback()
                raise ValueError("请先暂停或停止任务，再清空结果")
            connection.execute("DELETE FROM remote_task_results WHERE task_id=?", (task_id,))
            connection.execute(
                "UPDATE remote_tasks SET status='paused',completed_steps=0,current_model='',"
                "current_question='',message='任务数据已清空，可从头继续',error='',"
                "cancel_requested=0,pause_requested=0,worker_id='',lease_token='',lease_expires=0,"
                "updated_at=?,finished_at='' WHERE id=?",
                (now_text(), task_id),
            )
            self._event(connection, task_id, "任务结果数据已清空", "warning")
            connection.commit()
        self._purge_task_result_files(task_id, results_root)
        return self.get(task_id)

    def delete_result(self, task_id: str, request_id: str,
                      results_root: Path) -> dict[str, Any] | None:
        request_id = str(request_id or "")[:160]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_row = connection.execute(
                "SELECT * FROM remote_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task_row is None:
                connection.rollback()
                return None
            if task_row["status"] not in DELETABLE_STATES:
                connection.rollback()
                raise ValueError("请先暂停或停止任务，再删除单轮数据")
            result_row = connection.execute(
                "SELECT model_id,record_json FROM remote_task_results "
                "WHERE task_id=? AND request_id=?",
                (task_id, request_id),
            ).fetchone()
            if result_row is None:
                connection.rollback()
                raise KeyError("该轮结果不存在")
            try:
                record = json.loads(result_row["record_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                record = {}
            connection.execute(
                "DELETE FROM remote_task_results WHERE task_id=? AND request_id=?",
                (task_id, request_id),
            )
            completed = int(connection.execute(
                "SELECT COUNT(*) AS count FROM remote_task_results WHERE task_id=?",
                (task_id,),
            ).fetchone()["count"])
            model = str(record.get("collector_model") or result_row["model_id"] or "")
            round_number = int(record.get("round") or 0)
            message = f"已删除 {model} 第 {round_number} 轮数据，任务已暂停"
            connection.execute(
                "UPDATE remote_tasks SET status='paused',completed_steps=?,message=?,error='',"
                "cancel_requested=0,pause_requested=0,worker_id='',lease_token='',lease_expires=0,"
                "updated_at=?,finished_at='' WHERE id=?",
                (completed, message, now_text(), task_id),
            )
            self._event(connection, task_id, message, "warning")
            connection.commit()
        self._purge_single_result_file(request_id, results_root)
        return self.get(task_id)

    def clear_finished(self, results_root: Path) -> int:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM remote_tasks WHERE status IN ('completed','failed','cancelled','paused')"
            ).fetchall()
        deleted = 0
        for row in rows:
            if self.delete(str(row["id"]), results_root) is not None:
                deleted += 1
        return deleted

    def claim(self, worker_id: str, readiness: dict[str, Any] | None = None,
              lease_seconds: int = 900) -> dict[str, Any] | None:
        worker_id = str(worker_id or "local-worker")[:100]
        readiness = readiness if isinstance(readiness, dict) else {}
        safe_readiness = {}
        for model in DIAGNOSIS_MODELS:
            raw = readiness.get(model) if isinstance(readiness.get(model), dict) else {}
            safe_readiness[model] = {
                "ready": bool(raw.get("ready")),
                "message": str(raw.get("message") or "")[:160],
            }
        readiness_json = json.dumps(safe_readiness, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO remote_workers(worker_id,last_seen_epoch,last_seen,status,current_task_id,message,readiness_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
                "last_seen_epoch=excluded.last_seen_epoch,last_seen=excluded.last_seen,status='idle',"
                "current_task_id='',message='等待任务',readiness_json=excluded.readiness_json",
                (worker_id, now, now_text(), "idle", "", "等待任务", readiness_json),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='cancelled',message='任务已取消',"
                "lease_token='',lease_expires=0,updated_at=?,finished_at=? "
                "WHERE status='running' AND cancel_requested=1 AND lease_expires>0 AND lease_expires<?",
                (now_text(), now_text(), now),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='paused',message='任务已暂停',pause_requested=0,"
                "worker_id='',lease_token='',lease_expires=0,updated_at=? "
                "WHERE status='running' AND pause_requested=1 AND lease_expires>0 AND lease_expires<?",
                (now_text(), now),
            )
            connection.execute(
                "UPDATE remote_tasks SET status='queued',worker_id='',lease_token='',lease_expires=0,"
                "message='本机连接中断，等待重新领取',updated_at=? "
                "WHERE status='running' AND cancel_requested=0 AND pause_requested=0 "
                "AND lease_expires>0 AND lease_expires<?",
                (now_text(), now),
            )
            active = connection.execute(
                "SELECT * FROM remote_tasks WHERE status='running' AND lease_expires>? "
                "ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if active is not None:
                if str(active["worker_id"] or "") == worker_id:
                    if active["cancel_requested"]:
                        connection.execute(
                            "UPDATE remote_tasks SET status='cancelled',message='任务已取消',"
                            "worker_id='',lease_token='',lease_expires=0,updated_at=?,finished_at=? "
                            "WHERE id=?",
                            (now_text(), now_text(), active["id"]),
                        )
                        self._event(connection, active["id"], "任务已取消")
                        connection.commit()
                        return None
                    if active["pause_requested"]:
                        connection.execute(
                            "UPDATE remote_tasks SET status='paused',message='任务已暂停',pause_requested=0,"
                            "worker_id='',lease_token='',lease_expires=0,updated_at=? WHERE id=?",
                            (now_text(), active["id"]),
                        )
                        self._event(connection, active["id"], "任务已暂停", "warning")
                        connection.commit()
                        return None
                    token = secrets.token_urlsafe(32)
                    message = "本机采集器已恢复，正在从断点继续"
                    connection.execute(
                        "UPDATE remote_tasks SET lease_token=?,lease_expires=?,message=?,updated_at=? "
                        "WHERE id=?",
                        (token, now + max(120, lease_seconds), message, now_text(), active["id"]),
                    )
                    connection.execute(
                        "UPDATE remote_workers SET status='busy',current_task_id=?,message=?,"
                        "last_seen_epoch=?,last_seen=? WHERE worker_id=?",
                        (active["id"], message, now, now_text(), worker_id),
                    )
                    self._event(connection, active["id"], message)
                    connection.commit()
                    claimed = connection.execute(
                        "SELECT * FROM remote_tasks WHERE id=?", (active["id"],)
                    ).fetchone()
                    task = self._decode(claimed, include_private=True) or {}
                    progress = self._model_progress(connection, task)
                    task["model_progress"] = progress
                    task["completed_rounds"] = {
                        model: list(value["completed_rounds"])
                        for model, value in progress.items()
                    }
                    return task
                connection.commit()
                return None
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
            connection.execute(
                "UPDATE remote_workers SET status='busy',current_task_id=?,message='正在执行任务',"
                "last_seen_epoch=?,last_seen=? WHERE worker_id=?",
                (row["id"], now, now_text(), worker_id),
            )
            self._event(connection, row["id"], f"本机采集器 {worker_id} 已领取任务")
            connection.commit()
            claimed = connection.execute("SELECT * FROM remote_tasks WHERE id=?", (row["id"],)).fetchone()
        task = self._decode(claimed, include_private=True) or {}
        with self._connection() as connection:
            progress = self._model_progress(connection, task)
        task["model_progress"] = progress
        task["completed_rounds"] = {
            model: list(value["completed_rounds"])
            for model, value in progress.items()
        }
        return task

    def list_workers(self) -> list[dict[str, Any]]:
        moment = time.time()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_workers ORDER BY last_seen_epoch DESC LIMIT 50"
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["online"] = moment - float(item.pop("last_seen_epoch") or 0) <= 20
            try:
                item["readiness"] = json.loads(item.pop("readiness_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["readiness"] = {}
            output.append(item)
        return output

    @staticmethod
    def _decode_login(row: sqlite3.Row | None, *, include_private: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        if not include_private:
            value.pop("lease_token", None)
            value.pop("lease_expires", None)
        return value

    def request_login(self, model_id: str) -> dict[str, Any]:
        model_id = str(model_id or "").strip().lower()
        if model_id not in DIAGNOSIS_MODELS:
            raise ValueError("只支持豆包、腾讯元宝和文心一言登录")
        with self._connection() as connection:
            active = connection.execute(
                "SELECT * FROM remote_login_requests WHERE model_id=? "
                "AND status IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
                (model_id,),
            ).fetchone()
            if active is not None:
                return self._decode_login(active) or {}
            login_id = uuid.uuid4().hex
            created = now_text()
            connection.execute(
                "INSERT INTO remote_login_requests(id,model_id,status,message,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (login_id, model_id, "queued", "等待本机打开隐身登录窗口", created, created),
            )
            row = connection.execute(
                "SELECT * FROM remote_login_requests WHERE id=?", (login_id,)
            ).fetchone()
        return self._decode_login(row) or {}

    def list_login_requests(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_login_requests ORDER BY created_at DESC LIMIT 60"
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = self._decode_login(row) or {}
            latest.setdefault(str(value.get("model_id") or ""), value)
        return [latest[model] for model in DIAGNOSIS_MODELS if model in latest]

    def claim_login(self, worker_id: str, readiness: dict[str, Any] | None = None,
                    lease_seconds: int = 900) -> dict[str, Any] | None:
        worker_id = str(worker_id or "local-worker")[:100]
        readiness = readiness if isinstance(readiness, dict) else {}
        safe_readiness = {}
        for model in DIAGNOSIS_MODELS:
            raw = readiness.get(model) if isinstance(readiness.get(model), dict) else {}
            safe_readiness[model] = {
                "ready": bool(raw.get("ready")),
                "message": str(raw.get("message") or "")[:160],
            }
        readiness_json = json.dumps(safe_readiness, ensure_ascii=False, separators=(",", ":"))
        moment = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO remote_workers(worker_id,last_seen_epoch,last_seen,status,current_task_id,message,readiness_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET "
                "last_seen_epoch=excluded.last_seen_epoch,last_seen=excluded.last_seen,status='idle',"
                "current_task_id='',message='等待任务',readiness_json=excluded.readiness_json",
                (worker_id, moment, now_text(), "idle", "", "等待任务", readiness_json),
            )
            connection.execute(
                "UPDATE remote_login_requests SET status='queued',worker_id='',lease_token='',"
                "lease_expires=0,message='本机连接中断，等待重新打开登录窗口',updated_at=? "
                "WHERE status='running' AND lease_expires>0 AND lease_expires<?",
                (now_text(), moment),
            )
            row = connection.execute(
                "SELECT * FROM remote_login_requests WHERE status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            token = secrets.token_urlsafe(32)
            message = "本机正在打开隐身登录窗口"
            connection.execute(
                "UPDATE remote_login_requests SET status='running',worker_id=?,lease_token=?,"
                "lease_expires=?,message=?,updated_at=? WHERE id=?",
                (worker_id, token, moment + max(120, lease_seconds), message, now_text(), row["id"]),
            )
            connection.execute(
                "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='login',"
                "current_task_id=?,message=? WHERE worker_id=?",
                (moment, now_text(), row["id"], message, worker_id),
            )
            connection.commit()
            claimed = connection.execute(
                "SELECT * FROM remote_login_requests WHERE id=?", (row["id"],)
            ).fetchone()
        return self._decode_login(claimed, include_private=True)

    def _leased_login_row(self, connection: sqlite3.Connection, login_id: str,
                          lease_token: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_login_requests WHERE id=?", (login_id,)
        ).fetchone()
        if row is None:
            raise KeyError("登录请求不存在")
        if not secure_token_matches(lease_token, row["lease_token"]):
            raise PermissionError("登录请求租约无效")
        return row

    def login_heartbeat(self, login_id: str, lease_token: str,
                        payload: dict[str, Any], lease_seconds: int = 900) -> dict[str, Any]:
        message = str(payload.get("message") or "等待你在本机隐身窗口完成登录")[:500]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_login_row(connection, login_id, lease_token)
            if row["status"] != "running":
                connection.rollback()
                raise ValueError("登录请求已经结束")
            moment = time.time()
            connection.execute(
                "UPDATE remote_login_requests SET message=?,lease_expires=?,updated_at=? WHERE id=?",
                (message, moment + max(120, lease_seconds), now_text(), login_id),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='login',"
                    "current_task_id=?,message=? WHERE worker_id=?",
                    (moment, now_text(), login_id, message, row["worker_id"]),
                )
            connection.commit()
        return {"ok": True}

    def finish_login(self, login_id: str, lease_token: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("status") or "ready").lower()
        if requested not in {"ready", "failed"}:
            raise ValueError("登录结束状态无效")
        error = str(payload.get("error") or "")[:1000]
        message = "隐身登录已就绪" if requested == "ready" else "隐身登录失败"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_login_row(connection, login_id, lease_token)
            connection.execute(
                "UPDATE remote_login_requests SET status=?,message=?,error=?,lease_token='',"
                "lease_expires=0,updated_at=?,finished_at=? WHERE id=?",
                (requested, message, error, now_text(), now_text(), login_id),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='idle',"
                    "current_task_id='',message='等待任务' WHERE worker_id=?",
                    (time.time(), now_text(), row["worker_id"]),
                )
            connection.commit()
            finished = connection.execute(
                "SELECT * FROM remote_login_requests WHERE id=?", (login_id,)
            ).fetchone()
        return self._decode_login(finished) or {}

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
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='busy',"
                    "current_task_id=?,message=? WHERE worker_id=?",
                    (time.time(), now_text(), task_id, message, row["worker_id"]),
                )
            if message and message != row["message"]:
                self._event(connection, task_id, message)
            connection.commit()
            cancel_requested = bool(row["cancel_requested"])
            pause_requested = bool(row["pause_requested"])
        return {
            "ok": True,
            "cancel_requested": cancel_requested,
            "pause_requested": pause_requested,
        }

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
            if row["cancel_requested"]:
                connection.commit()
                return {
                    "ok": True,
                    "stored": False,
                    "completed_steps": int(row["completed_steps"]),
                    "cancel_requested": True,
                    "pause_requested": bool(row["pause_requested"]),
                }
            existing = connection.execute(
                "SELECT request_id FROM remote_task_results WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing:
                connection.commit()
                return {
                    "ok": True,
                    "duplicate": True,
                    "completed_steps": int(row["completed_steps"]),
                    "cancel_requested": bool(row["cancel_requested"]),
                    "pause_requested": bool(row["pause_requested"]),
                }
            record = dict(record)
            record.update({"remote_task_id": task_id, "remote_request_id": request_id})
            # Diagnosis responses are already stored once in the report database.
            # Do not create a second server-side JSONL copy of customer report data.
            if str(row["task_kind"] or "") != "diagnosis":
                results_root.mkdir(parents=True, exist_ok=True)
                target = results_root / f"{model_id}_results.jsonl"
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                with _RESULT_LOCK:
                    with target.open("a", encoding="utf-8") as handle:
                        handle.write(line)
                        handle.flush()
                        os.fsync(handle.fileno())
            connection.execute(
                "INSERT INTO remote_task_results(request_id,task_id,model_id,created_at,record_json) VALUES(?,?,?,?,?)",
                (request_id, task_id, model_id, now_text(), json.dumps(record, ensure_ascii=False)),
            )
            completed = min(int(row["total_steps"]), int(row["completed_steps"]) + 1)
            message = f"已完成 {completed}/{row['total_steps']} 轮"
            connection.execute(
                "UPDATE remote_tasks SET completed_steps=?,current_model=?,message=?,lease_expires=?,updated_at=? WHERE id=?",
                (completed, model_id, message, time.time() + 900, now_text(), task_id),
            )
            self._event(connection, task_id, message)
            connection.commit()
        return {
            "ok": True,
            "duplicate": False,
            "completed_steps": completed,
            "cancel_requested": False,
            "pause_requested": bool(row["pause_requested"]),
        }

    def finish(self, task_id: str, lease_token: str, payload: dict[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("status") or "completed").lower()
        if requested not in {"completed", "failed", "cancelled", "paused"}:
            raise ValueError("结束状态无效")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._leased_row(connection, task_id, lease_token)
            if row["cancel_requested"]:
                requested = "cancelled"
            elif row["pause_requested"]:
                requested = "paused"
            error = str(payload.get("error") or "")[:2000]
            messages = {
                "completed": "任务已完成",
                "failed": "任务执行失败",
                "cancelled": "任务已取消",
                "paused": "任务已暂停",
            }
            finished_at = "" if requested == "paused" else now_text()
            connection.execute(
                "UPDATE remote_tasks SET status=?,message=?,error=?,lease_token='',lease_expires=0,"
                "cancel_requested=0,pause_requested=0,worker_id='',updated_at=?,finished_at=? WHERE id=?",
                (requested, messages[requested], error, now_text(), finished_at, task_id),
            )
            if row["worker_id"]:
                connection.execute(
                    "UPDATE remote_workers SET last_seen_epoch=?,last_seen=?,status='idle',"
                    "current_task_id='',message='等待任务' WHERE worker_id=?",
                    (time.time(), now_text(), row["worker_id"]),
                )
            self._event(connection, task_id, messages[requested],
                        "error" if requested == "failed" else
                        "warning" if requested == "paused" else "info")
            connection.commit()
        return self.get(task_id) or {}


def task_page_html() -> str:
    """Persistent task console with per-model and per-round inspection controls."""
    return r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>采集任务控制台</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f7f5;color:#172b25;font:14px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif}.wrap{max-width:1260px;margin:28px auto;padding:0 18px}.card{background:#fff;border:1px solid #dce8e3;border-radius:16px;padding:20px;margin-bottom:18px;box-shadow:0 10px 32px #153c2c0c}h1{font-size:27px;margin:0 0 4px}h2{font-size:18px}.topbar,.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.topbar{justify-content:space-between;margin-bottom:18px}.grow{flex:1}.muted{color:#6b7d76}.ok{color:#067647}.error{color:#c43b45}.hidden{display:none!important}a{color:#087f5b}textarea,input,select,button{font:inherit;border:1px solid #c9d9d2;border-radius:9px;padding:9px 11px}textarea{width:100%;min-height:110px}input[type=password]{min-width:310px}button{border:0;background:#108a68;color:#fff;cursor:pointer}button.secondary{background:#60746c}button.warning{background:#b7791f}button.danger{background:#c43b45}button:disabled{opacity:.48;cursor:not-allowed}.badge{padding:3px 9px;border-radius:999px;background:#e9f5f0}.task{padding:18px 0;border-top:1px solid #e9efec}.task:first-child{border-top:0}.bar{height:9px;background:#e5ede9;border-radius:9px;overflow:hidden;margin:9px 0}.bar i{display:block;height:100%;background:linear-gradient(90deg,#139b74,#43c49a)}.customer{font-weight:700;color:#087f5b}.task-actions{margin-top:11px}.model-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin:10px 0}.model-progress{padding:9px 11px;border:1px solid #dce8e3;border-radius:10px;background:#f7faf8}.details{margin-top:14px;border-radius:12px;background:#f7faf8;border:1px solid #dce8e3;padding:13px}.event-log{background:#12221c;color:#d9e8e1;border-radius:10px;padding:10px 12px;margin-bottom:13px;font:12px/1.7 ui-monospace,monospace}.event-log div{border-bottom:1px solid #ffffff14;padding:3px}.event-log time{color:#87d7bb;margin-right:10px}.result-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.result-card{background:#fff;border:1px solid #dce8e3;border-radius:11px;padding:13px;min-width:0}.quality{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0}.quality span{padding:2px 7px;border-radius:999px;background:#eef3f1;font-size:12px}.quality .good{background:#dcfce7;color:#166534}.quality .bad{background:#fee2e2;color:#991b1b}details{margin-top:8px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;word-break:break-word;max-height:380px;overflow:auto;background:#f6f8f7;padding:10px;border-radius:8px;font:13px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif}.sources{margin:8px 0 0;padding-left:20px;word-break:break-all}.worker{padding:10px 12px;border:1px solid #dce8e3;border-radius:10px;background:#f7faf8}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#94a39d;margin-right:6px}.dot.online,.dot.ready{background:#12a878}.dot.running,.dot.queued{background:#e79b28}.login-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.login-card{padding:14px;border:1px solid #dce8e3;border-radius:11px;background:#f7faf8}.login-card header{display:flex;justify-content:space-between}.login-card button{width:100%}@media(max-width:820px){.model-grid,.result-grid,.login-grid{grid-template-columns:1fr}.topbar{align-items:flex-start;flex-direction:column}input[type=password]{width:100%;min-width:0}}
</style></head><body><div class="wrap">
<div class="topbar"><div><h1>采集任务控制台</h1><a href="/geo/">返回客户诊断页</a></div><span class="muted">刷新页面不会停止采集；任务状态保存在服务端</span></div>
<section class="card"><div class="row"><h2 class="grow">任务队列与采集数据</h2><button class="danger" id="cleanup">清理全部已结束/暂停任务</button><button class="secondary" id="reload">立即刷新</button></div><label>管理授权（仅保存在本机 Chrome）<br><input id="key" type="password" autocomplete="off" placeholder="从插件进入会自动填写"></label><div id="task-summary" class="row muted">正在加载…</div><p class="muted">暂停会等待当前正在采集的一轮安全落盘后停止；继续会跳过已有轮次并从断点恢复。停止用于彻底结束任务。</p><div id="tasks" class="muted">输入密钥后加载</div></section>
<section class="card"><div class="row"><h2 class="grow">本机 Worker</h2><span class="muted">20 秒内有心跳视为在线</span></div><div id="workers" class="row muted">输入密钥后加载</div></section>
<section class="card"><h2>三模型隐身登录检测</h2><div id="logins" class="login-grid muted">输入密钥后加载</div></section>
<section class="card"><h2>高级：手动创建任务</h2><label>模型</label><div class="row" id="models"></div><label>问题（每行一个，最多 10 个）</label><textarea id="questions"></textarea><div class="row"><label>循环次数 <select id="rounds"><option>1</option><option>2</option><option>3</option><option>8</option></select></label><label>顺序 <select id="mode"><option value="interleaved">按轮交错</option><option value="sequential">逐题完成</option></select></label><button id="create">提交任务</button><span id="notice"></span></div></section>
</div><script>
const names={doubao:"豆包",yuanbao:"腾讯元宝",wenxin:"文心一言",deepseek:"DeepSeek",quark:"夸克"};
const $=id=>document.getElementById(id),esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safeUrl=value=>{try{const u=new URL(String(value||""));return /^https?:$/.test(u.protocol)?u.href:""}catch(_){return""}};
const apiRoot=window.location.pathname.replace(/\/tasks\/?$/,"/api");
const keyStore="monitorTaskKeyV2";
const openDetails=new Set();
$("models").innerHTML=Object.entries(names).map(([k,v])=>`<label><input type="checkbox" value="${k}" checked> ${v}</label>`).join("");
function headers(){return{"Content-Type":"application/json","X-Monitor-Task-Token":$("key").value}}
async function api(path,options={}){const r=await fetch(path,{...options,headers:{...headers(),...(options.headers||{})},cache:"no-store"});const d=await r.json().catch(()=>({}));if(!r.ok||d.ok===false)throw Error(d.error||`HTTP ${r.status}`);return d}
function statusName(s){return({queued:"排队中",running:"执行中",paused:"已暂停",completed:"已完成",failed:"失败",cancelled:"已取消"})[s]||s}
function loginStatus(s){return({queued:"等待弹窗",running:"等待手动登录",ready:"已就绪",failed:"登录失败"})[s]||"未登录"}
function summary(tasks){const counts={running:0,queued:0,paused:0,completed:0,failed:0,cancelled:0};tasks.forEach(t=>{if(t.status in counts)counts[t.status]+=1});$("task-summary").innerHTML=`<span><b>${counts.running}</b> 个执行中</span><span><b>${counts.queued}</b> 个排队</span><span><b>${counts.paused}</b> 个暂停</span><span>${counts.completed} 个完成</span><span>${counts.failed} 个失败</span><span>${counts.cancelled} 个取消</span>`}
function taskButtons(t){const active=["queued","running"].includes(t.status),editable=["paused","completed","failed","cancelled"].includes(t.status);return `<button class="secondary" onclick="loadDetail('${t.id}')">逐轮数据与日志</button>${active?`<button class="warning" onclick="pauseTask('${t.id}')">暂停</button><button class="danger" onclick="cancelTask('${t.id}')">停止</button>`:""}${t.status==="paused"?`<button onclick="resumeTask('${t.id}')">继续执行</button>`:""}${editable?`<button onclick="rerunTask('${t.id}')">从头重跑</button><button class="warning" onclick="clearTask('${t.id}')">清空数据</button><button class="danger" onclick="deleteTask('${t.id}')">删除任务</button>`:""}`}
function taskCard(t){const pct=t.total_steps?Math.round(t.completed_steps*100/t.total_steps):0,report=t.customer_slug?`<a href="/geo/${esc(t.customer_slug)}" target="_blank">打开客户报告</a>`:"";return `<article class="task"><div class="row"><b>${esc(t.id.slice(0,10))}</b><span class="badge">${statusName(t.status)}</span>${t.customer_slug?`<span class="customer">/${esc(t.customer_slug)} · ${esc(t.brand_name||"")}${t.product_name?" · "+esc(t.product_name):""}</span>`:""}<span>${esc((t.models||[]).map(x=>names[x]||x).join("、"))}</span><span class="muted">${esc(t.created_at)}</span></div><p>${esc(t.message)}${t.current_question?" · "+esc(t.current_question):""}</p><div class="bar"><i style="width:${pct}%"></i></div><div class="row task-actions"><b>${t.completed_steps}/${t.total_steps} 轮 · ${pct}%</b>${taskButtons(t)}${report}${t.error?`<span class="error">${esc(t.error)}</span>`:""}</div><div id="detail-${t.id}" class="details hidden"></div></article>`}
async function load(){if(!$("key").value)return;try{const [d,w,l]=await Promise.all([api(`${apiRoot}/tasks`),api(`${apiRoot}/workers`),api(`${apiRoot}/logins`)]);summary(d.tasks||[]);$("tasks").innerHTML=(d.tasks||[]).length?d.tasks.map(taskCard).join(""):"暂无任务";for(const id of [...openDetails]){if($(`detail-${id}`))renderDetail(id);else openDetails.delete(id)}$("workers").innerHTML=(w.workers||[]).length?w.workers.map(x=>`<div class="worker"><span class="dot ${x.online?"online":""}"></span><b>${esc(x.worker_id)}</b> · ${x.online?"在线":"离线"} · ${esc(x.message)}<br><small>${esc(x.last_seen)}</small></div>`).join(""):"尚无 Worker 心跳";const latest=Object.fromEntries((l.logins||[]).map(x=>[x.model_id,x])),worker=(w.workers||[]).find(x=>x.online)||{};$("logins").innerHTML=["doubao","yuanbao","wenxin"].map(model=>{const item=latest[model]||{},ready=Boolean(worker.readiness?.[model]?.ready),running=["queued","running"].includes(item.status),status=ready?"ready":item.status||"idle";return `<div class="login-card"><header><b>${names[model]}</b><span><i class="dot ${status}"></i>${ready?"已就绪":loginStatus(status)}</span></header><p class="muted">${esc(ready?"已登录并找到输入框":item.message||"尚未检测")}</p><button ${running||!worker.online?"disabled":""} onclick="startLogin('${model}')">${running?"等待登录":ready?"重新检测":"打开登录"}</button></div>`}).join("")}catch(e){$("tasks").innerHTML=`<span class="error">${esc(e.message)}</span>`;$("workers").innerHTML=`<span class="error">${esc(e.message)}</span>`}}
function progressHtml(task){const p=task.model_progress||{};return `<div class="model-grid">${(task.models||[]).map(model=>{const x=p[model]||{completed:0,total:0,completed_rounds:[]};return `<div class="model-progress"><b>${names[model]||esc(model)}</b><br>${x.completed}/${x.total} 轮<br><small class="muted">已采：${esc((x.completed_rounds||[]).join("、")||"无")}</small></div>`}).join("")}</div>`}
function resultHtml(taskId,r){const sourceCount=(r.sources||[]).length,expected=Number(r.expected_source_count||0),analysis=r.analysis||{};const links=(r.sources||[]).map(s=>{const url=safeUrl(s.url||s.href);return url?`<li><a href="${esc(url)}" target="_blank" rel="noopener">${esc(s.title||url)}</a></li>`:`<li>${esc(s.title||"无效链接")}</li>`}).join("");return `<div class="result-card"><div class="row"><b>${names[r.model_id]||esc(r.model_id)} · 第 ${r.round} 轮</b><span class="muted">${esc(r.created_at)}</span></div><p>${esc(r.question)}</p><div class="quality"><span class="${r.body_capture_complete?"good":"bad"}">正文 ${r.body_length} 字 · ${r.body_capture_complete?"完整":"待核查"}</span><span class="${r.source_capture_complete?"good":"bad"}">信源 ${sourceCount}/${expected}</span><span class="${Object.keys(analysis).length?"good":"bad"}">分析 ${Object.keys(analysis).length?"完成":"缺失"}</span><span>${r.recommended?"命中品牌":"未命中品牌"}${r.rank?" · 排名 "+r.rank:""}</span></div><details><summary>回答正文</summary><pre>${esc(r.body||"未采集到正文")}</pre></details><details><summary>全部信源链接</summary>${links?`<ol class="sources">${links}</ol>`:`<p class="muted">本轮没有识别到外部信源</p>`}</details><button class="danger" onclick="deleteResult('${taskId}','${esc(r.request_id)}')">删除这一轮数据</button></div>`}
async function renderDetail(id){const box=$(`detail-${id}`);if(!box)return;box.innerHTML="正在加载逐轮数据…";box.classList.remove("hidden");try{const [d,r]=await Promise.all([api(`${apiRoot}/tasks/${id}`),api(`${apiRoot}/tasks/${id}/results`)]);const events=d.task.events||[];box.innerHTML=`<h3>模型进度</h3>${progressHtml(d.task)}<h3>实时日志</h3><div class="event-log">${events.length?events.map(x=>`<div class="${esc(x.level)}"><time>${esc(x.created_at)}</time>${esc(x.message)}</div>`).join(""):"暂无日志"}</div><h3>每模型每轮采集结果（${r.results.length}）</h3><div class="result-grid">${r.results.length?r.results.map(x=>resultHtml(id,x)).join(""):"尚未采集到正文与信源"}</div>`}catch(e){box.innerHTML=`<span class="error">${esc(e.message)}</span>`}}
function loadDetail(id){const box=$(`detail-${id}`);if(openDetails.has(id)){openDetails.delete(id);box?.classList.add("hidden");return}openDetails.add(id);renderDetail(id)}
async function action(id,name,question){if(question&&!confirm(question))return;try{await api(`${apiRoot}/tasks/${id}/${name}`,{method:"POST",body:"{}"});await load()}catch(e){alert(e.message)}}
const pauseTask=id=>action(id,"pause","暂停后会保留已采轮次，确定暂停？");const resumeTask=id=>action(id,"resume");const cancelTask=id=>action(id,"cancel","停止后任务不会自动继续，确定停止？");const rerunTask=id=>action(id,"rerun","将创建一个从第 1 轮开始的新任务，确定重跑？");const clearTask=id=>action(id,"clear","确定清空该任务全部正文、信源和分析数据？");const deleteTask=id=>action(id,"delete","确定永久删除任务、正文、信源和日志？");
async function deleteResult(taskId,requestId){if(!confirm("确定删除这一轮正文、信源和分析数据？任务会转为暂停，可继续补采这一轮。"))return;try{await api(`${apiRoot}/tasks/${taskId}/results/${encodeURIComponent(requestId)}/delete`,{method:"POST",body:"{}"});openDetails.add(taskId);await load()}catch(e){alert(e.message)}}
async function startLogin(model){try{await api(`${apiRoot}/logins/${model}/start`,{method:"POST",body:"{}"});await load()}catch(e){alert(e.message)}}
async function cleanup(){if(!confirm("永久清理全部已结束和已暂停任务及其数据？"))return;try{const d=await api(`${apiRoot}/tasks/cleanup`,{method:"POST",body:"{}"});alert(`已清理 ${d.deleted||0} 个任务`);load()}catch(e){alert(e.message)}}
$("key").value=localStorage.getItem(keyStore)||sessionStorage.getItem("monitorTaskKey")||"";$("key").oninput=()=>{localStorage.setItem(keyStore,$("key").value);sessionStorage.setItem("monitorTaskKey",$("key").value);load()};
$("create").onclick=async()=>{const models=[...document.querySelectorAll("#models input:checked")].map(x=>x.value),questions=$("questions").value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);try{const d=await api(`${apiRoot}/tasks`,{method:"POST",body:JSON.stringify({models,questions,rounds:Number($("rounds").value),question_mode:$("mode").value})});$("notice").className="ok";$("notice").textContent=`已创建 ${d.task.id.slice(0,10)}`;load()}catch(e){$("notice").className="error";$("notice").textContent=e.message}};
$("reload").onclick=load;$("cleanup").onclick=cleanup;load();setInterval(load,5000);
</script></body></html>'''
