from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import save_doubao_refs as saver


ROOT = Path(__file__).resolve().parents[2]
MODELS = ("doubao", "yuanbao", "wenxin", "deepseek", "quark")
SUSPICIOUS_TERMS = (
    "博士园",
    "检测服务",
    "养发机构",
    "星伊睫",
    "睫毛书",
    "麦穗睫毛",
    "语速大灯泡",
    "按压头",
    "分馏椰子油",
)
KNOWN_GROUNDED_PRODUCTS = (
    ("泊泉雅", "泊泉雅维生素补水保湿面膜"),
)


def result_path(model: str) -> Path:
    return ROOT / "runtime" / "web_results" / f"{model}_results.jsonl"


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def products_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    products = row.get("products")
    return products if isinstance(products, list) else []


def needs_review(row: dict[str, Any]) -> bool:
    if not row.get("remote_request_id"):
        return False
    products = products_of(row)
    if not products:
        return True
    appearance_ranks = [
        item.get("rank") for item in products
        if isinstance(item, dict) and item.get("rank_type") == "appearance_order"
    ]
    if appearance_ranks != list(range(1, len(appearance_ranks) + 1)):
        return True
    product_text = "\n".join(
        str(item.get("product_name") or "") + " " + str(item.get("evidence") or "")
        for item in products if isinstance(item, dict)
    )
    return any(term in product_text for term in SUSPICIOUS_TERMS)


def normalize_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    appearance_rank = 0
    normalized = []
    for item in products:
        if not isinstance(item, dict):
            continue
        product = dict(item)
        brand = str(product.get("brand_name") or "").strip()
        if brand in {"未知", "unknown", "Unknown"}:
            brand = ""
        product["brand_name"] = brand
        product["brand_identified"] = bool(brand)
        if product.get("rank_type") == "appearance_order":
            appearance_rank += 1
            product["rank"] = appearance_rank
        normalized.append(product)
    return normalized


def recover_known_grounded_products(answer: str) -> list[dict[str, Any]]:
    products = []
    for brand, product_name in KNOWN_GROUNDED_PRODUCTS:
        if product_name not in answer:
            continue
        products.append({
            "product_name": product_name,
            "brand_name": brand,
            "evidence": product_name,
            "rank": len(products) + 1,
            "rank_type": "appearance_order",
        })
    return products


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    # The LAN receiver may append while a batch is being reviewed. Merge any
    # callbacks that arrived after the initial read before replacing the file.
    known = {str(row.get("remote_request_id") or "") for row in rows}
    for current in load_rows(path):
        request_id = str(current.get("remote_request_id") or "")
        if request_id and request_id not in known:
            rows.append(current)
            known.add(request_id)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def repair_model(
    model: str,
    *,
    dry_run: bool,
    limit: int = 0,
    pending_only: bool = False,
    backup: bool = True,
    exclude_request_ids: set[str] | None = None,
) -> dict[str, Any]:
    path = result_path(model)
    rows = load_rows(path)
    excluded = exclude_request_ids or set()
    candidate_indexes = [
        index for index, row in enumerate(rows)
        if (
            str(row.get("product_review_status") or "") == "ai_pending"
            if pending_only else needs_review(row)
        )
        and str(row.get("remote_request_id") or "") not in excluded
    ]
    candidates = len(candidate_indexes)
    if dry_run:
        return {"rows": len(rows), "candidates": candidates, "reviewed": 0, "removed_duplicates": 0}

    # New callbacks should become visible first. A bounded batch is written
    # atomically after every invocation, so a reboot never loses a long run.
    selected_indexes = set(reversed(candidate_indexes))
    if limit > 0:
        selected_indexes = set(list(reversed(candidate_indexes))[:limit])
    if not selected_indexes:
        return {"rows": len(rows), "candidates": candidates, "reviewed": 0, "removed_duplicates": 0}

    if backup:
        backup_root = ROOT / "runtime" / "analysis_backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_root / path.name)

    reviewed = 0
    attempted_ids: list[str] = []
    failed_ids: list[str] = []
    seen_requests = set()
    repaired_rows = []
    removed_duplicates = 0
    for index, row in enumerate(rows):
        request_id = str(row.get("remote_request_id") or "")
        if request_id and request_id in seen_requests:
            removed_duplicates += 1
            continue
        if request_id:
            seen_requests.add(request_id)
        if index in selected_indexes:
            request_id_for_retry = str(row.get("remote_request_id") or f"{model}:{index}")
            attempted_ids.append(request_id_for_retry)
            question = str(row.get("question") or row.get("prompt") or "").strip()
            answer = str(row.get("web_body") or row.get("reply") or row.get("answer") or "").strip()
            products, status, method, analysis_model = saver.review_products_with_ai(answer, question)
            if status == "ai_pending":
                recovered = recover_known_grounded_products(answer)
                if recovered:
                    products = recovered
                    status = "ai_verified"
                    method = "grounded_repair"
                    analysis_model = ""
            if status == "ai_verified":
                row["products"] = products
                row["product_review_status"] = status
                row["product_extraction_method"] = method
                row["product_analysis_model"] = analysis_model
                reviewed += 1
            else:
                failed_ids.append(request_id_for_retry)
        question = str(row.get("question") or row.get("prompt") or "").strip()
        row["products"] = normalize_products(
            saver.ground_product_brands(
                str(row.get("web_body") or row.get("reply") or row.get("answer") or ""),
                saver.filter_products_for_question(question, products_of(row)),
            )
        )
        row["brands"] = sorted({
            str(item.get("brand_name") or "").strip()
            for item in products_of(row) if str(item.get("brand_name") or "").strip()
        })
        sources = row.get("sources") if isinstance(row.get("sources"), list) else []
        row["expected_source_count"] = max(int(row.get("expected_source_count") or 0), len(sources))
        repaired_rows.append(row)
    write_rows(path, repaired_rows)
    return {
        "rows": len(repaired_rows),
        "candidates": candidates,
        "reviewed": reviewed,
        "removed_duplicates": removed_duplicates,
        "attempted_ids": attempted_ids,
        "failed_ids": failed_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair suspicious remote-model product analysis records.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument("--limit", type=int, default=0, help="maximum candidates per model")
    parser.add_argument("--pending-only", action="store_true", help="only retry ai_pending rows")
    args = parser.parse_args()
    for model in args.models:
        print(model, json.dumps(repair_model(
            model,
            dry_run=args.dry_run,
            limit=max(0, args.limit),
            pending_only=args.pending_only,
            backup=True,
        ), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
