"""Framework-neutral task runner for a replacement Windows worker."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .contracts import Analyzer, Collector, build_record
from .protocol import ServerClient, ServerError


MODEL_ORDER = ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi")
DIAGNOSIS_FIXED_ROUNDS = {"deepseek": 2, "kimi": 2}
# Start the longest serial collectors first. Quark does not consume a child
# process slot, so it can still run immediately even when submitted last.
EXECUTION_PRIORITY = ("kimi", "doubao", "yuanbao", "wenxin", "deepseek", "quark")


def round_attempts(model_id: str) -> int:
    """Return a bounded retry budget without multiplying provider retries.

    Quark already performs the only useful page-level recovery inside its
    extension (reload, new conversation and one re-submit). Repeating that
    whole workflow again in the outer worker turned one 45 second provider
    stall into several minutes per sample. Missing Quark rounds are still
    checkpointed and retried by the durable server queue after backoff.
    """
    key = "GEO_QUARK_ROUND_ATTEMPTS" if model_id == "quark" else "GEO_ROUND_ATTEMPTS"
    default = "1" if model_id == "quark" else "3"
    return max(1, min(5, int(os.environ.get(key, default))))


def text_fingerprint(value: str) -> str:
    """Stable exact-content identity, ignoring presentation-only whitespace."""
    normalized = "".join(str(value or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


class TaskControl(Exception):
    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def load_factory(spec: str) -> Callable[..., Any]:
    module_name, separator, attribute = str(spec or "").partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("factory must use package.module:function syntax")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError(f"factory is not callable: {spec}")
    return factory


def build_schedule(questions: list[str], rounds: int, mode: str) -> list[str]:
    if int(rounds) < 1:
        raise ValueError("rounds must be positive")
    clean = [str(item).strip() for item in questions if str(item).strip()]
    if mode == "sequential":
        return [question for question in clean for _ in range(int(rounds))]
    if mode != "interleaved":
        raise ValueError("question_mode must be interleaved or sequential")
    return [question for _ in range(int(rounds)) for question in clean]


def deterministic_request_id(task_id: str, model_id: str, round_number: int, question: str) -> str:
    identity = "\0".join((task_id, model_id, str(round_number), question))
    return "task-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


class LocalSpool:
    """Append-only local source of truth. All paths remain below its root."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._lock = threading.Lock()

    def append(self, task_id: str, payload: dict[str, Any]) -> Path:
        safe_task = "".join(char for char in task_id if char.isalnum() or char in "-_")[:80]
        if not safe_task:
            raise ValueError("invalid task id")
        target = (self.root / "results" / f"{safe_task}.jsonl").resolve()
        if self.root not in target.parents:
            raise ValueError("spool path escaped its root")
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock, target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return target

    def find_record(self, task_id: str, request_id: str) -> dict[str, Any] | None:
        safe_task = "".join(char for char in task_id if char.isalnum() or char in "-_")[:80]
        target = (self.root / "results" / f"{safe_task}.jsonl").resolve()
        if self.root not in target.parents or not target.exists():
            return None
        found: dict[str, Any] | None = None
        with self._lock, target.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if value.get("request_id") != request_id:
                    continue
                if value.get("submitted") is True:
                    found = None
                elif isinstance(value.get("record"), dict):
                    found = dict(value["record"])
        return found

    def mark_submitted(self, task_id: str, request_id: str) -> Path:
        """Append an acknowledgement so a later admin deletion triggers recollection."""
        return self.append(task_id, {
            "request_id": str(request_id),
            "submitted": True,
            "submitted_at": time.time(),
        })


