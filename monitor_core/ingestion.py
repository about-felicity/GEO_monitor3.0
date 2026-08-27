"""Normalize live callbacks directly into PostgreSQL analytics records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any

from monitor_core.analytics import beijing_day, normalize_source
from monitor_core.recommendation_questions import canonical_recommendation_question


BEIJING = timezone(timedelta(hours=8))


def _question(value: Any) -> str:
    text = str(value or "").strip()
    return canonical_recommendation_question(text) or text or "未知问题"


def normalize_remote_record(model_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Convert a Wenxin/Yuanbao/DeepSeek/Afu callback to one query-ready run."""
    finished = str(record.get("finished_at") or record.get("started_at") or "")
    answer = str(record.get("web_body") or record.get("reply") or "")
    if model_id == "yuanbao":
        stable = "\0".join((
            str(record.get("serial") or ""), str(record.get("round") or ""),
            str(record.get("started_at") or ""), str(record.get("finished_at") or ""),
            str(record.get("question") or ""),
            str(record.get("web_body") or record.get("reply") or ""),
        ))
    elif model_id == "deepseek":
        stable = "\0".join(str(record.get(key) or "") for key in
                            ("round", "question", "started_at", "finished_at"))
    else:
        stable = "\0".join(str(record.get(key) or "") for key in
                            ("serial", "task_id", "round", "prompt", "started_at", "finished_at"))
    run_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]
    normalized_sources = []
    seen_urls: set[str] = set()
    for raw in (record.get("sources") or []):
        if not isinstance(raw, dict):
            continue
        source = normalize_source(raw)
        key = str(source.get("canonical_url") or source.get("url") or "").strip()
        if key and key in seen_urls:
            continue
        if key:
            seen_urls.add(key)
        normalized_sources.append(source)
    return {
        "model_id": model_id,
        "run_id": run_id,
        "question": _question(record.get("question") or record.get("prompt")),
        "finished_at": finished,
        "day": str(record.get("day") or beijing_day(finished)),
        "serial": str(record.get("serial") or record.get("remote_source_device") or model_id),
        "answer": answer,
        "status": str(record.get("status") or "success"),
        "task_id": int(record.get("task_id") or 1),
        "capture_mode": str(record.get("capture_mode") or ""),
        "capture_label": str(record.get("capture_label") or (
            "搜索卡片" if record.get("capture_mode") == "baidu_search_ai"
            else "文心页面兜底" if record.get("capture_mode") == "baidu_wenxin_search"
            else ""
        )),
        "body_capture_complete": bool(record.get("body_capture_complete", True)),
        "sources": normalized_sources,
        # Collector counts may include the same article twice through tracking
        # and canonical URLs. Analytics intentionally counts unique links.
        "expected_source_count": len(normalized_sources),
        "source_capture_complete": bool(record.get("source_capture_complete", True)),
        "products": [dict(item) for item in (record.get("products") or []) if isinstance(item, dict)],
        "brands": [str(item).strip() for item in (record.get("brands") or []) if str(item).strip()],
        "product_review_status": str(record.get("product_review_status") or ""),
        "product_analysis_model": str(record.get("product_analysis_model") or ""),
        "product_extraction_method": str(record.get("product_extraction_method") or ""),
    }


def normalize_doubao_payload(payload: dict[str, Any], products: list[dict[str, Any]],
                             review_status: str = "", model: str = "",
                             method: str = "") -> dict[str, Any]:
    """Convert a Doubao browser payload without any CSV intermediate."""
    finished = str(
        payload.get("captured_at") or payload.get("capturedAt")
        or payload.get("extractedAt") or datetime.now(BEIJING).isoformat(timespec="seconds")
    )
    normalized_products = []
    brands = []
    for index, raw in enumerate(products, 1):
        item = dict(raw)
        brand = str(item.get("brand") or item.get("brand_name") or "").strip()
        product = str(item.get("product_name") or item.get("name") or "").strip()
        item.update({"brand": brand, "brand_name": brand,
                     "product_name": product, "rank": int(item.get("rank") or index)})
        if brand and brand not in brands:
            brands.append(brand)
        if brand or product:
            normalized_products.append(item)
    return {
        "model_id": "doubao",
        "question": _question(payload.get("question") or payload.get("chatTitle")),
        "finished_at": finished,
        "day": beijing_day(finished),
        "serial": str(payload.get("source_device") or payload.get("mumu_serial") or "doubao-web"),
        "answer": str(payload.get("answerText") or payload.get("answer_text") or ""),
        "status": "success",
        "sources": [normalize_source(item) for item in (payload.get("items") or []) if isinstance(item, dict)],
        "products": normalized_products,
        "brands": brands,
        "product_review_status": review_status,
        "product_analysis_model": model,
        "product_extraction_method": method,
        "source_capture_complete": bool(payload.get("complete")),
        "expected_source_count": int(payload.get("expectedCount") or payload.get("count") or 0),
        "page_url": str(payload.get("url") or ""),
    }
