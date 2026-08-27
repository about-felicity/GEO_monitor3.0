"""Apply current master-brand normalization to historical product rows."""

import csv
import os
import shutil
import tempfile
from datetime import datetime

import save_doubao_refs as saver


def main():
    path = saver.OUT_PRODUCTS_CSV
    with saver.product_data_write_lock():
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

        changed = 0
        changes = {}
        for row in rows:
            old = str(row.get("brand_name") or "").strip()
            new = saver.canonical_ai_brand(old, row.get("product_name"), "")
            if new and new != old:
                row["brand_name"] = new
                changed += 1
                changes[(old, new)] = changes.get((old, new), 0) + 1

        if not changed:
            print("No historical brand rows require normalization.")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path + ".before_brand_normalize_" + stamp + ".bak"
        shutil.copy2(path, backup)

        fd, temp_path = tempfile.mkstemp(prefix="doubao_brand_normalize_", suffix=".csv", dir=saver.BASE_DIR)
        os.close(fd)
        try:
            with open(temp_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=saver.PRODUCT_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            if not saver.replace_file_with_retry(temp_path, path):
                raise RuntimeError("Could not replace product CSV")
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    print("changed=%d backup=%s" % (changed, backup))
    for (old, new), count in sorted(changes.items(), key=lambda item: (-item[1], item[0])):
        print("%s -> %s: %d" % (old, new, count))


if __name__ == "__main__":
    main()
