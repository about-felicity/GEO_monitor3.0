from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from doubao_source_content_worker import DB_PATH, INDEX_PATH, collect_urls
from monitor_core.owned_products import OWN_PRODUCT_RULES, own_product_mentions


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "runtime" / "owned_product_label_audit.csv"
DEFAULT_SUMMARY = ROOT / "runtime" / "owned_product_label_audit_summary.json"
RELIABLE_QUALITIES = {"high", "medium"}


def _load_bodies() -> dict[str, str]:
    if not DB_PATH.exists():
        return {}
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return {
            str(url): str(text or "")
            for url, text in connection.execute(
                "SELECT url, content_text FROM source_content "
                "WHERE status='ok' AND extraction_quality IN ('high','medium')"
            )
        }
    finally:
        connection.close()


def _snippet(text: str, product: str) -> str:
    rule = next((item for item in OWN_PRODUCT_RULES if item["name"] == product), None)
    if not rule or not text:
        return ""
    positions = [text.casefold().find(str(rule["brand"]).casefold())]
    positions.extend(text.casefold().find(str(term).casefold()) for term in rule["terms"])
    positions = [position for position in positions if position >= 0]
    if not positions:
        return ""
    position = min(positions)
    return " ".join(text[max(0, position - 70):position + 170].split())


def audit() -> tuple[list[dict[str, str]], dict[str, object]]:
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8")) if INDEX_PATH.exists() else {}
    entries = index.get("entries") or {}
    bodies = _load_bodies()
    rows: list[dict[str, str]] = []

    for source in collect_urls():
        url = str(source.get("url") or "")
        title = str(source.get("title") or "")
        entry = entries.get(url) or {}
        body = bodies.get(url, "")
        if not body and entry.get("status") == "ok" and entry.get("extraction_quality") in RELIABLE_QUALITIES:
            body = str(entry.get("excerpt") or "")
        title_products = set(own_product_mentions(title))
        body_products = set(own_product_mentions(body))
        explicit_products = sorted(title_products | body_products)
        stored_products = set(entry.get("own_product_mentions") or [])
        owned_brand_mentions = sorted(set(entry.get("owned_brand_mentions") or []))

        if explicit_products:
            status = "verified_product"
            if body_products - stored_products and not title_products:
                status = "possible_missed_index"
        elif owned_brand_mentions:
            status = "brand_only_removed"
        else:
            status = "no_product_evidence"

        scope = (
            "标题+正文" if title_products and body_products
            else "标题" if title_products
            else "正文" if body_products
            else ""
        )
        evidence_product = explicit_products[0] if explicit_products else ""
        evidence_text = title if evidence_product in title_products else body
        rows.append({
            "status": status,
            "model": str(source.get("model") or ""),
            "title": title,
            "url": url,
            "scope": scope,
            "owned_products": "、".join(explicit_products),
            "owned_brand_mentions": "、".join(owned_brand_mentions),
            "evidence": _snippet(evidence_text, evidence_product),
            "reliable_body": "是" if body else "否",
        })

    counts = Counter(row["status"] for row in rows)
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "unique_sources": len(rows),
        "reliable_archived_bodies": len(bodies),
        "verified_product": counts["verified_product"],
        "brand_only_removed": counts["brand_only_removed"],
        "possible_missed_index": counts["possible_missed_index"],
        "no_product_evidence": counts["no_product_evidence"],
        "method": "Only an explicit owned-brand plus its configured product descriptor qualifies.",
    }
    return rows, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit owned-product labels in both directions.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()
    rows, summary = audit()
    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["status"])
        writer.writeheader()
        writer.writerows(rows)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
