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
from .protocol import ServerClient


MODEL_ORDER = ("doubao", "yuanbao", "wenxin")


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
                if value.get("request_id") == request_id and isinstance(value.get("record"), dict):
                    found = dict(value["record"])
        return found


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
        self._analysis_lock = threading.Lock()

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
        return output

    def _check_control(
        self, task_id: str, lease: str, model_id: str, question: str, message: str
    ) -> None:
        state = self.client.heartbeat(
            task_id, lease, model=model_id, question=question, message=message
        )
        if state.get("cancel_requested"):
            raise TaskControl("cancelled")
        if state.get("pause_requested"):
            raise TaskControl("paused")

    def _run_model(self, task: dict[str, Any], model_id: str, stopped: threading.Event) -> None:
        task_id = str(task["id"])
        lease = str(task["lease_token"])
        schedule = build_schedule(
            [str(item) for item in task.get("questions") or []],
            int(task.get("rounds") or 1),
            str(task.get("question_mode") or "interleaved"),
        )
        completed = {
            int(item) for item in (task.get("completed_rounds") or {}).get(model_id, [])
        }
        collector = self._collector(model_id)
        ready = collector.check_ready()
        if not bool(ready.get("ready", ready.get("ok", False))):
            raise RuntimeError(str(ready.get("message") or f"{model_id} is not ready"))
        try:
            for round_number, question in enumerate(schedule, 1):
                if stopped.is_set():
                    return
                if round_number in completed:
                    continue
                request_id = deterministic_request_id(task_id, model_id, round_number, question)
                pending = self.spool.find_record(task_id, request_id)
                if pending is not None:
                    self._check_control(
                        task_id, lease, model_id, question,
                        f"{model_id} round {round_number}/{len(schedule)} replaying local result",
                    )
                    self.client.submit_result(task_id, lease, model_id, request_id, pending)
                    continue
                self._check_control(
                    task_id, lease, model_id, question,
                    f"{model_id} round {round_number}/{len(schedule)} starting a new conversation",
                )

                def progress(message: str) -> None:
                    self._check_control(task_id, lease, model_id, question, str(message)[:500])

                captured = collector.collect_new_conversation(question, round_number, progress)
                captured.validate()
                with self._analysis_lock:
                    analysis = self.analyzer.analyze(
                        model_id,
                        question,
                        str(task.get("brand_name") or ""),
                        str(task.get("product_name") or ""),
                        captured,
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
        except Exception:
            stopped.set()
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
        with ThreadPoolExecutor(max_workers=max(1, len(selected)), thread_name_prefix="geo-model") as pool:
            futures = [pool.submit(self._run_model, task, model, stopped) for model in selected]
            for future in as_completed(futures):
                try:
                    future.result()
                except BaseException as exc:
                    errors.append(exc)
        controls = [exc for exc in errors if isinstance(exc, TaskControl)]
        if controls:
            self.client.finish(task_id, lease, controls[0].status)
        elif errors:
            summary = "; ".join(f"{type(exc).__name__}: {exc}" for exc in errors)
            self.client.finish(task_id, lease, "failed", summary)
        else:
            self.client.finish(task_id, lease, "completed")

    def poll(self, *, once: bool = False, poll_seconds: float = 5.0) -> None:
        while True:
            response = self.client.claim(self.worker_id, self.readiness())
            task = response.get("task")
            if task:
                self.run_task(task)
            if once:
                return
            time.sleep(max(2.0, float(poll_seconds)))
