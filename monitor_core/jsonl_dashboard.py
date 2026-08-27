from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from monitor_core.analytics import canonical_url, media_name, source_type
from monitor_core.recommendation_questions import canonical_recommendation_question
from monitor_core.quality import answer_quality_reason


BEIJING = timezone(timedelta(hours=8))


def _day(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed.astimezone(BEIJING).date().isoformat()
    except ValueError:
        return ""


def _counted(values) -> list[dict]:
    return [{"name": name, "count": count} for name, count in Counter(values).most_common()]


def build_jsonl_dashboard(model_id: str, results: Path, output: Path) -> dict:
    records = []
    if results.exists():
        for line in results.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    runs, seen, quarantine = [], set(), []
    for record in records:
        declared_model = str(record.get("collector_model") or record.get("model_id") or "").strip()
        if declared_model and declared_model != model_id:
            quarantine.append({"round": record.get("round"), "question": record.get("question"),
                               "reason": f"模型标识不匹配：{declared_model}"})
            continue
        if record.get("status") != "success":
            continue
        question = canonical_recommendation_question(record.get("question") or record.get("prompt"))
        if not question:
            continue
        answer = str(record.get("reply") or record.get("web_body") or "")
        quality_reason = answer_quality_reason(question, answer)
        if quality_reason:
            quarantine.append({"round": record.get("round"), "question": question, "reason": quality_reason})
            continue
        stable = "\0".join(str(record.get(key) or "") for key in ("round", "prompt", "started_at", "finished_at"))
        run_id = hashlib.sha256(stable.encode()).hexdigest()[:20]
        if run_id in seen:
            continue
        seen.add(run_id)
        sources, source_seen = [], set()
        for raw in record.get("sources") or []:
            url = str(raw.get("url") or raw.get("href") or "").strip()
            normalized = canonical_url(url)
            if not normalized or normalized in source_seen:
                continue
            source_seen.add(normalized)
            domain = urlparse(url).netloc.casefold().removeprefix("www.")
            title = str(raw.get("title") or "").strip()
            kind = str(raw.get("type") or raw.get("source_type") or "").strip() or source_type(domain, title)
            sources.append({"title": title, "url": url, "canonical_url": normalized,
                            "domain": domain,
                            "media": str(raw.get("media") or "").strip() or media_name(domain),
                            "type": kind})
        runs.append({"run_id": run_id, "sequence": len(runs) + 1, "round": int(record.get("round") or 0),
                     "serial": str(record.get("serial") or model_id), "question": question,
                     "reply": answer, "web_body": str(record.get("web_body") or answer),
                     "started_at": str(record.get("started_at") or ""),
                     "finished_at": str(record.get("finished_at") or ""),
                     "day": _day(record.get("finished_at") or record.get("started_at") or ""),
                     "status": "success", "sources": sources,
                     "brands": [str(item).strip() for item in (record.get("brands") or []) if str(item).strip()],
                     "products": [dict(item) for item in (record.get("products") or []) if isinstance(item, dict)]})
    all_sources = [source for run in runs for source in run["sources"]]
    questions = []
    for question in dict.fromkeys(run["question"] for run in runs):
        selected = [run for run in runs if run["question"] == question]
        refs = [source for run in selected for source in run["sources"]]
        questions.append({"question": question, "runs": len(selected), "sources": len(refs),
                          "unique_sources": len({item["canonical_url"] for item in refs})})
    devices = []
    for serial in dict.fromkeys(run["serial"] for run in runs):
        selected = [run for run in runs if run["serial"] == serial]
        devices.append({"serial": serial, "runs": len(selected),
                        "sources": sum(len(run["sources"]) for run in selected),
                        "latest": max((run["finished_at"] for run in selected), default="")})
    daily = []
    for date in sorted({run["day"] for run in runs if run["day"]}, reverse=True):
        selected = [run for run in runs if run["day"] == date]
        refs = [source for run in selected for source in run["sources"]]
        daily.append({"date": date, "runs": len(selected), "successful_runs": len(selected),
                      "sources": len(refs), "unique_sources": len({item["canonical_url"] for item in refs}),
                      "question_count": len({run["question"] for run in selected}),
                      "device_count": len({run["serial"] for run in selected}), "product_mentions": 0,
                      "brands": [], "media": _counted(item["media"] for item in refs),
                      "types": _counted(item["type"] for item in refs),
                      "questions": _counted(run["question"] for run in selected)})
    payload = {"generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
               "total_runs": len(records), "successful_runs": len(runs), "total_sources": len(all_sources),
               "unique_sources": len({item["canonical_url"] for item in all_sources}),
               "question_count": len(questions), "device_count": len(devices), "questions": questions,
               "devices": devices, "runs": runs, "daily": daily,
               "top_media": _counted(item["media"] for item in all_sources),
               "source_types": _counted(item["type"] for item in all_sources), "brands": [], "products": [],
               "quality_quarantine": {"count": len(quarantine), "records": quarantine}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