class WorkerRunner:
    def __init__(
        self,
        client: ServerClient,
        worker_id: str,
        collector_factory: Callable[[str], Collector],
        analyzer: Analyzer,
        spool: LocalSpool,
    ):
        self.client = client
        self.worker_id = worker_id
        self.collector_factory = collector_factory
        self.analyzer = analyzer
        self.spool = spool
        self._collectors: dict[str, Collector] = {}
        self._collector_lock = threading.Lock()
        self._analysis_slots = threading.BoundedSemaphore(
            max(1, min(6, int(os.environ.get("GEO_ANALYSIS_CONCURRENCY", "3"))))
        )
        self._readiness_cache: dict[str, Any] = {}
        self._readiness_expires = 0.0
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_last: dict[tuple[str, str], float] = {}
        self._heartbeat_success: dict[tuple[str, str], float] = {}
        self._heartbeat_state: dict[tuple[str, str], dict[str, Any]] = {}
        self._provider_last_completed: dict[str, float] = {}

    def _collector(self, model_id: str) -> Collector:
        with self._collector_lock:
            collector = self._collectors.get(model_id)
            if collector is None:
                collector = self.collector_factory(model_id)
                self._collectors[model_id] = collector
            return collector

    def close(self) -> None:
        with self._collector_lock:
            collectors = list(self._collectors.values())
            self._collectors.clear()
        for collector in collectors:
            try:
                collector.close()
            except Exception:
                pass

    def readiness(self) -> dict[str, Any]:
        moment = time.monotonic()
        if self._readiness_cache and moment < self._readiness_expires:
            return {model: dict(value) for model, value in self._readiness_cache.items()}
        output: dict[str, Any] = {}
        for model_id in MODEL_ORDER:
            try:
                collector = self._collector(model_id)
                raw = collector.check_ready()
                ready = bool(raw.get("ready", raw.get("ok", False)))
                output[model_id] = {
                    "ready": ready,
                    "message": str(raw.get("message") or ("ready" if ready else "not ready"))[:160],
                }
            except Exception as exc:
                output[model_id] = {"ready": False, "message": f"{type(exc).__name__}: {exc}"[:160]}
        self._readiness_cache = {model: dict(value) for model, value in output.items()}
        self._readiness_expires = moment + max(10, int(os.environ.get("GEO_READINESS_TTL", "30")))
        return output

    def run_login(self, login: dict[str, Any]) -> None:
        login_id = str(login["id"])
        lease = str(login["lease_token"])
        model = str(login.get("model_id") or "").strip().casefold()
        collector = None

        def progress(message: str) -> None:
            self.client.login_heartbeat(login_id, lease, str(message)[:500])

        try:
            from web_collectors.browser import cookie_env_name
            from web_collectors.collector import BrowserCollector
            from windows_enterprise_worker.session_store import save_session

            if model not in {"deepseek", "kimi"}:
                raise ValueError(f"{model} 不使用网页 Cookie 登录流程")
            os.environ.pop(cookie_env_name(model), None)
            progress(f"正在本机打开 {model} 登录窗口")
            collector = BrowserCollector(model, headless=False)
            cookies = collector.wait_for_login_cookies(timeout=600, progress=progress)
            if not cookies:
                raise RuntimeError("登录完成后未检测到有效会话")
            storage = collector.storage_state()
            os.environ[cookie_env_name(model)] = json.dumps(
                cookies, ensure_ascii=False, separators=(",", ":")
            )
            save_session(model, cookies, storage)
            self._readiness_expires = 0.0
            self.client.finish_login(login_id, lease, "ready")
        except BaseException as exc:
            self.client.finish_login(
                login_id, lease, "failed", f"{type(exc).__name__}: {exc}"
            )
        finally:
            if collector is not None:
                collector.close()

    def _check_control(
        self, task_id: str, lease: str, model_id: str, question: str, message: str,
        *, force: bool = False,
    ) -> None:
        key = (task_id, model_id)
        moment = time.monotonic()
        interval = max(2.0, float(os.environ.get("GEO_HEARTBEAT_MIN_INTERVAL", "4")))
        with self._heartbeat_lock:
            previous = self._heartbeat_last.get(key, 0.0)
            if not force and moment - previous < interval:
                state = dict(self._heartbeat_state.get(key) or {})
            else:
                self._heartbeat_last[key] = moment
                state = {}
        if not state:
            try:
                state = self.client.heartbeat(
                    task_id, lease, model=model_id, question=question, message=message
                )
            except (ConnectionError, TimeoutError, OSError):
                with self._heartbeat_lock:
                    last_success = self._heartbeat_success.get(key, 0.0)
                    state = dict(self._heartbeat_state.get(key) or {})
                if not state or moment - last_success > 120:
                    raise
            else:
                with self._heartbeat_lock:
                    self._heartbeat_success[key] = time.monotonic()
                    self._heartbeat_state[key] = dict(state)
        if state.get("cancel_requested"):
            raise TaskControl("cancelled")
        if state.get("pause_requested"):
            raise TaskControl("paused")

    def _run_model(self, task: dict[str, Any], model_id: str, stopped: threading.Event) -> None:
        task_id = str(task["id"])
        lease = str(task["lease_token"])
        from monitor_core.remote_tasks import configured_model_rounds
        rounds = configured_model_rounds(task, model_id)
        schedule = build_schedule(
            [str(item) for item in task.get("questions") or []],
            rounds,
            str(task.get("question_mode") or "interleaved"),
        )
        completed = {
            int(item) for item in (task.get("completed_rounds") or {}).get(model_id, [])
        }
        persisted_fingerprints = task.get("answer_fingerprints") or {}
        model_fingerprints = persisted_fingerprints.get(model_id) or {}
        seen_answers: dict[str, set[str]] = {
            str(question_key): {str(value) for value in values or [] if value}
            for question_key, values in model_fingerprints.items()
            if isinstance(values, list)
        } if isinstance(model_fingerprints, dict) else {}
        persisted_captures = task.get("capture_fingerprints") or {}
        model_captures = persisted_captures.get(model_id) or {}
        seen_capture_ids: dict[str, dict[str, set[str]]] = {
            str(question_key): {
                str(answer_key): {str(value) for value in values or [] if value}
                for answer_key, values in answers.items() if isinstance(values, list)
            }
            for question_key, answers in model_captures.items()
            if isinstance(answers, dict)
        } if isinstance(model_captures, dict) else {}
        collector = self._collector(model_id)
        task_kind = str(task.get("task_kind") or "")
        prepare = getattr(collector, "prepare_diagnosis", None)
        if callable(prepare):
            if task_kind == "diagnosis":
                prepare(sum(1 for number in range(1, len(schedule) + 1) if number not in completed))
            else:
                # A collector instance is reused across tasks. Reset any burst
                # state left by a fast diagnosis: paid monitoring is strictly
                # ask one round, collect one round, persist, then continue.
                prepare(1)
        # claim() was sent with a fresh readiness snapshot. Re-running every
        # browser probe here launches three throwaway Chrome processes, delays
        # the slow emulator/Kimi paths and creates an avoidable memory spike.
        ready = self._readiness_cache.get(model_id) or collector.check_ready()
        if not bool(ready.get("ready", ready.get("ok", False))):
            raise RuntimeError(str(ready.get("message") or f"{model_id} is not ready"))

        def persist_capture(round_number: int, question: str, captured: Any) -> None:
            captured.validate()
            question_key = text_fingerprint(question)
            answer_key = text_fingerprint(captured.body)
            capture_key = text_fingerprint(
                getattr(captured, "capture_identity", "") or captured.page_url
            )
            prior_capture_keys = seen_capture_ids.setdefault(question_key, {}).setdefault(
                answer_key, set()
            )
            repeated_body = answer_key and answer_key in seen_answers.setdefault(question_key, set())
            independently_observed = bool(capture_key and capture_key not in prior_capture_keys)
            if repeated_body and not independently_observed:
                raise RuntimeError(
                    f"{model_id} 第 {round_number} 轮回答正文与该问题的前一轮完全相同，"
                    "疑似重复抓取旧会话，本轮作废并新建会话重试"
                )
            with self._analysis_slots:
                analysis = self.analyzer.analyze(
                    model_id,
                    question,
                    str(task.get("brand_name") or ""),
                    str(task.get("product_name") or ""),
                    captured,
                )
            request_id = deterministic_request_id(
                task_id, model_id, round_number, question
            )
            record = build_record(
                task=task,
                model_id=model_id,
                question=question,
                round_number=round_number,
                captured=captured,
                analysis=analysis,
            )
            self.spool.append(task_id, {"request_id": request_id, "record": record})
            self.client.submit_result(task_id, lease, model_id, request_id, record)
            self.spool.mark_submitted(task_id, request_id)
            if answer_key:
                seen_answers[question_key].add(answer_key)
                if capture_key:
                    prior_capture_keys.add(capture_key)
            completed.add(round_number)

        batched: dict[int, Any] = {}
        batch_collect = getattr(collector, "collect_batch", None)
        progressive_batch_collect = getattr(collector, "collect_batch_progressive", None)
        missing_rounds = [
            (number, question)
            for number, question in enumerate(schedule, 1)
            if number not in completed
            and self.spool.find_record(
                task_id, deterministic_request_id(task_id, model_id, number, question)
            ) is None
        ]
        if (task_kind == "diagnosis" and callable(batch_collect) and len(missing_rounds) > 1
                and bool(getattr(collector, "batch_collection_enabled", True))):
            first_question = missing_rounds[0][1]

            def batch_progress(message: str) -> None:
                self._check_control(
                    task_id, lease, model_id, first_question, str(message)[:500]
                )

            try:
                self._check_control(
                    task_id, lease, model_id, first_question,
                    f"{model_id} 正在并行启动 {len(missing_rounds)} 轮独立抽样",
                    force=True,
                )
                if callable(progressive_batch_collect):
                    question_by_round = dict(missing_rounds)

                    def publish_result(round_number: int, captured: Any) -> None:
                        if round_number in completed:
                            return
                        persist_capture(
                            round_number, question_by_round[round_number], captured
                        )

                    batched = dict(progressive_batch_collect(
                        missing_rounds, batch_progress, publish_result
                    ) or {})
                else:
                    batched = dict(batch_collect(missing_rounds, batch_progress) or {})
                if set(batched) != {number for number, _ in missing_rounds}:
                    raise RuntimeError("批量采集返回轮次不完整")
                for captured in batched.values():
                    captured.validate()
            except TaskControl:
                raise
            except BaseException as exc:
                batched.clear()
                # A progressive collector may already have published part of
                # its burst before the child failed.  Its instance is reused
                # by the reliable fallback below, so reset burst state before
                # asking only the genuinely missing round.  Without this,
                # Doubao keeps its original batch size (for example 3) and
                # needlessly asks three new conversations to fill one gap.
                if callable(prepare):
                    prepare(1)
                if model_id == "kimi":
                    # A rejected burst must enter the same cooldown as a
                    # completed Kimi round before the reliable fallback tries
                    # again; otherwise fallback itself can amplify rate limits.
                    self._provider_last_completed[model_id] = time.monotonic()
                self._check_control(
                    task_id, lease, model_id, first_question,
                    f"{model_id} 批量回收未完整，已自动切换可靠逐轮模式：{type(exc).__name__}",
                )
        try:
            for round_number, question in enumerate(schedule, 1):
                if stopped.is_set():
                    return
                if round_number in completed:
                    continue
                # Kimi applies a tighter conversational rate limit than the
                # other providers.  A short gap between its two diagnosis
                # rounds prevents the provider's "有点累了" response without
                # slowing the other five models, which continue in parallel.
                if model_id == "kimi" and round_number not in batched:
                    safe_gap = max(
                        30, int(os.environ.get("GEO_KIMI_ROUND_GAP_SECONDS", "60"))
                    )
                    elapsed = time.monotonic() - self._provider_last_completed.get(model_id, 0.0)
                    if elapsed < safe_gap:
                        time.sleep(safe_gap - elapsed)
                request_id = deterministic_request_id(task_id, model_id, round_number, question)
                pending = self.spool.find_record(task_id, request_id)
                if pending is not None:
                    self._check_control(
                        task_id, lease, model_id, question,
                        f"{model_id} round {round_number}/{len(schedule)} replaying local result",
                    )
                    self.client.submit_result(task_id, lease, model_id, request_id, pending)
                    self.spool.mark_submitted(task_id, request_id)
                    continue
                self._check_control(
                    task_id, lease, model_id, question,
                    f"{model_id} round {round_number}/{len(schedule)} starting a new conversation",
                    force=True,
                )

                def progress(message: str) -> None:
                    self._check_control(task_id, lease, model_id, question, str(message)[:500])

                attempts = round_attempts(model_id)
                last_error: BaseException | None = None
                for attempt in range(1, attempts + 1):
                    try:
                        captured = batched.pop(round_number, None)
                        if captured is None:
                            captured = collector.collect_new_conversation(question, round_number, progress)
                        if model_id == "kimi":
                            self._provider_last_completed[model_id] = time.monotonic()
                        persist_capture(round_number, question, captured)
                        break
                    except TaskControl:
                        raise
                    except BaseException as exc:
                        last_error = exc
                        if attempt >= attempts:
                            raise
                        self._check_control(
                            task_id, lease, model_id, question,
                            f"{model_id} 第 {round_number} 轮暂时未完成，正在自动恢复（{attempt}/{attempts}）",
                        )
                        retry_delay = min(20, 5 * attempt)
                        if model_id == "kimi":
                            retry_delay = max(
                                retry_delay,
                                int(os.environ.get("GEO_KIMI_RETRY_SECONDS", "60")),
                            )
                        time.sleep(retry_delay)
                else:  # pragma: no cover - defensive; the loop either succeeds or raises.
                    raise RuntimeError(str(last_error or "采集恢复失败"))
        except Exception:
            # A provider failure is isolated to that provider. Other model
            # futures must be allowed to finish and persist their checkpoints;
            # the task is then re-queued to collect only the missing rounds.
            raise

    def run_task(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        lease = str(task["lease_token"])
        selected = [model for model in MODEL_ORDER if model in set(task.get("models") or [])]
        unsupported = sorted(set(task.get("models") or []) - set(MODEL_ORDER))
        if unsupported or not selected:
            reason = "unsupported models: " + ", ".join(unsupported or ["none"])
            self.client.finish(task_id, lease, "failed", reason)
            return
        stopped = threading.Event()
        errors: list[BaseException] = []
        concurrency = max(1, min(len(selected), int(os.environ.get("GEO_MODEL_CONCURRENCY", "3"))))
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="geo-model") as pool:
            priority = [model for model in EXECUTION_PRIORITY if model in selected]
            futures = [pool.submit(self._run_model, task, model, stopped) for model in priority]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
                    if isinstance(exc, TaskControl):
                        stopped.set()
        controls = [exc for exc in errors if isinstance(exc, TaskControl)]
        if controls:
            self.client.finish(task_id, lease, controls[0].status)
        elif errors:
            summary = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            self.client.finish(task_id, lease, "retrying", summary)
        else:
            self.client.finish(task_id, lease, "completed")

    def poll(self, *, once: bool = False, poll_seconds: float = 5.0) -> None:
        failures = 0
        while True:
            try:
                response = self.client.claim(self.worker_id, self.readiness())
                failures = 0
                login = response.get("login")
                task = response.get("task")
                if login:
                    self.run_login(login)
                elif task:
                    self.run_task(task)
                if once:
                    return
                time.sleep(max(2.0, float(poll_seconds)))
            except (ConnectionError, TimeoutError, OSError, ServerError) as exc:
                if once:
                    raise
                failures += 1
                delay = min(60.0, max(2.0, float(poll_seconds)) * (2 ** min(failures - 1, 5)))
                print(
                    f"worker connection retry in {delay:.0f}s: {type(exc).__name__}: {str(exc)[:500]}",
                    flush=True,
                )
                time.sleep(delay)
