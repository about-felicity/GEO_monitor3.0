from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from monitor_core.quality import answer_quality_reason, repair_fragmented_answer
from windows_worker_sdk.contracts import CapturedAnswer, ProgressCallback


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = ROOT.parent / "DouBao_Monitor_v2.0"
DEFAULT_ADB = Path(r"D:\Program Files\Microvirt\MEmu\adb.exe")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_provider_answer(model_id: str, question: str, body: str) -> None:
    """Reject provider errors, page chrome and cross-topic replies before upload."""
    text = str(body or "").strip()
    if model_id == "kimi" and any(marker in text for marker in (
        "和Kimi聊的人太多了", "和Kimi聊天的人太多了",
        "Kimi有点累了", "晚点再问我一遍", "订阅会员可进入优先队列",
        "服务繁忙", "请求过于频繁",
    )):
        raise RuntimeError("Kimi 暂时繁忙，本轮将自动退避后重试")
    prompt = str(question or "").strip()
    prompt_count = text.count(prompt) if prompt else 0
    if prompt_count >= 2 or any(
        _PROVIDER_NAVIGATION_MARKER.fullmatch(line.strip())
        for line in text.replace("\r", "\n").split("\n")
    ):
        display = {
            "doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言",
            "quark": "千问", "deepseek": "DeepSeek", "kimi": "Kimi",
        }.get(model_id, model_id)
        raise RuntimeError(f"{display} 正文定位命中页面导航或重复提问，本轮作废并自动重试")
    quality_reason = answer_quality_reason(question, text)
    if quality_reason:
        display = {
            "doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言",
            "quark": "千问", "deepseek": "DeepSeek", "kimi": "Kimi",
        }.get(model_id, model_id)
        raise RuntimeError(f"{display} 回答未通过问题一致性校验：{quality_reason}；本轮作废并新建会话重试")


def _validate_collected_question(
    model_id: str, expected_question: str, value: dict[str, Any]
) -> str:
    """Gate 1: prove the captured conversation belongs to this prompt.

    Mobile collectors additionally verify the selected conversation in the
    provider page.  This shared check preserves that observed question in the
    result contract and rejects an explicit mismatch before topic analysis.
    """
    actual = str(
        value.get("captured_question") or value.get("actual_question")
        or value.get("question") or value.get("prompt") or ""
    ).strip()
    if not actual:
        return ""
    expected_key = re.sub(r"\s+", "", str(expected_question or "")).casefold()
    actual_key = re.sub(r"\s+", "", actual).casefold()
    if actual_key != expected_key:
        display = {
            "doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言",
            "quark": "千问", "deepseek": "DeepSeek", "kimi": "Kimi",
        }.get(model_id, model_id)
        raise RuntimeError(f"{display} 网页会话中的问题与本轮任务不一致，本轮作废并新建会话重试")
    return actual


_PROVIDER_NAVIGATION_MARKER = re.compile(
    r"^(?:近期对话|最近对话|历史对话|全部对话|对话历史|我的对话|"
    r"历史会话|最近会话|我的会话|新对话|新建对话|新会话|新建会话|"
    r"recent\s+chats?|chat\s+history|new\s+chat)\s*$",
    re.I,
)


def _navigation_free_body(model_id: str, question: str, body: str) -> str:
    """Remove provider chrome and echoed prompts before quality validation."""
    lines = [line.strip() for line in str(body or "").replace("\r", "\n").split("\n")]
    clean: list[str] = []
    question_key = re.sub(r"\s+", "", str(question or "")).casefold()
    skip_role = False
    for index, line in enumerate(lines):
        if _PROVIDER_NAVIGATION_MARKER.fullmatch(line):
            break
        line_key = re.sub(r"\s+", "", line).casefold()
        if not clean and line_key in {"用户", "user"} and index + 1 < len(lines):
            next_key = re.sub(r"\s+", "", lines[index + 1]).casefold()
            if question_key and next_key == question_key:
                skip_role = True
                continue
        if skip_role and question_key and line_key == question_key:
            skip_role = False
            continue
        if not clean and line_key in {"助手", "assistant"}:
            continue
        if not clean and question_key and line_key == question_key:
            continue
        clean.append(line)
    return "\n".join(clean).strip()


def _sanitize_provider_result(
    model_id: str, question: str, value: dict[str, Any]
) -> dict[str, Any]:
    """Return a copy whose answer fields contain assistant prose only."""
    result = dict(value)
    raw = str(
        result.get("body") or result.get("web_body") or result.get("reply")
        or result.get("answerText") or ""
    )
    clean = _navigation_free_body(model_id, question, raw)
    sources = [
        dict(item) for item in (result.get("sources") or result.get("items") or [])
        if isinstance(item, dict)
    ]
    clean = _source_free_body(clean, sources)
    clean = repair_fragmented_answer(clean, provider=model_id)
    if len("".join(clean.split())) < 6:
        display = {
            "doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言",
            "quark": "千问", "deepseek": "DeepSeek", "kimi": "Kimi",
        }.get(model_id, model_id)
        raise RuntimeError(f"{display} 未抓到完整回答正文，本轮作废并自动重试")
    result["answer_sanitized"] = clean != raw.strip()
    result["body"] = clean
    result["web_body"] = clean
    result["reply"] = clean
    return result


