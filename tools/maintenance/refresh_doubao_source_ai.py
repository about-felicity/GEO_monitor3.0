import csv
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import save_doubao_refs as saver


BASE_DIR = Path(__file__).resolve().parents[2]
CSV_PATH = BASE_DIR / "doubao_refs_result.csv"
AI_CACHE_PATH = BASE_DIR / "doubao_source_ai_cache.json"


def load_cache():
    if not AI_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(AI_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache):
    AI_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def host_of(url):
    return urlparse(url or "").netloc.lower().split(":")[0]


def collect_samples():
    samples = {}
    if not CSV_PATH.exists():
        return samples
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            href = row.get("href") or ""
            host = host_of(href)
            if not host:
                continue
            current = samples.get(host)
            # Prefer rows with longer titles because the AI has more context.
            if not current or len(row.get("title") or "") > len(current.get("title") or ""):
                samples[host] = row
    return samples


def should_refresh(host, row, cache):
    cached = cache.get(host)
    if not cached:
        return True
    media = cached.get("media", "")
    if saver.is_weak_media_name(media, host):
        return True
    title = row.get("title", "")
    if media and media.lower() in title.lower() and len(media) > 1:
        return False
    return False


def classify_one(host, row):
    href = row.get("href") or ""
    meta = saver.get_source_meta(href)
    if row.get("title") and not meta.get("title"):
        meta["title"] = row.get("title", "")
    return saver.call_anthropic_source_classifier(href, meta) or saver.call_openai_source_classifier(href, meta)


def main():
    os.environ["DOUBAO_USE_AI_SOURCE"] = "1"
    os.environ["DOUBAO_REFRESH_WEAK_AI_SOURCE"] = "1"
    os.environ.setdefault("DOUBAO_AI_TIMEOUT", "12")
    os.environ.setdefault("DOUBAO_META_TIMEOUT", "5")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        raise RuntimeError("缺少模型 API_KEY：请先设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY")

    samples = collect_samples()
    cache = load_cache()
    refreshed = []
    skipped = []
    failed = []

    for host, row in sorted(samples.items()):
        if not should_refresh(host, row, cache):
            skipped.append(host)
            continue
        result = classify_one(host, row)
        if result:
            cache[host] = result
            save_cache(cache)
            refreshed.append((host, result))
            print("refreshed:", host, "=>", result.get("source_type"), result.get("media"))
        else:
            failed.append(host)
            print("failed:", host)

    has_xlsx = saver.write_xlsx_from_csv()
    print(json.dumps({
        "ok": True,
        "refreshed": len(refreshed),
        "skipped": len(skipped),
        "failed": failed,
        "xlsx": str(saver.OUT_XLSX) if has_xlsx else "",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
