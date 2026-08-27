"""Fail-fast integrity audit for dashboard brand-mention metrics."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta

from monitor_core import database
from monitor_core.analytics import (
    _brand_matcher,
    _catalogs,
    _compact,
    _daily_mentions,
    _mentions,
    daily_brand_source_mentions,
    prepare_analytics,
    recommendation_body,
    canonical_brand_name,
)


def audit(days: list[str]) -> tuple[list[str], dict]:
    runs_by_model = database.load_runs_by_model(
        day_from=min(days), day_to=max(days)
    )
    aliases, _products = _catalogs(runs_by_model)
    matcher = _brand_matcher(aliases)
    canonical_by_key = {
        _compact(alias): brand
        for brand, values in aliases.items()
        for alias in values | {brand}
    }

    expected: dict[tuple[str, str], list[str]] = {}
    for model, runs in runs_by_model.items():
        for run in runs:
            if str(run.get("day") or "") not in days:
                continue
            key = (model, str(run.get("run_id") or ""))
            review_status = str(run.get("product_review_status") or "").strip()
            structured_brands = {
                canonical
                for item in run.get("products") or []
                if isinstance(item, dict)
                if (normalized := canonical_brand_name(
                    item.get("brand") or item.get("brand_name") or ""
                ))
                if (canonical := canonical_by_key.get(_compact(normalized), normalized))
            }
            text_brands = {
                canonical
                for mention in _mentions(
                    recommendation_body(run.get("answer") or ""), matcher
                )
                if (canonical := canonical_brand_name(mention))
            }
            expected[key] = sorted(
                text_brands | structured_brands
            ) if review_status != "ai_pending" else []

    prepare_analytics(runs_by_model)
    errors: list[str] = []
    summary: dict = {}
    for model, runs in sorted(runs_by_model.items()):
        model_summary = summary.setdefault(model, {})
        for day in days:
            day_runs = [
                run for run in runs
                if str(run.get("day") or "") == day
                and str(run.get("status") or "success") == "success"
            ]
            day_summary = model_summary.setdefault(day, {
                "runs": len(day_runs), "brand_mentions": 0,
                "pending_runs": 0, "pending_with_brand_mentions": 0,
                "source_rows": 0, "unique_sources": 0,
                "source_brand_mentions": 0,
            })
            unique_sources: dict[str, dict] = {}
            for run in day_runs:
                key = (model, str(run.get("run_id") or ""))
                actual = sorted(set(run.get("brands") or []))
                wanted = sorted(set(expected.get(key, [])))
                if actual != wanted:
                    errors.append(
                        f"answer mismatch {model}/{day}/{key[1]}: "
                        f"expected={wanted!r} actual={actual!r}"
                    )
                day_summary["brand_mentions"] += len(actual)
                pending = str(run.get("product_review_status") or "") == "ai_pending"
                day_summary["pending_runs"] += int(pending)
                day_summary["pending_with_brand_mentions"] += int(pending and bool(actual))
                for source in run.get("sources") or []:
                    day_summary["source_rows"] += 1
                    source_key = str(
                        source.get("canonical_url") or source.get("url") or ""
                    ).strip()
                    if source_key:
                        unique_sources.setdefault(source_key, source)
                    title = set(source.get("title_brand_mentions") or [])
                    body = set(source.get("body_brand_mentions") or [])
                    combined = set(source.get("brand_mentions") or [])
                    if combined != title | body:
                        errors.append(
                            f"source union mismatch {model}/{day}/{key[1]}/{source_key}"
                        )
                    if source.get("type") == "视频" and body:
                        errors.append(
                            f"video has body brands {model}/{day}/{key[1]}/{source_key}"
                        )
                    if bool(source.get("own_brand")) != bool(source.get("owned_brands")):
                        errors.append(
                            f"owned-brand flag mismatch {model}/{day}/{key[1]}/{source_key}"
                        )
            day_summary["unique_sources"] = len(unique_sources)
            day_summary["source_brand_mentions"] = sum(
                len(set(source.get("brand_mentions") or []))
                for source in unique_sources.values()
            )

            for question in sorted({str(run.get("question") or "") for run in day_runs}):
                question_runs = [
                    run for run in day_runs
                    if str(run.get("question") or "") == question
                ]
                manual_answers = Counter()
                for run in question_runs:
                    manual_answers.update(set(run.get("brands") or []))
                answer_rollup = _daily_mentions(question_runs, "brands")
                if len(answer_rollup) != 1 or answer_rollup[0]["runs"] != len(question_runs):
                    errors.append(
                        f"answer denominator mismatch {model}/{day}/{question}"
                    )
                    continue
                reported_answers = Counter({
                    item["name"]: item["mentions"]
                    for item in answer_rollup[0]["items"]
                })
                if reported_answers != manual_answers:
                    errors.append(
                        f"answer brand rollup mismatch {model}/{day}/{question}"
                    )
                for item in answer_rollup[0]["items"]:
                    expected_rate = round(
                        item["mentions"] * 100 / len(question_runs), 2
                    ) if question_runs else 0
                    if item["mention_rate"] != expected_rate:
                        errors.append(
                            f"answer rate mismatch {model}/{day}/{question}/{item['name']}"
                        )

            # Recalculate the daily rollup independently from the dashboard row.
            rollup = daily_brand_source_mentions(day_runs)
            if day_runs:
                if len(rollup) != 1 or rollup[0]["sources"] != len(unique_sources):
                    errors.append(f"source denominator mismatch {model}/{day}")
                else:
                    manual = Counter()
                    for source in unique_sources.values():
                        manual.update(set(source.get("brand_mentions") or []))
                    reported = Counter({
                        item["name"]: item["mentions"]
                        for item in rollup[0]["items"]
                    })
                    if reported != manual:
                        errors.append(f"source brand rollup mismatch {model}/{day}")
                    for item in rollup[0]["items"]:
                        brand = item["name"]
                        eligible = sum(
                            source.get("type") == "视频"
                            or bool(source.get("body_analysis_ready"))
                            or brand in set(source.get("title_brand_mentions") or [])
                            for source in unique_sources.values()
                        )
                        if item.get("eligible_sources") != eligible:
                            errors.append(
                                f"source eligible denominator mismatch {model}/{day}/{brand}"
                            )
                        expected_rate = round(
                            item["mentions"] * 100 / eligible, 2
                        ) if eligible else 0
                        if item["mention_rate"] != expected_rate:
                            errors.append(
                                f"source rate mismatch {model}/{day}/{brand}"
                            )
    return errors, summary


def main() -> int:
    today = date.today()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days", nargs="+",
        default=[str(today - timedelta(days=1)), str(today)],
    )
    args = parser.parse_args()
    errors, summary = audit(sorted(set(args.days)))
    for model, days in summary.items():
        for day, values in days.items():
            print(model, day, " ".join(f"{key}={value}" for key, value in values.items()))
    if errors:
        print(f"FAILED errors={len(errors)}")
        for error in errors[:100]:
            print(error)
        return 1
    print("OK brand mention integrity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