def _source_free_body(body: str, sources: list[dict[str, Any]]) -> str:
    """Keep source-card metadata out of answer prose for every collector."""
    generic_titles = {"全部", "详情", "查看", "打开", "来源", "网页", "链接", "更多"}
    raw_titles = [
        title for item in sources
        for title in [" ".join(str(item.get("title") or "").split()).strip()]
        if len(title) >= 2 and title.casefold() not in generic_titles
    ]
    titles = {re.sub(r"\s+", "", title).casefold() for title in raw_titles}
    urls = {
        str(item.get("url") or item.get("href") or "").strip()
        for item in sources
    }
    marker = re.compile(
        r"^(?:相关视频|参考(?:资料|来源|链接|信源)|引用(?:来源|链接|信源)|"
        r"资料来源|信息来源|sources?|references?|related\s+videos?)\s*[:：]?$",
        re.I,
    )
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(body or "").replace("\r", "\n").split("\n")]
    output: list[str] = []
    for index, line in enumerate(lines):
        for title in raw_titles:
            line = line.replace(title, "").strip(" -–—:：")
        if not line:
            if output and output[-1]:
                output.append("")
            continue
        compact = re.sub(r"\s+", "", line).casefold()
        if marker.fullmatch(line) and index >= max(1, len(lines) // 3):
            break
        if line in urls or re.fullmatch(r"https?://\S+", line, re.I) or compact in titles:
            continue
        output.append(line)
    return "\n".join(output).strip()


def _activity(model_id: str):
    # Lazy import keeps the low-level memory/ADB helpers usable by the
    # supervisor without creating an import cycle during module startup.
    from .supervisor import SUPERVISOR
    return SUPERVISOR.activity(model_id)


def _ensure_supervisor() -> None:
    """Start the always-on health supervisor as soon as the worker builds collectors."""
    from .supervisor import SUPERVISOR  # noqa: F401


def _physical_memory() -> dict[str, int]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    value = MEMORYSTATUSEX()
    value.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if os.name == "nt" and ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        return {"load": int(value.dwMemoryLoad), "total": int(value.ullTotalPhys), "available": int(value.ullAvailPhys)}
    return {"load": 0, "total": 1, "available": 1}


def _resource_gate(
    progress: ProgressCallback | None = None, *, wait: bool = False
) -> None:
    """Keep memory pressure from turning a healthy diagnosis into a failed task.

    Readiness probes remain fail-fast so the server does not claim new work while
    the host is already under pressure. Once a task is running, however, a new
    heavyweight child waits for memory to recover instead of aborting every
    model and discarding the unfinished rounds.
    """
    deadline = time.monotonic() + max(
        60, min(600, int(os.environ.get("GEO_RESOURCE_WAIT_SECONDS", "240")))
    )
    next_notice = 0.0
    while True:
        memory = _physical_memory()
        free_ratio = memory["available"] / max(1, memory["total"])
        if memory["load"] < 88 and free_ratio >= 0.12:
            return
        if not wait or time.monotonic() >= deadline:
            raise RuntimeError(
                f"主机内存安全门禁等待超时：占用 {memory['load']}%，可用 {memory['available'] // 1048576} MB"
            )
        now = time.monotonic()
        if progress is not None and now >= next_notice:
            progress("系统正在自动平衡本机资源，本轮采集稍后继续")
            next_notice = now + 20
        time.sleep(3)


def _read_last_jsonl(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError("采集子进程未生成结果文件")
    found: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                found = value
    if found is None:
        raise RuntimeError("采集结果文件为空")
    return found


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError("采集子进程未生成结果文件")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
    if not rows:
        raise RuntimeError("采集结果文件为空")
    return rows


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _recover_yuanbao_web_result(
    path: Path, question: str, previous_signature: tuple[int, int] | None
) -> dict[str, Any] | None:
    """Recover a fresh browser result when the legacy loop exits before JSONL append.

    Yuanbao writes its browser capture before the compatibility JSONL record. If
    the final source completeness check exhausts its retries, the browser file
    can contain a valid answer while the child exits cleanly without JSONL. Only
    a file changed by this exact run and matching this exact question is eligible.
    """
    current_signature = _file_signature(path)
    if current_signature is None or current_signature == previous_signature:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or str(value.get("question") or "").strip() != question.strip():
        return None
    body = str(value.get("body") or "").strip()
    sources = [dict(item) for item in value.get("sources") or [] if isinstance(item, dict)]
    if not body:
        return None
    expected = max(int(value.get("expected_source_count") or 0), len(sources))
    recovered = dict(value)
    recovered.update({
        "status": "success",
        "skip_reason": "",
        "reply": body,
        "web_body": body,
        "sources": sources,
        "expected_source_count": expected,
        "body_capture_complete": True,
        "source_capture_complete": bool(
            value.get("source_capture_complete") and len(sources) >= expected
        ),
        "recovered_from_browser_result": True,
    })
    return recovered


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, timeout=15, check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_process(
    command: list[str], progress: ProgressCallback, *, timeout: int, cwd: Path,
    tick: Callable[[], None] | None = None,
) -> list[str]:
    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUNBUFFERED", "1")
    bundled_driver = ROOT / "runtime" / "chromedriver-151" / "chromedriver-win64" / "chromedriver.exe"
    if bundled_driver.is_file():
        environment.setdefault("GEO_CHROMEDRIVER_PATH", str(bundled_driver))
    process = subprocess.Popen(
        command, cwd=str(cwd), env=environment, text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW, bufsize=1,
    )
    lines: deque[str] = deque(maxlen=120)
    events: queue.Queue[str | None] = queue.Queue(maxsize=256)

    def reader() -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip()
            lines.append(line)
            try:
                events.put_nowait(line)
            except queue.Full:
                pass
        try:
            events.put_nowait(None)
        except queue.Full:
            pass

    threading.Thread(target=reader, name="geo-child-output", daemon=True).start()
    deadline = time.monotonic() + timeout
    next_heartbeat = 0.0
    latest = ""
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"采集子进程超过 {timeout} 秒硬超时")
            try:
                item = events.get(timeout=1)
                if item:
                    latest = item[-300:]
            except queue.Empty:
                pass
            if tick:
                tick()
            if now >= next_heartbeat:
                progress(latest or "本地采集进行中")
                # Control polling doubles as the preemption interrupt. Keep it
                # short enough that an interactive diagnosis does not sit
                # behind a long paid-monitor browser wait.
                next_heartbeat = now + max(
                    3, int(os.environ.get("GEO_CHILD_HEARTBEAT_SECONDS", "5"))
                )
        code = process.wait(timeout=5)
        if code:
            tail = " | ".join(list(lines)[-8:])[-1800:]
            raise RuntimeError(f"采集子进程退出码 {code}: {tail}")
        if tick:
            tick()
        return list(lines)
    except BaseException:
        _terminate_tree(process)
        raise


_RUN_SLOTS = threading.BoundedSemaphore(
    max(1, min(6, int(os.environ.get("GEO_SUBPROCESS_CONCURRENCY", "4"))))
)


def _run(
    command: list[str], progress: ProgressCallback, *, timeout: int, cwd: Path,
    tick: Callable[[], None] | None = None,
) -> list[str]:
    with _RUN_SLOTS:
        _resource_gate(progress, wait=True)
        return _run_process(command, progress, timeout=timeout, cwd=cwd, tick=tick)


def _captured(value: dict[str, Any], *, mode: str, started: str) -> CapturedAnswer:
    sources = [dict(item) for item in value.get("sources") or value.get("items") or [] if isinstance(item, dict)]
    raw_body = str(value.get("body") or value.get("web_body") or value.get("reply") or value.get("answerText") or "").strip()
    body = _source_free_body(raw_body, sources)
    expected = int(value.get("expected_source_count") or value.get("expectedCount") or value.get("count") or len(sources))
    complete = bool(value.get("source_capture_complete", value.get("complete", len(sources) >= expected)))
    return CapturedAnswer(
        body=body,
        captured_question=str(value.get("captured_question") or ""),
        capture_identity=str(
            value.get("capture_identity") or value.get("page_navigation_id")
            or value.get("harvest_navigation_id") or value.get("submission_navigation_id")
            or value.get("conversation_id") or value.get("chat_url")
            or value.get("page_url") or value.get("url") or ""
        ),
        sources=sources,
        page_url=str(value.get("url") or value.get("page_url") or value.get("chat_url") or ""),
        expected_source_count=expected,
        body_capture_complete=bool(value.get("body_capture_complete", bool(body))),
        source_capture_complete=complete,
        body_capture_origin=str(value.get("body_capture_origin") or "browser_dom"),
        source_capture_origins=dict(value.get("source_capture_origins") or {"browser": len(sources)}),
        capture_mode=mode,
        started_at=started,
        finished_at=_now(),
    )


def _yuanbao_capture_incomplete_reason(value: dict[str, Any]) -> str:
    body = str(value.get("body") or value.get("web_body") or value.get("reply") or "").strip()
    if not body:
        return "正文为空"
    pairs = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"))
    for opening, closing in pairs:
        if body.count(opening) > body.count(closing):
            return f"正文末尾存在未闭合符号 {opening}"
    tail = body.rstrip("。！？!?；;，,、：:\n ")
    if re.search(r"(?:但|而|并|且|以及|同时|不过|然而)\s*[A-Za-z0-9][A-Za-z0-9+._/-]{0,15}$", tail):
        return "正文停在未完成的转折或补充语句"
    expected = max(
        int(value.get("expected_source_count") or 0),
        len(value.get("sources") or []),
    )
    if expected >= 10 and len(body) < 220:
        return f"{expected} 条信源仅对应 {len(body)} 字正文，疑似只抓到首段"
    return ""


class SubprocessCollector:
    model_id = ""

    def close(self) -> None:
        return None

    def check_ready(self) -> dict[str, Any]:
        _resource_gate()
        return {"ready": True, "message": f"{self.model_id} collector configured"}


class KimiExtensionCollector(SubprocessCollector):
    """Independent Kimi collector backed by the user's Chrome extension."""

    model_id = "kimi"
    batch_collection_enabled = True

    def __init__(self) -> None:
        from .kimi_extension import KimiExtensionClient

        self.client = KimiExtensionClient()
        self.task_id = "standalone"

    def prepare_task(self, task_id: str, _task_kind: str) -> None:
        self.task_id = re.sub(r"[^A-Za-z0-9_-]", "-", str(task_id or ""))[:80] or "task"

    def check_ready(self) -> dict[str, Any]:
        return self.client.probe()

    def _result(self, question: str, row: dict[str, Any], started: str) -> CapturedAnswer:
        captured_question = _validate_collected_question(self.model_id, question, row)
        value = _sanitize_provider_result(self.model_id, question, row)
        value["captured_question"] = captured_question or question
        _validate_provider_answer(self.model_id, question, str(value.get("body") or ""))
        return _captured(value, mode="kimi_chrome_extension", started=started)

    def collect_new_conversation(
        self, question: str, round_number: int, progress: ProgressCallback
    ) -> CapturedAnswer:
        started = _now()
        _resource_gate(progress, wait=True)
        with _activity(self.model_id):
            rows = self.client.run_job(
                job_id=f"geo-{self.task_id}-kimi-{round_number}",
                questions=[question], rounds=1, progress=progress,
                timeout=int(os.environ.get("GEO_KIMI_EXTENSION_TIMEOUT", "240")),
            )
        return self._result(question, rows[0], started)

    def collect_batch(
        self, rounds: list[tuple[int, str]], progress: ProgressCallback
    ) -> dict[int, CapturedAnswer]:
        if len(rounds) < 2:
            number, question = rounds[0]
            return {number: self.collect_new_conversation(question, number, progress)}
        questions = [question for _, question in rounds]
        unique_questions = list(dict.fromkeys(questions))
        if len(unique_questions) == 1:
            job_questions, job_rounds = unique_questions, len(rounds)
        elif len(unique_questions) == len(questions):
            job_questions, job_rounds = questions, 1
        else:
            return {
                number: self.collect_new_conversation(question, number, progress)
                for number, question in rounds
            }
        started = _now()
        _resource_gate(progress, wait=True)
        with _activity(self.model_id):
            rows = self.client.run_job(
                job_id=f"geo-{self.task_id}-kimi-batch",
                questions=job_questions, rounds=job_rounds, progress=progress,
                timeout=int(os.environ.get("GEO_KIMI_EXTENSION_TIMEOUT", "240")),
            )
        if len(rows) != len(rounds):
            raise RuntimeError(f"Kimi 插件批量回收仅完成 {len(rows)}/{len(rounds)} 轮")
        return {
            number: self._result(question, row, started)
            for (number, question), row in zip(rounds, rows)
        }


class DoubaoCollector(SubprocessCollector):
    model_id = "doubao"
    serial = os.environ.get("GEO_DOUBAO_SERIAL", "127.0.0.1:21513")

    def __init__(self) -> None:
        self._batch_size = 1
        self._batch_question = ""
        self._batch_answers: deque[CapturedAnswer] = deque()

    def prepare_diagnosis(self, pending_rounds: int) -> None:
        self._batch_size = max(1, min(3, int(pending_rounds)))
        self._batch_question = ""
        self._batch_answers.clear()

    def check_ready(self) -> dict[str, Any]:
        _resource_gate()
        adb = Path(os.environ.get("GEO_ADB_PATH", DEFAULT_ADB))
        if not adb.is_file():
            return {"ready": False, "message": f"ADB 不存在：{adb}"}
        check = subprocess.run([str(adb), "-s", self.serial, "get-state"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=12)
        ready = check.returncode == 0 and "device" in check.stdout
        return {"ready": ready, "message": "豆包模拟器在线，网页账号将在首轮校验" if ready else "豆包模拟器离线"}

    def _command(self, question: str, rounds: int, results: Path, temp: Path) -> list[str]:
        adb = Path(os.environ.get("GEO_ADB_PATH", DEFAULT_ADB))
        command = [
            sys.executable, str(LEGACY_ROOT / "doubao_mumu_controller" / "doubao_mumu_web_pipeline.py"),
            "--question", question, "--rounds", str(rounds), "--device-index", os.environ.get("GEO_DOUBAO_DEVICE_INDEX", "1"),
            "--browser-slot", os.environ.get("GEO_DOUBAO_BROWSER_SLOT", "1"), "--adb", str(adb),
            "--login-wait-seconds", os.environ.get("GEO_LOGIN_WAIT_SECONDS", "30"),
            "--max-round-retries", "3", "--results", str(results), "--log", str(temp / "collector.log"),
            "--diagnostics-dir", str(temp / "diagnostics"),
        ]
        if rounds > 1:
            command += ["--keep-app-running-between-rounds", "--burst-submit",
                        "--burst-settle-seconds", os.environ.get("GEO_BATCH_SETTLE_SECONDS", "15")]
        return command

    def _capture_row(self, row: dict[str, Any], question: str, started: str) -> CapturedAnswer:
        captured_question = _validate_collected_question(self.model_id, question, row)
        payload = _sanitize_provider_result(
            self.model_id, question, dict(row.get("capture_payload") or {}),
        )
        payload["captured_question"] = captured_question or question
        payload.setdefault("chat_url", row.get("chat_url"))
        _validate_provider_answer(self.model_id, question, str(payload.get("body") or ""))
        return _captured(payload, mode="emulator_question_chrome_capture", started=started)

    def collect_batch_progressive(
        self,
        rounds: list[tuple[int, str]],
        progress: ProgressCallback,
        on_result: Callable[[int, CapturedAnswer], None],
    ) -> dict[int, CapturedAnswer]:
        """Publish each harvested Doubao answer instead of waiting for the whole burst."""
        output: dict[int, CapturedAnswer] = {}
        cursor = 0
        while cursor < len(rounds):
            question = rounds[cursor][1]
            chunk: list[tuple[int, str]] = []
            while (cursor < len(rounds) and len(chunk) < 3
                   and rounds[cursor][1] == question):
                chunk.append(rounds[cursor])
                cursor += 1
            started = _now()
            with tempfile.TemporaryDirectory(prefix="geo-doubao-") as folder:
                temp = Path(folder)
                results = temp / "result.jsonl"
                emitted: set[int] = set()

                def publish_available() -> None:
                    if not results.is_file():
                        return
                    try:
                        rows = _read_jsonl_rows(results)
                    except RuntimeError:
                        # The child creates the JSONL before its first atomic
                        # append. An empty file means "not ready yet", not a
                        # failed batch.
                        return
                    rows_by_round = {
                        int(row.get("round") or 0): row
                        for row in rows
                        if row.get("ok") and int(row.get("round") or 0) > 0
                    }
                    for local_round, (actual_round, expected_question) in enumerate(chunk, 1):
                        if actual_round in emitted or local_round not in rows_by_round:
                            continue
                        row = rows_by_round[local_round]
                        if str(row.get("question") or "") != expected_question:
                            raise RuntimeError(f"豆包第 {actual_round} 轮问题指纹不匹配")
                        captured = self._capture_row(row, expected_question, started)
                        output[actual_round] = captured
                        emitted.add(actual_round)
                        on_result(actual_round, captured)
                        progress(f"豆包已完成 {len(output)}/{len(rounds)} 轮，结果已入库")

                with _activity(self.model_id):
                    _run(
                        self._command(question, len(chunk), results, temp), progress,
                        timeout=int(os.environ.get("GEO_DOUBAO_TIMEOUT", "720")),
                        cwd=LEGACY_ROOT, tick=publish_available,
                    )
                publish_available()
                missing = [number for number, _ in chunk if number not in output]
                if missing:
                    raise RuntimeError(
                        f"豆包批量采集缺少轮次：{'、'.join(map(str, missing))}"
                    )
        return output

    def collect_batch(
        self, rounds: list[tuple[int, str]], progress: ProgressCallback
    ) -> dict[int, CapturedAnswer]:
        return self.collect_batch_progressive(rounds, progress, lambda _number, _captured: None)

    def collect_new_conversation(self, question: str, round_number: int, progress: ProgressCallback) -> CapturedAnswer:
        if self._batch_answers and self._batch_question == question:
            return self._batch_answers.popleft()
        started = _now()
        with tempfile.TemporaryDirectory(prefix="geo-doubao-") as folder:
            temp = Path(folder)
            results = temp / "result.jsonl"
            with _activity(self.model_id):
                _run(
                    self._command(question, self._batch_size, results, temp), progress,
                    timeout=int(os.environ.get("GEO_DOUBAO_TIMEOUT", "720")), cwd=LEGACY_ROOT,
                )
            rows = _read_jsonl_rows(results)
            answers: list[CapturedAnswer] = []
            for row in rows:
                if not row.get("ok"):
                    raise RuntimeError(str(row.get("error") or "豆包未返回成功结果"))
                answers.append(self._capture_row(row, question, started))
            if len(answers) < self._batch_size:
                raise RuntimeError(f"豆包批量采集仅完成 {len(answers)}/{self._batch_size} 轮")
            self._batch_question = question
            self._batch_answers.extend(answers[1:self._batch_size])
            return answers[0]


class YuanbaoCollector(SubprocessCollector):
    model_id = "yuanbao"
    serial = os.environ.get("GEO_YUANBAO_SERIAL", "127.0.0.1:21503")
    # The emulator burst path can spend over a minute before reporting an
    # incomplete batch and then repeat every round in the reliable fallback.
    # Start with the proven one-round path so each sample is checkpointed and
    # the failed speculative attempt cannot extend the customer wait.
    batch_collection_enabled = False

    def check_ready(self) -> dict[str, Any]:
        _resource_gate()
        adb = Path(os.environ.get("GEO_ADB_PATH", DEFAULT_ADB))
        if not adb.is_file():
            return {"ready": False, "message": f"ADB 不存在：{adb}"}
        check = subprocess.run([str(adb), "-s", self.serial, "get-state"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=12)
        ready = check.returncode == 0 and "device" in check.stdout
        return {"ready": ready, "message": "元宝模拟器在线，网页账号将在首轮校验" if ready else "元宝模拟器离线"}

    def collect_new_conversation(self, question: str, round_number: int, progress: ProgressCallback) -> CapturedAnswer:
        return self.collect_batch([(round_number, question)], progress)[round_number]

    def collect_batch(
        self, rounds: list[tuple[int, str]], progress: ProgressCallback
    ) -> dict[int, CapturedAnswer]:
        if not rounds:
            return {}
        started = _now()
        profile = LEGACY_ROOT / "yuanbao_monitor" / "chrome_profile_auto"
        command = [
            sys.executable, "-m", "windows_enterprise_worker.yuanbao_burst",
            "--legacy-root", str(LEGACY_ROOT),
            "--questions-json", json.dumps([question for _, question in rounds], ensure_ascii=False),
            "--serial", self.serial,
            "--chrome-port", os.environ.get("GEO_YUANBAO_CHROME_PORT", "9222"),
            "--profile", str(profile),
            "--settle-seconds", os.environ.get("GEO_BATCH_SETTLE_SECONDS", "15"),
            "--timeout", os.environ.get("GEO_YUANBAO_WEB_TIMEOUT", "180"),
        ]
        with _activity(self.model_id):
            lines = _run(
                command, progress,
                timeout=int(os.environ.get("GEO_YUANBAO_TIMEOUT", "720")), cwd=ROOT,
            )
        value: dict[str, Any] | None = None
        for line in reversed(lines):
            if line.startswith("GEO_YUANBAO_BURST_JSON="):
                parsed = json.loads(line.partition("=")[2])
                value = parsed if isinstance(parsed, dict) else None
                break
        results = value.get("results") if value else None
        if not isinstance(results, list) or len(results) != len(rounds):
            raise RuntimeError(f"元宝批量回收仅完成 {len(results or [])}/{len(rounds)} 轮")
        output: dict[int, CapturedAnswer] = {}
        for (number, question), row in zip(rounds, results):
            if not isinstance(row, dict) or str(row.get("question") or "") != question:
                raise RuntimeError(f"元宝第 {number} 轮问题指纹不匹配")
            captured_question = _validate_collected_question(self.model_id, question, row)
            row = _sanitize_provider_result(self.model_id, question, row)
            row["captured_question"] = captured_question or question
            incomplete_reason = _yuanbao_capture_incomplete_reason(row)
            if incomplete_reason:
                raise RuntimeError(f"元宝第 {number} 轮正文疑似截断：{incomplete_reason}")
            row["body_capture_complete"] = True
            _validate_provider_answer(self.model_id, question, str(row.get("body") or ""))
            output[number] = _captured(
                row, mode="yuanbao_emulator_burst_capture", started=started,
            )
        return output


class BridgeCollector(SubprocessCollector):
    def __init__(self, model_id: str):
        self.model_id = model_id

    def _command(self, question: str = "", ready: bool = False) -> list[str]:
        command = [sys.executable, "-m", "windows_enterprise_worker.bridge", self.model_id]
        if self.model_id == "wenxin":
            command += ["--legacy-root", str(LEGACY_ROOT)]
        if ready:
            command.append("--ready")
        else:
            command += ["--question", question]
        if self.model_id in {"deepseek", "kimi"}:
            command.append("--headless")
        elif self.model_id == "quark" and os.environ.get("GEO_QUARK_HEADLESS", "1") != "0":
            command.append("--headless")
        return command

    @staticmethod
    def _json(lines: list[str]) -> dict[str, Any]:
        for line in reversed(lines):
            if line.startswith("GEO_BRIDGE_JSON="):
                value = json.loads(line.partition("=")[2])
                if isinstance(value, dict):
                    return value
        raise RuntimeError("网页采集桥未返回规范 JSON")

    def check_ready(self) -> dict[str, Any]:
        try:
            lines = _run(self._command(ready=True), lambda _: None, timeout=60, cwd=ROOT)
            value = self._json(lines)
            ready = bool(value.get("ready", value.get("ok", False)))
            return {"ready": ready, "message": str(value.get("message") or ("可用" if ready else "未就绪"))[:200]}
        except Exception as exc:
            return {"ready": False, "message": f"{type(exc).__name__}: {exc}"[:200]}

    def collect_new_conversation(self, question: str, round_number: int, progress: ProgressCallback) -> CapturedAnswer:
        started = _now()
        with _activity(self.model_id):
            lines = _run(self._command(question=question), progress, timeout=420, cwd=ROOT)
        value = self._json(lines)
        captured_question = _validate_collected_question(self.model_id, question, value)
        value = _sanitize_provider_result(self.model_id, question, value)
        value["captured_question"] = captured_question
        body = str(value.get("body") or value.get("reply") or value.get("web_body") or "")
        _validate_provider_answer(self.model_id, question, body)
        if self.model_id == "deepseek":
            sources = [item for item in value.get("sources") or [] if isinstance(item, dict)]
            if len("".join(body.split())) < 120 and len(sources) >= 3:
                raise RuntimeError("DeepSeek 仅抓到回答尾段，本轮作废并自动重试")
        return _captured(value, mode=f"{self.model_id}_browser_capture", started=started)

    def collect_batch(
        self, rounds: list[tuple[int, str]], progress: ProgressCallback
    ) -> dict[int, CapturedAnswer]:
        if self.model_id not in {"kimi", "wenxin"} or len(rounds) < 2:
            return {
                number: self.collect_new_conversation(question, number, progress)
                for number, question in rounds
            }
        started = _now()
        questions = [question for _, question in rounds]
        command = self._command()
        command += ["--questions-json", json.dumps(questions, ensure_ascii=False)]
        progress(f"{self.model_id} 正在快速提交 {len(rounds)} 轮独立会话")
        with _activity(self.model_id):
            lines = _run(command, progress, timeout=540, cwd=ROOT)
        value = self._json(lines)
        results = value.get("results")
        if not isinstance(results, list) or len(results) != len(rounds):
            raise RuntimeError(f"{self.model_id} 批量回收仅完成 {len(results or [])}/{len(rounds)} 轮")
        captured: dict[int, CapturedAnswer] = {}
        for (number, question), result in zip(rounds, results):
            if not isinstance(result, dict):
                raise RuntimeError(f"{self.model_id} 第 {number} 轮批量结果格式无效")
            captured_question = _validate_collected_question(self.model_id, question, result)
            result = _sanitize_provider_result(self.model_id, question, result)
            result["captured_question"] = captured_question
            _validate_provider_answer(
                self.model_id, question,
                str(result.get("body") or result.get("reply") or result.get("web_body") or ""),
            )
            captured[number] = _captured(
                result, mode=f"{self.model_id}_browser_burst_capture", started=started,
            )
        return captured


class QuarkQueueCollector(SubprocessCollector):
    model_id = "quark"
    # Queue all missing diagnosis samples together. The extension can start
    # their independent Qianwen conversations as a burst, while the callback
    # below persists each completed sample immediately (12/14, 13/14, 14/14).
    batch_collection_enabled = True
    base_url = os.environ.get("GEO_QUARK_RECEIVER_URL", "http://127.0.0.1:8765").rstrip("/")

    def _request(self, path: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8") if value is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data,
            headers={"Content-Type": "application/json; charset=utf-8"} if data else {},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("千问接收器返回格式无效")
        return result

    def check_ready(self) -> dict[str, Any]:
        try:
            health = self._request("/api/health")
            ready = bool(health.get("ok") and health.get("extension_ready"))
            return {
                "ready": ready,
                "message": "千问扩展与本地任务队列可用" if ready else "请在夸克浏览器重新加载千问采集扩展，并保持 AI 对话页已登录",
            }
        except Exception as exc:
            return {"ready": False, "message": f"千问本地接收器不可用：{type(exc).__name__}"}

    def collect_new_conversation(self, question: str, round_number: int, progress: ProgressCallback) -> CapturedAnswer:
        started = _now()
        task_id = "geo-" + uuid.uuid4().hex
        self._request("/api/tasks", {"task_id": task_id, "prompt": question})
        deadline = time.monotonic() + int(os.environ.get("GEO_QUARK_TIMEOUT", "420"))
        try:
            activity = _activity(self.model_id)
            activity.__enter__()
            while time.monotonic() < deadline:
                status = self._request(f"/api/tasks/{task_id}").get("task") or {}
                state = str(status.get("state") or "")
                if state == "completed":
                    result = status.get("result")
                    if not isinstance(result, dict):
                        raise RuntimeError("千问任务完成但缺少结果")
                    captured_question = _validate_collected_question(
                        self.model_id, question, result
                    )
                    result = _sanitize_provider_result(self.model_id, question, result)
                    result["captured_question"] = captured_question
                    _validate_provider_answer(
                        self.model_id, question,
                        str(result.get("body") or result.get("reply") or result.get("web_body") or ""),
                    )
                    return _captured(result, mode="quark_extension_background", started=started)
                if state in {"failed", "cancelled"}:
                    raise RuntimeError(str(status.get("error") or f"千问任务 {state}"))
                progress(f"千问扩展任务 {state or 'queued'}")
                time.sleep(5)
            raise TimeoutError("千问扩展任务超时")
        except BaseException:
            try:
                self._request(f"/api/tasks/{task_id}/cancel", {"reason": "worker_stopped"})
            except Exception:
                pass
            raise
        finally:
            if 'activity' in locals():
                activity.__exit__(None, None, None)

    def collect_batch(
        self, rounds: list[tuple[int, str]], progress: ProgressCallback
    ) -> dict[int, CapturedAnswer]:
        return self._collect_batch(rounds, progress, None)

    def collect_batch_progressive(
        self,
        rounds: list[tuple[int, str]],
        progress: ProgressCallback,
        on_result: Callable[[int, CapturedAnswer], None],
    ) -> dict[int, CapturedAnswer]:
        return self._collect_batch(rounds, progress, on_result)

    def _collect_batch(
        self,
        rounds: list[tuple[int, str]],
        progress: ProgressCallback,
        on_result: Callable[[int, CapturedAnswer], None] | None,
    ) -> dict[int, CapturedAnswer]:
        if len(rounds) < 2:
            number, question = rounds[0]
            captured = self.collect_new_conversation(question, number, progress)
            if on_result:
                on_result(number, captured)
            return {number: captured}
        started = _now()
        batch_id = "geo-batch-" + uuid.uuid4().hex
        identities = [(number, question, "geo-" + uuid.uuid4().hex) for number, question in rounds]
        self._request("/api/tasks/batch", {
            "batch_id": batch_id,
            "tasks": [{"task_id": task_id, "prompt": question} for _, question, task_id in identities],
        })
        deadline = time.monotonic() + int(os.environ.get("GEO_QUARK_BATCH_TIMEOUT", "540"))
        output: dict[int, CapturedAnswer] = {}
        replacement_attempts: dict[int, int] = {number: 0 for number, _, _ in identities}
        max_replacements = max(1, min(3, int(os.environ.get("GEO_QUARK_BATCH_REPLACEMENTS", "2"))))
        with _activity(self.model_id):
            while time.monotonic() < deadline:
                for index, (number, question, task_id) in enumerate(list(identities)):
                    if number in output:
                        continue
                    status = self._request(f"/api/tasks/{task_id}").get("task") or {}
                    state = str(status.get("state") or "")
                    if state == "completed":
                        result = status.get("result")
                        if not isinstance(result, dict):
                            raise RuntimeError(f"千问第 {number} 轮缺少结果")
                        captured_question = _validate_collected_question(self.model_id, question, result)
                        result = _sanitize_provider_result(self.model_id, question, result)
                        result["captured_question"] = captured_question
                        _validate_provider_answer(
                            self.model_id, question,
                            str(result.get("body") or result.get("reply") or result.get("web_body") or ""),
                        )
                        captured = _captured(
                            result, mode="quark_extension_burst_capture", started=started,
                        )
                        output[number] = captured
                        if on_result:
                            on_result(number, captured)
                    elif state in {"failed", "cancelled"}:
                        # Keep every successful sample. Quark occasionally
                        # suppresses one repeated request without an answer;
                        # discarding the whole batch made the public progress
                        # sit at 11/14 and needlessly re-ran completed samples.
                        attempt = replacement_attempts[number] + 1
                        if attempt > max_replacements:
                            raise RuntimeError(str(status.get("error") or f"千问第 {number} 轮 {state}"))
                        replacement_attempts[number] = attempt
                        replacement_id = "geo-" + uuid.uuid4().hex
                        self._request("/api/tasks", {
                            "task_id": replacement_id,
                            "prompt": question,
                            "sample_index": number,
                            "retry_attempt": attempt,
                        })
                        identities[index] = (number, question, replacement_id)
                        progress(
                            f"千问第 {number} 轮未返回正文，已保留其余结果并单独恢复"
                            f"（{attempt}/{max_replacements}）"
                        )
                if len(output) == len(rounds):
                    return output
                progress(f"千问已回收 {len(output)}/{len(rounds)} 轮")
                time.sleep(3)
        for _, _, task_id in identities:
            try:
                self._request(f"/api/tasks/{task_id}/cancel", {"reason": "batch_timeout"})
            except Exception:
                pass
        raise TimeoutError(f"千问批量任务超时：{len(output)}/{len(rounds)}")


def create_collector(model_id: str):
    _ensure_supervisor()
    normalized = str(model_id).strip().casefold()
    if normalized == "doubao":
        return DoubaoCollector()
    if normalized == "yuanbao":
        return YuanbaoCollector()
    if normalized == "kimi":
        return KimiExtensionCollector()
    if normalized in {"wenxin", "deepseek"}:
        return BridgeCollector(normalized)
    if normalized == "quark":
        return QuarkQueueCollector()
    raise ValueError(f"不支持的模型：{model_id}")
