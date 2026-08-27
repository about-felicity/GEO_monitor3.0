"""One-off normalization of historical question fields in Doubao CSV files.

Reads every archived row, applies canonical_question_name from
doubao_question_aliases, and rewrites the files atomically.  Chat titles are
kept as-is; only the aggregation key (question) is updated.
"""

import csv
import os
import shutil
import tempfile

import doubao_question_aliases as qa
import save_doubao_refs as saver


FILES_AND_FIELDS = [
    (saver.OUT_ANSWERS_CSV, saver.ANSWER_FIELDS, ["question"]),
    (saver.OUT_PRODUCTS_CSV, saver.PRODUCT_FIELDS, ["question"]),
    (saver.OUT_CSV, saver.FIELDS, ["question"]),
]


def rewrite_csv(path, fields, normalize_fields):
    if not os.path.exists(path):
        print("skip: %s not found" % path)
        return 0, 0

    backup = path + ".before_question_normalize.bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)

    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or fields
        for row in reader:
            rows.append(row)

    changed = 0
    for row in rows:
        for field in normalize_fields:
            original = str(row.get(field) or "").strip()
            if not original:
                continue
            normalized = qa.canonical_question_name(original)
            if normalized and normalized != original:
                row[field] = normalized
                changed += 1

    fd, temp_path = tempfile.mkstemp(
        prefix="doubao_normalize_",
        suffix=".csv",
        dir=saver.BASE_DIR,
    )
    os.close(fd)
    try:
        with open(temp_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        if not saver.replace_file_with_retry(temp_path, path):
            print("failed: could not replace %s" % path)
            return 0, 0
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    return len(rows), changed


def main():
    with saver.product_data_write_lock(timeout_seconds=120):
        for path, fields, normalize_fields in FILES_AND_FIELDS:
            total, changed = rewrite_csv(path, fields, normalize_fields)
            print("%s: rows=%d changed=%d" % (os.path.basename(path), total, changed))


if __name__ == "__main__":
    main()
