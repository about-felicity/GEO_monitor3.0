"""Back-test the deterministic product analyzer against verified database rows."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json

import doubao_env_loader  # noqa: F401
import save_doubao_refs as saver
from monitor_core.database import connection
from monitor_core.product_analysis import (
    build_knowledge,
    deterministic_review,
    equivalent_sets,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default="2026-08-16")
    args = parser.parse_args()
    with connection() as conn:
        training = conn.execute(
            "SELECT r.question,r.answer,COALESCE((SELECT jsonb_agg(p.payload ORDER BY p.product_index) "
            "FROM monitor_products p WHERE p.model_id=r.model_id AND p.run_id=r.run_id),'[]'::jsonb) products "
            "FROM monitor_runs r WHERE COALESCE(r.payload->>'product_review_status','')='ai_verified' AND day<>%s",
            (args.day,),
        ).fetchall()
        tests = conn.execute(
            "SELECT r.model_id,r.question,r.answer,COALESCE((SELECT jsonb_agg(p.payload ORDER BY p.product_index) "
            "FROM monitor_products p WHERE p.model_id=r.model_id AND p.run_id=r.run_id),'[]'::jsonb) products "
            "FROM monitor_runs r WHERE COALESCE(r.payload->>'product_review_status','')='ai_verified' AND day=%s",
            (args.day,),
        ).fetchall()
    knowledge = build_knowledge(training)
    stats = defaultdict(lambda: {"total": 0, "accepted": 0, "correct": 0})
    errors = []
    for row in tests:
        current = stats[row["model_id"]]
        current["total"] += 1
        answer = str(row["answer"] or "")
        products, method = deterministic_review(
            answer, str(row["question"] or ""), knowledge,
            saver.extract_products(answer), saver.numbered_product_block_count(answer),
        )
        # Production accepts only exact reuse of a previously paid-and-verified
        # answer. Parser consensus is useful for candidate generation but is
        # intentionally not a zero-token certification path.
        if products is None or method != "verified_answer_reuse":
            continue
        current["accepted"] += 1
        if equivalent_sets(products, row["products"] or []):
            current["correct"] += 1
        elif len(errors) < 20:
            errors.append({
                "model": row["model_id"], "question": row["question"], "method": method,
                "algorithm": [item.get("product_name") for item in products],
                "verified": [item.get("product_name") for item in row["products"] or []],
            })
    for value in stats.values():
        value["precision"] = round(value["correct"] / max(1, value["accepted"]), 4)
        value["coverage"] = round(value["accepted"] / max(1, value["total"]), 4)
    print(json.dumps({"day": args.day, "models": stats, "errors": errors},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
