import csv
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
PRODUCT_CSV_PATH = BASE_DIR / "doubao_products_result.csv"
BRAND_AI_CACHE_PATH = BASE_DIR / "doubao_brand_ai_cache.json"


def load_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize_question(text):
    text = str(text or "").strip()
    aliases = {
        "推荐一款染发剂": "染发剂推荐",
        "推荐染发剂": "染发剂推荐",
        "推荐面膜": "面膜推荐",
        "推荐防脱精华液": "防脱精华液推荐",
        "祛痘精华推荐": "推荐祛痘精华液",
        "祛痘精华液推荐": "推荐祛痘精华液",
        "推荐祛痘精华": "推荐祛痘精华液",
        "沐浴精油推荐": "推荐沐浴精油",
        "沐浴油推荐": "推荐沐浴精油",
        "推荐沐浴油": "推荐沐浴精油",
        "推荐护发素": "护发素推荐",
    }
    return aliases.get(text, text)


KNOWN_BAD = {
    "款高性", "遵循天然", "强韧丰盈系列", "染发剂", "款抖音", "市监小", "不存在纯",
    "祛痘", "红肿痘", "痘", "痘痘", "敏感红", "油痘", "淡化痘", "突发红肿",
    "油敏痘", "使用小贴士", "植祛小", "面膜每周", "面膜每周2", "面膜一周",
    "高端沙龙卡诗", "高端沙龙级卡诗", "内蒙古", "韩愢单剂", "橡树",
}

KNOWN_BRANDS = {
    "优色林", "欧舒丹", "浴见", "Diptyque", "KONO", "Off&Relax", "梵玢 FBCY",
    "欧莱雅", "惊时", "伊帕尔汗", "韩方五谷", "TOCI", "百雀羚", "肌肤未来",
    "妮维雅", "尊蓝", "EHD", "Nebe", "极方", "多潘", "康如", "苏玫氏",
    "乐霖", "拜耳康王", "花王", "施华蔻", "三橡树", "高缇雅", "韩愢壹",
    "薇诺娜", "溪木源", "安修泽", "毕生之研", "丽可植", "肌漾", "ALRA",
    "芙清", "大水滴", "John Jeff", "道和时尚", "韩束", "域发", "森之宣言",
    "植芙琳", "妍绮", "OKSS", "海飞丝", "KIMTRUE", "Spes", "自然堂", "青植元",
    "焕颜计", "依漾", "卡诗",
}


def candidate_from_product(name):
    text = str(name or "").strip()
    text = re.sub(r"^[^\w\u4e00-\u9fff]+", "", text)
    text = re.sub(r"^\d+(?:\.\d+)?\s*(?:ml|mL|ML|g|G)\b", "", text).strip()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    for brand in sorted(KNOWN_BRANDS, key=len, reverse=True):
        if brand and brand in text:
            return brand
    token = re.split(r"[\s·\-_/]+", text)[0].strip()
    return token if token else ""


def read_candidates(limit=80):
    counter = Counter()
    examples = defaultdict(list)
    if not PRODUCT_CSV_PATH.exists():
        return []
    with PRODUCT_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = row.get("product_name") or ""
            cand = candidate_from_product(name)
            if not cand or cand in KNOWN_BRANDS or cand in KNOWN_BAD:
                continue
            if len(cand) > 14 or re.search(r"(?:ml|g|价格|图片|评测|测评|指南|排行|推荐$|怎么样)", cand):
                continue
            counter[cand] += 1
            if len(examples[cand]) < 3:
                examples[cand].append({
                    "question": normalize_question(row.get("question")),
                    "product_name": name,
                    "evidence": (row.get("evidence") or "")[:180],
                })
    return [
        {"candidate": cand, "count": count, "examples": examples[cand]}
        for cand, count in counter.most_common(limit)
    ]


def call_deepseek_anthropic(candidates):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic").rstrip("/")
    model = os.environ.get("MODEL_ID", "deepseek-v4-pro")
    if not api_key:
        raise RuntimeError("缺少 ANTHROPIC_API_KEY，无法调用品牌 AI 兜底")

    prompt = {
        "task": "判断候选词是否为真实消费品品牌，并给出规范品牌名。不是品牌的词必须判 false。",
        "rules": [
            "品类词、功效词、句子残片、平台词、文章标题、产品规格不是品牌。",
            "如果是品牌，canonical_brand 用中文常用名或官方英文名。",
            "输出严格 JSON：{\"items\":[{\"candidate\":\"...\",\"is_brand\":true/false,\"canonical_brand\":\"...\",\"reason\":\"...\"}]}",
        ],
        "candidates": candidates,
    }
    body = {
        "model": model,
        "max_tokens": 3000,
        "messages": [
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}
        ],
    }
    req = urllib.request.Request(
        base_url + "/v1/messages",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=int(os.environ.get("DOUBAO_BRAND_AI_TIMEOUT", "60"))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = "".join(part.get("text", "") for part in data.get("content", []) if isinstance(part, dict))
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise RuntimeError("模型没有返回 JSON: " + text[:500])
    parsed = json.loads(match.group(0))
    return parsed.get("items") or []


def main():
    limit = int(os.environ.get("DOUBAO_BRAND_AI_LIMIT", "60"))
    candidates = read_candidates(limit=limit)
    cache = load_json(BRAND_AI_CACHE_PATH)
    brands = cache.get("brands") if isinstance(cache.get("brands"), dict) else {}
    pending = [item for item in candidates if item["candidate"] not in brands]
    if not pending:
        print(json.dumps({"ok": True, "message": "没有新的待判断品牌候选", "candidates": len(candidates)}, ensure_ascii=False))
        return
    result_items = call_deepseek_anthropic(pending)
    for item in result_items:
        cand = str(item.get("candidate") or "").strip()
        if not cand:
            continue
        is_brand = bool(item.get("is_brand"))
        canonical = str(item.get("canonical_brand") or "").strip()
        brands[cand] = {
            "is_brand": is_brand,
            "brand": canonical if is_brand else "",
            "reason": str(item.get("reason") or ""),
        }
    cache["brands"] = brands
    save_json(BRAND_AI_CACHE_PATH, cache)
    print(json.dumps({"ok": True, "checked": len(result_items), "cache": str(BRAND_AI_CACHE_PATH)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
