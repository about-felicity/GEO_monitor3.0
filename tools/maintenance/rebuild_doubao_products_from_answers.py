"""Re-run AI product review from the archived Doubao answer bodies.

This tool is intentionally opt-in because it calls the configured model and
therefore may incur API cost.  By default it only retries rows that were not
AI-verified.  Use --all to re-review every archived answer.
"""

import argparse
import csv
import os
import tempfile

import doubao_question_aliases as qa
import save_doubao_refs as saver


def write_csv_atomic(path, fields, rows):
    fd, temp_path = tempfile.mkstemp(prefix="doubao_rebuild_", suffix=".csv", dir=saver.BASE_DIR)
    os.close(fd)
    try:
        with open(temp_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        if not saver.replace_file_with_retry(temp_path, path):
            raise RuntimeError("CSV is busy; retry worker will run again: " + path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="re-review every archived answer")
    parser.add_argument("--pending-only", action="store_true", help="retry only AI pending answers")
    parser.add_argument("--limit", type=int, default=0, help="maximum answers to process")
    parser.add_argument("--question", default="", help="only re-review this exact question")
    parser.add_argument("--run-no", default="", help="only re-review this exact run number")
    parser.add_argument("--date", default="", help="only re-review run_time starting with YYYY-MM-DD")
    parser.add_argument("--shard-count", type=int, default=1, help="number of parallel review shards")
    parser.add_argument("--shard-index", type=int, default=0, help="zero-based shard handled by this process")
    args = parser.parse_args()

    if not os.path.exists(saver.OUT_ANSWERS_CSV):
        raise SystemExit("No archived answer bodies found: " + saver.OUT_ANSWERS_CSV)

    with open(saver.OUT_ANSWERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        answers = list(csv.DictReader(f))
    if args.all:
        selected = answers
    elif args.pending_only:
        selected = [r for r in answers if r.get("review_status") == "ai_pending"]
    else:
        selected = [r for r in answers if r.get("review_status") != "ai_verified"]
    if args.question:
        selected = [r for r in selected if (r.get("question") or "").strip() == args.question.strip()]
    if args.run_no:
        selected = [
            r for r in selected
            if str(r.get("run_no") or "").strip() == str(args.run_no).strip()
        ]
    if args.date:
        selected = [
            r for r in selected
            if str(r.get("run_time") or "").startswith(args.date.strip())
        ]
    # The foreground collector may have submitted the same archived answer
    # twice. Review each run/body only once so a rebuild cannot recreate two
    # identical product snapshots.
    deduped = {}
    for row in selected:
        key = (str(row.get("run_no") or ""), str(row.get("answer_hash") or ""))
        deduped[key] = row
    selected = list(deduped.values())
    shard_count = max(1, int(args.shard_count or 1))
    shard_index = int(args.shard_index or 0)
    if shard_index < 0 or shard_index >= shard_count:
        raise SystemExit("shard-index must be between 0 and shard-count - 1")
    if shard_count > 1:
        def belongs_to_shard(row):
            run_no = str(row.get("run_no") or "0")
            try:
                shard_key = int(run_no)
            except Exception:
                shard_key = sum(ord(ch) for ch in run_no)
            return shard_key % shard_count == shard_index
        selected = [row for row in selected if belongs_to_shard(row)]
    if args.limit:
        selected = selected[:max(0, args.limit)]
    if not selected:
        print("Nothing to review.")
        return

    reviewed = {}
    replacement_rows = []
    normalized_questions = {}
    for answer in selected:
        products, status, method, model = saver.review_products_with_ai(
            answer.get("answer_text") or "", answer.get("question") or ""
        )
        key = (str(answer.get("run_no") or ""), str(answer.get("answer_hash") or ""))
        reviewed[key] = (status, model)
        normalized_question = qa.canonical_question_name(answer.get("question") or answer.get("chat_title") or "")
        if not normalized_question:
            normalized_question = answer.get("question") or ""
        normalized_questions[key] = normalized_question
        if not saver.is_recommendation_question(normalized_question):
            continue
        for position, item in enumerate(products, 1):
            replacement_rows.append({
                "run_no": answer.get("run_no", ""),
                "run_time": answer.get("run_time", ""),
                "chat_id": answer.get("chat_id", ""),
                "chat_title": answer.get("chat_title", ""),
                "question": normalized_question,
                "page_url": answer.get("page_url", ""),
                **{
                    field: answer.get(field, "")
                    for field in saver.ACCOUNT_TIME_FIELDS
                },
                "product_index": item.get("rank") or position,
                "product_name": item.get("product_name", ""),
                "brand_name": item.get("brand_name", ""),
                "evidence": item.get("evidence", ""),
                "product_count": len(products),
                "rank_type": item.get("rank_type") or "appearance_order",
                "extraction_method": method,
                "review_status": status,
                "model": model,
                "reviewed_at": saver.now_str(),
                "answer_hash": answer.get("answer_hash", ""),
                "extracted_at": answer.get("extracted_at", ""),
            })

    # Re-read both files only when committing. Foreground capture may have
    # appended new rounds while the model calls above were running.
    with saver.product_data_write_lock():
        saver.ensure_products_csv_schema()
        existing = []
        if os.path.exists(saver.OUT_PRODUCTS_CSV):
            with open(saver.OUT_PRODUCTS_CSV, "r", encoding="utf-8-sig", newline="") as f:
                existing = list(csv.DictReader(f))
        selected_runs = {key[0] for key in reviewed}
        retained = [row for row in existing if str(row.get("run_no") or "") not in selected_runs]
        write_csv_atomic(saver.OUT_PRODUCTS_CSV, saver.PRODUCT_FIELDS, retained + replacement_rows)

        with open(saver.OUT_ANSWERS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            latest_answers = list(csv.DictReader(f))
        for answer in latest_answers:
            key = (str(answer.get("run_no") or ""), str(answer.get("answer_hash") or ""))
            if key in reviewed:
                answer["review_status"], answer["model"] = reviewed[key]
                answer["reviewed_at"] = saver.now_str()
            if key in normalized_questions:
                answer["question"] = normalized_questions[key]
        write_csv_atomic(saver.OUT_ANSWERS_CSV, saver.ANSWER_FIELDS, latest_answers)
    print("reviewed=%d products=%d" % (len(reviewed), len(replacement_rows)))


if __name__ == "__main__":
    main()
