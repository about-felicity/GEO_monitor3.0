from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windows_enterprise_worker.analyzer import DeepSeekAnalyzer
from windows_worker_sdk.contracts import CapturedAnswer, build_record
from windows_worker_sdk.runner import LocalSpool, deterministic_request_id


TASK_ID = "07d16961f9dd483588a334b3f1bec603"
QUESTION = "推荐一款临沂LED显示屏"


def main() -> int:
    manual_id = (ROOT / "runtime" / "bydz_quark_manual_task.txt").read_text(
        encoding="utf-8-sig"
    ).strip()
    with urllib.request.urlopen(
        f"http://127.0.0.1:8765/api/tasks/{manual_id}", timeout=10
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    source = payload.get("task", {}).get("result") or {}
    if payload.get("task", {}).get("state") != "completed" or source.get("status") != "success":
        raise RuntimeError("千问等义问题尚未采集成功")

    captured = CapturedAnswer(
        body=str(source.get("web_body") or source.get("reply") or ""),
        sources=list(source.get("sources") or []),
        page_url=str(source.get("page_url") or ""),
        expected_source_count=int(source.get("expected_source_count") or 0),
        body_capture_complete=True,
        source_capture_complete=bool(source.get("source_capture_complete")),
        body_capture_origin="browser_dom",
        source_capture_origins={"browser": len(source.get("sources") or [])},
        capture_mode="quark_extension_semantic_variant_capture",
        started_at=str(source.get("started_at") or ""),
        finished_at=str(source.get("finished_at") or ""),
    )
    captured.validate()
    analysis = DeepSeekAnalyzer().analyze(
        "quark", QUESTION, "百一电子", "临沂LED显示屏", captured
    )
    task = {
        "id": TASK_ID,
        "customer_slug": "bydz",
        "brand_name": "百一电子",
        "product_name": "临沂LED显示屏",
        "task_kind": "case_study",
    }
    record = build_record(
        task=task,
        model_id="quark",
        question=QUESTION,
        round_number=10,
        captured=captured,
        analysis=analysis,
    )
    record["query_variant"] = str(source.get("prompt") or "")
    request_id = deterministic_request_id(TASK_ID, "quark", 10, QUESTION)
    spool = LocalSpool(ROOT / "runtime" / "worker_spool")
    if spool.find_record(TASK_ID, request_id) is not None:
        raise RuntimeError("千问第 10 轮已存在，拒绝重复写入")
    spool.append(TASK_ID, {"request_id": request_id, "record": record})
    print(json.dumps({"request_id": request_id, "record": record}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
