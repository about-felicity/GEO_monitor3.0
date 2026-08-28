from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import signal
import time
from datetime import datetime
from pathlib import Path

from monitor_core.quality import answer_quality_reason
from monitor_core.scheduling import build_question_schedule

from .collector import create_collector
from .config import SITES


STOP = False


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_questions(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def persist_database(model_id: str, record: dict, log: logging.Logger) -> None:
    """Mirror a completed local web round into the shared analytics database."""
    from monitor_core import database
    if not database.enabled():
        return
    from monitor_core.ingestion import normalize_remote_record
    identity = "\0".join((model_id, str(record.get("started_at") or ""), str(record.get("round") or ""), str(record.get("question") or "")))
    request_id = "web-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    envelope = {"source_device": f"{model_id}-web", "received_at": str(record.get("finished_at") or ""), "transport": "local_web"}
    try:
        outcome = database.store_ingested_run(model_id, request_id, record, envelope, normalize_remote_record(model_id, record))
        if outcome.get("quarantined"):
            log.warning("本轮已保留原始记录，但未进入统计：%s", outcome.get("quarantine_reason"))
    except Exception:
        # JSONL remains the local source of truth. A temporary database outage
        # must not trigger another question and create a duplicate observation.
        log.exception("结果已写入 JSONL，但同步 PostgreSQL 失败")


def main() -> int:
    parser = argparse.ArgumentParser(description="五模型统一无头网页采集器")
    parser.add_argument("--model", required=True, choices=tuple(SITES))
    parser.add_argument("--questions-file", type=Path, required=True)
    parser.add_argument("--rounds-per-question", type=int, default=1)
    parser.add_argument("--question-mode", choices=("interleaved", "sequential"), default="interleaved")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--stable-seconds", type=int, default=6)
    parser.add_argument("--min-interval", type=float, default=30)
    parser.add_argument("--max-interval", type=float, default=60)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.rounds_per_question < 1:
        raise SystemExit("--rounds-per-question 必须大于 0")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger(f"web.{args.model}")
    questions = load_questions(args.questions_file)
    if not questions:
        raise SystemExit("问题列表为空")
    schedule = build_question_schedule(questions, args.rounds_per_question, args.question_mode)
    try:
        state = json.loads(args.state.read_text(encoding="utf-8")) if args.resume else {}
    except (OSError, ValueError, json.JSONDecodeError):
        state = {}
    index = max(0, int(state.get("next_index") or 0))

    active_collector: list[object] = []

    def stop(*_args) -> None:
        global STOP
        STOP = True
        if active_collector:
            try:
                active_collector[0].close()
            except Exception:
                pass
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    collector = create_collector(args.model, headless=True)
    active_collector.append(collector)
    try:
        while not STOP and index < len(schedule):
            question = schedule[index]
            started = now()
            log.info("第 %d/%d 轮：%s", index + 1, len(schedule), question)
            try:
                result = collector.collect(question, timeout=args.timeout, stable_seconds=args.stable_seconds)
                body = str(result.get("body") or result.get("page_body") or "").strip()
                reason = answer_quality_reason(question, body)
                if reason:
                    raise RuntimeError(f"回答质量校验失败：{reason}")
                sources = [dict(item) for item in result.get("sources") or [] if isinstance(item, dict)]
                record = {
                    "collector_model": args.model,
                    "model_id": args.model,
                    "serial": f"{args.model}-web",
                    "round": index + 1,
                    "question": question,
                    "prompt": question,
                    "reply": body,
                    "web_body": body,
                    "sources": sources,
                    "status": "success",
                    "started_at": started,
                    "finished_at": now(),
                    "capture_mode": str(result.get("capture_mode") or "headless_web"),
                    "capture_label": (
                        "Scrapling 隐身浏览器网页直采"
                        if str(result.get("capture_mode") or "").startswith("scrapling")
                        else "隐身无头浏览器网页直采"
                    ),
                    "body_capture_complete": True,
                    "expected_source_count": int(result.get("expected_source_count") or len(sources)),
                    "source_capture_complete": bool(result.get("source_capture_complete", True)),
                    "page_url": str(result.get("url") or ""),
                }
                append_jsonl(args.results, record)
                persist_database(args.model, record, log)
                index += 1
                save_state(args.state, {"next_index": index, "updated_at": now()})
                log.info("采集完成：正文 %d 字，信源 %d 条", len(body), len(sources))
            except Exception as exc:
                log.exception("网页采集失败：%s", exc)
                save_state(args.state, {"next_index": index, "updated_at": now(), "last_error": str(exc)})
                if STOP:
                    break
                time.sleep(min(60, max(10, args.min_interval)))
                continue
            if index < len(schedule) and not STOP:
                time.sleep(random.uniform(max(1, args.min_interval), max(args.min_interval, args.max_interval)))
    finally:
        collector.close()
        active_collector.clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
