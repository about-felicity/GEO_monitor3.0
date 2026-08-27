"""Rebuild public brand fields from every archived full article body."""

from __future__ import annotations

import json

import doubao_brand_settings as brand_settings
import doubao_source_content_worker as worker


def main() -> int:
    connection = worker.init_db()
    index = worker.load_index()
    entries = index.setdefault("entries", {})
    _brands, vocab_hash = worker.brand_vocabulary()
    owned_aliases = [
        (item["name"], alias)
        for item in brand_settings.load_settings().get("owned_brands") or []
        for alias in item.get("aliases") or [item["name"]]
    ]
    updated = owned = 0
    try:
        cursor = connection.execute(
            "SELECT url,content_text FROM source_content WHERE status='ok'"
        )
        for url, content_text in cursor:
            public = dict(entries.get(url) or {})
            text = str(content_text or "")
            hits = sorted({
                name for name, alias in owned_aliases
                if worker.dashboard.title_mentions_brand(text, alias)
            })
            public["owned_brand_mentions"] = hits
            public["brand_mentions"] = sorted(
                set(public.get("brand_mentions") or []) | set(hits)
            )
            public["own_product_mentions"] = worker.dashboard.own_product_mentions(text)
            public["own_product_schema_version"] = worker.dashboard.OWN_PRODUCT_SCHEMA_VERSION
            public["vocab_hash"] = vocab_hash
            entries[url] = public
            updated += 1
            owned += bool(hits)
        index["vocab_hash"] = vocab_hash
        index["updated_at"] = worker.now_str()
        worker.atomic_json_write(worker.INDEX_PATH, index)
    finally:
        connection.close()
    print(json.dumps({"ok": True, "updated": updated, "owned": owned,
                      "vocab_hash": vocab_hash}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
