"""Repair run_no collisions caused by concurrent Doubao capture writers."""

import csv
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import save_doubao_refs as saver


BASE_DIR = Path(__file__).resolve().parents[2]
FILES = {
    "refs": (Path(saver.OUT_CSV), saver.FIELDS),
    "answers": (Path(saver.OUT_ANSWERS_CSV), saver.ANSWER_FIELDS),
    "products": (Path(saver.OUT_PRODUCTS_CSV), saver.PRODUCT_FIELDS),
}


def read_rows(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_rows_atomic(path, fields, rows):
    fd, temp_path = tempfile.mkstemp(prefix="doubao_run_repair_", suffix=".csv", dir=str(BASE_DIR))
    os.close(fd)
    try:
        with open(temp_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        if not saver.replace_file_with_retry(
            temp_path, str(path), attempts=120, delay_seconds=0.5
        ):
            raise PermissionError("CSV remained busy after 60 seconds: " + str(path))
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def safe_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def main():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with saver.product_data_write_lock(timeout_seconds=120):
        data = {name: read_rows(path) for name, (path, _) in FILES.items()}
        all_numbers = [safe_int(row.get("run_no")) for rows in data.values() for row in rows]
        try:
            all_numbers.append(safe_int(Path(saver.OUT_RUN_COUNTER).read_text(encoding="ascii")))
        except Exception:
            pass
        next_number = max(all_numbers or [0]) + 1

        answers_by_run = defaultdict(list)
        for row in data["answers"]:
            run_no = safe_int(row.get("run_no"))
            chat_id = str(row.get("chat_id") or "").strip()
            if run_no and chat_id:
                answers_by_run[run_no].append(row)

        conflicts = {}
        for run_no, rows in answers_by_run.items():
            identities = {}
            for row in rows:
                chat_id = str(row.get("chat_id") or "").strip()
                identities.setdefault(chat_id, row)
            if len(identities) > 1:
                conflicts[run_no] = identities

        if not conflicts:
            print("No conflicting run numbers found.")
            return

        product_chats_by_run = defaultdict(set)
        for row in data["products"]:
            product_chats_by_run[safe_int(row.get("run_no"))].add(str(row.get("chat_id") or "").strip())

        mapping = {}
        repair_log = []
        for run_no in sorted(conflicts):
            identities = conflicts[run_no]
            product_chats = {chat for chat in product_chats_by_run.get(run_no, set()) if chat in identities}
            ordered = sorted(
                identities,
                key=lambda chat: (
                    str(identities[chat].get("run_time") or ""),
                    str(identities[chat].get("extracted_at") or ""),
                    chat,
                ),
            )
            # Preserve the identity whose product snapshot survived the old
            # run-level replace operation. This minimizes model re-review.
            keep_chat = sorted(product_chats)[0] if product_chats else ordered[0]
            mapping[(run_no, keep_chat)] = run_no
            for chat_id in ordered:
                if chat_id == keep_chat:
                    continue
                mapping[(run_no, chat_id)] = next_number
                repair_log.append((run_no, chat_id, next_number))
                next_number += 1

        for rows in data.values():
            for row in rows:
                old_run = safe_int(row.get("run_no"))
                chat_id = str(row.get("chat_id") or "").strip()
                new_run = mapping.get((old_run, chat_id))
                if new_run:
                    row["run_no"] = str(new_run)

        product_identity = {
            (safe_int(row.get("run_no")), str(row.get("chat_id") or "").strip())
            for row in data["products"]
        }
        pending = 0
        for row in data["answers"]:
            identity = (safe_int(row.get("run_no")), str(row.get("chat_id") or "").strip())
            if identity in product_identity:
                continue
            if saver.is_recommendation_question(row.get("question") or ""):
                row["review_status"] = "ai_pending"
                row["model"] = "deepseek-v4-flash"
                row["reviewed_at"] = saver.now_str()
                pending += 1

        for path, _ in FILES.values():
            if path.exists():
                shutil.copy2(path, str(path) + ".before_run_conflict_repair_" + stamp + ".bak")
        for name, (path, fields) in FILES.items():
            write_rows_atomic(path, fields, data[name])
        highest_reserved = max(
            [safe_int(row.get("run_no")) for rows in data.values() for row in rows] or [0]
        )
        Path(saver.OUT_RUN_COUNTER).write_text(str(highest_reserved), encoding="ascii")

    print("conflict_runs=%d reassigned=%d pending_reaudit=%d" % (
        len(conflicts), len(repair_log), pending
    ))
    for old_run, chat_id, new_run in repair_log:
        print("old_run=%s chat_id=%s new_run=%s" % (old_run, chat_id, new_run))


if __name__ == "__main__":
    main()
