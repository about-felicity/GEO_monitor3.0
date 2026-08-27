"""Remove duplicate product-review snapshots without touching source data."""

import csv
import os
import re
import shutil
import tempfile
from datetime import datetime

import save_doubao_refs as saver


def logical_product_key(value):
    return re.sub(r"[\s\-_—·]+", "", str(value or "")).casefold()


def main():
    path = saver.OUT_PRODUCTS_CSV
    if not os.path.exists(path):
        raise SystemExit("Product CSV does not exist: " + path)

    with saver.product_data_write_lock():
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        latest_snapshot = {}
        for row in rows:
            run_no = str(row.get("run_no") or "")
            answer_hash = str(row.get("answer_hash") or "")
            if not run_no or not answer_hash:
                continue
            key = (run_no, answer_hash)
            reviewed_at = str(row.get("reviewed_at") or "")
            if reviewed_at >= latest_snapshot.get(key, ""):
                latest_snapshot[key] = reviewed_at

        kept = []
        seen = set()
        for row in rows:
            run_no = str(row.get("run_no") or "")
            answer_hash = str(row.get("answer_hash") or "")
            reviewed_at = str(row.get("reviewed_at") or "")
            snapshot_key = (run_no, answer_hash)
            if run_no and answer_hash and reviewed_at != latest_snapshot.get(snapshot_key, reviewed_at):
                continue
            row_key = (
                run_no,
                answer_hash,
                str(row.get("product_index") or ""),
                logical_product_key(row.get("product_name")),
            )
            if row_key in seen:
                continue
            seen.add(row_key)
            kept.append(row)

        if len(kept) == len(rows):
            print("No duplicate product snapshots found.")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path + ".before_dedupe_" + stamp + ".bak"
        shutil.copy2(path, backup)

        fd, temp_path = tempfile.mkstemp(prefix="doubao_products_dedup_", suffix=".csv", dir=saver.BASE_DIR)
        os.close(fd)
        try:
            with open(temp_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=saver.PRODUCT_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(kept)
            if not saver.replace_file_with_retry(temp_path, path):
                raise RuntimeError("Could not replace product CSV")
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    print("rows_before=%d rows_after=%d removed=%d backup=%s" % (
        len(rows), len(kept), len(rows) - len(kept), backup,
    ))


if __name__ == "__main__":
    main()
