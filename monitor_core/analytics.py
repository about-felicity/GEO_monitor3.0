from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

csv.field_size_limit(64 * 1024 * 1024)

from monitor_core.owned_products import (
    OWN_PRODUCT_SCHEMA_VERSION,
    brands_for_products,
    own_product_mentions,
    owned_product_recommendations,
    owned_product_brand,
    owned_products_for_question,
)

try:
    import jieba
except ImportError:  # 部署未安装时仍可使用内置保守分词。
    jieba = None


BEIJING = timezone(timedelta(hours=8))
TRACKING = {"refer", "from", "source", "spm", "srsltid", "share_token"}
VIDEO_DOMAINS = ("douyin.com", "iesdouyin.com", "bilibili.com", "youtube.com", "kuaishou.com", "ixigua.com")
SHOP_DOMAINS = ("jd.com", "taobao.com", "tmall.com", "yangkeduo.com")
SOCIAL_DOMAINS = ("xiaohongshu.com", "weibo.com")
MEDIA_NAMES = {"zhihu.com": "知乎", "xiaohongshu.com": "小红书", "weibo.com": "微博", "bilibili.com": "哔哩哔哩", "douyin.com": "抖音", "iesdouyin.com": "抖音", "jd.com": "京东", "taobao.com": "淘宝", "tmall.com": "天猫", "baidu.com": "百度", "qq.com": "腾讯网", "163.com": "网易", "ifeng.com": "凤凰网"}
KEYWORD_STOP = {"推荐", "一款", "什么", "怎么", "可以", "一个", "使用", "产品", "品牌", "最新", "真的", "这款", "哪些", "比较", "选择", "效果", "十大", "排行榜", "测评"}
BRAND_STOP = {"家用", "小助手", "泡沫染发", "植物染发", "染发", "染发剂", "染发膏", "角蛋白", "米诺地尔", "生物素", "多肽", "精华液", "洗发水", "护发素", "防晒", "面膜", "水杨酸", "二硫化硒", "烟酰胺", "玻尿酸", "PCA锌", "辛酰甘氨酸", "氨基酸", "无硅油"}
PARTICLES = "的了是一在于和及与或到用为把被从对跟等很更最也都还就"
ENGLISH_STOP = {"the", "and", "for", "with", "from", "best", "top", "review", "reviews"}
ROOT = Path(__file__).resolve().parent.parent
CONTENT_INDEX_PATH = ROOT / "doubao_source_content_index.json"
BRAND_MATCH_SCHEMA_VERSION = 4

# Short Chinese brand names can occur across an ordinary word boundary. For
# example, 道和 in 渠道和使用 is not the brand 道和. Ambiguous names require
# either punctuation boundaries or a nearby brand/product cue.
AMBIGUOUS_BRAND_CONTEXTS = {
    "道和": {
        "left": ("品牌", "推荐", "选择", "选", "来自", "购买", "入手", "国货", "排名", "榜单", "对比", "测评", "首选"),
        "right": ("时尚", "小红瓶", "小绿瓶", "品牌", "产品", "官方", "旗舰", "系列", "出品", "研发", "公司", "集团", "专柜", "店", "的"),
    },
}


def _compact(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def brand_alias_occurs(text: str, alias: str) -> bool:
    """Return whether an alias is a real mention rather than a substring collision."""
    raw = str(text or "").casefold()
    token = _compact(alias)
    if not raw or not token:
        return False
    compact = _compact(raw)
    if not re.search(r"[\u3400-\u9fff]", token):
        # Match against the compact form so aliases such as ``Off & Relax``
        # and ``Off&Relax`` resolve to the same brand. Keep ASCII word
        # boundaries to avoid treating a short alias as part of a larger word.
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", compact))
    contexts = AMBIGUOUS_BRAND_CONTEXTS.get(token)
    if not contexts:
        return token in compact
    if re.search(r"(?<![\u3400-\u9fff])" + re.escape(str(alias).casefold()) + r"(?![\u3400-\u9fff])", raw):
        return True
    start = 0
    while True:
        position = compact.find(token, start)
        if position < 0:
            return False
        left = compact[max(0, position - 6):position]
        right = compact[position + len(token):position + len(token) + 8]
        if any(left.endswith(cue) for cue in contexts["left"]) or any(right.startswith(cue) for cue in contexts["right"]):
            return True
        start = position + 1


INVALID_BRAND_NAMES = {
    "拼多多", "淘宝", "天猫", "京东", "小红书", "抖音",
    "英国进口", "英国进口3D", "英国进口3D精油", "3D",
    "摩洛哥油", "摩洛哥坚果油", "香水柔顺",
    "生姜", "生姜洗发水", "侧柏叶生姜洗发水", "黑姜", "黑姜洗发水",
    "草本植萃", "眼睫毛增长液", "眉毛增长液",
    "拍1发3", "拍一发二", "黄金胶原蛋白", "多肽角蛋白", "分馏椰子油",
    "黄金", "黄金面膜", "黄金胶原蛋白面膜", "黄金胶原蛋白多肽发光面膜",
    "乐佳好货",
    "12种氨基酸", "俄罗斯睫毛营养液", "新升级通用", "植萃", "植物精华",
    "泡泡染发剂", "高端理发店同款", "端享优选店铺", "专攻", "美白祛斑霜",
    "韩国三文鱼", "粉水光", "草本", "草本育发", "苗族传承", "水晶", "紫苏",
}

# Verified non-brand entities repeatedly returned by upstream extractors.
# Exact values keep the filter conservative while blocking known ingredients,
# product nicknames, benefit terms, people and shop/channel labels.
INVALID_BRAND_NAMES.update({
    "小金瓶", "龙胆黑钻", "3D精油", "复活草", "极光", "377VC",
    "乌木玫瑰", "玉龙茶香", "愉禾依兰", "蓬松打底喷雾", "极光海燕",
    "侧柏叶", "生姜侧柏叶", "新疆乌斯玛", "澳洲坚果", "积雪草",
    "皮傲宁", "氨甲环酸", "海盐", "椰子果", "胶原蛋白多肽",
    "花香", "植萃精油", "基础款", "防脱固发", "生姜防脱洗发水",
    "盖白", "自然盖白", "金梳盖白", "一梳盖白", "泡泡", "空气感",
    "自然滋润", "贵妇", "闪亮",
    "本草", "毛囊清洁", "眼睫毛", "眼睫毛滋养液", "胶原蛋白面膜",
    "美容院定制款", "在研",
    "郝邵文", "海马体", "毛曼陀罗", "主持人严选", "泽经百货", "闻柳甄选",
    "瑾墨成百货商行", "踏恒百货",
})
INVALID_BRAND_KEYS = {_compact(item) for item in INVALID_BRAND_NAMES}
BRAND_CANONICAL_GROUPS = {
    "欧莱雅": {"欧莱雅", "巴黎欧莱雅", "欧莱雅PRO"},
    "资生堂": {"资生堂", "资生堂悦薇"},
    "VSVE 威诗薇儿": {"VSVE", "vsve 威诗薇儿", "VSVE 威诗薇儿"},
    "SPES 诗裴丝": {"SPES", "Spes", "Spes 诗裴丝", "SPES 诗裴丝"},
    "Fresh 馥蕾诗": {"馥蕾诗", "Fresh 馥蕾诗"},
    "GEMSHO 睫美秀": {"GEMSHO", "GEMSHO 睫美秀"},
    "Almea 阿米娅": {"Almea", "阿米娅", "Almea 阿米娅"},
    "Balea 芭乐雅": {"Balea", "芭乐雅", "Balea 芭乐雅"},
    "TALIKA 塔莉卡": {"TALIKA", "塔莉卡", "TALIKA 塔莉卡"},
    "DR.WU 达尔肤": {"达尔肤", "DR.WU", "DR.WU 达尔肤"},
    "爱茉莉魅尚萱": {"爱茉莉", "爱茉莉魅尚萱"},
    "DS实验室": {"DS", "DS实验室"},
    "首迷": {"首迷", "JREY首迷"},
    "白云山": {"白云山", "广药白云山", "广药白云山敬修堂", "广州白云山敬修堂"},
    "施华蔻": {"施华蔻", "德国施华蔻"},
    "卡诗": {"卡诗", "巴黎卡诗", "KERASTASE 巴黎卡诗"},
    "露卡菲娅": {"露卡菲娅", "露卡菲亚"},
    "Cavilla 卡维拉": {"卡维拉", "卡微拉", "Cavilla 卡维拉", "CAVILLA 卡维拉"},
    "KIMTRUE 且初": {"KIMTRUE", "且初", "KIMTRUE且初", "KIMTRUE 且初"},
    "百雀羚": {"百雀羚", "三生花"},
    "吕RYO": {"吕", "绿吕", "紫吕", "RYO", "RYO 吕", "吕RYO"},
    "儒曼": {"儒曼"},
    "清扬": {"清扬"},
    "米云": {"米云"},
    "滋源": {"滋源"},
}
BRAND_CANONICAL_GROUPS.update({
    "欧莱雅": {
        "欧莱雅", "巴黎欧莱雅", "欧莱雅PRO", "欧莱雅男士",
        "欧莱雅小红瓶", "欧莱雅精油染", "欧莱雅奇焕精油系列",
    },
    "卡诗": {
        "卡诗", "巴黎卡诗", "KERASTASE 巴黎卡诗",
        "卡诗山茶花精油", "卡诗黑钻",
    },
    "沙宣": {"沙宣", "沙宣红宝石精油"},
    "花王": {"花王", "花王莉婕泡泡染"},
    "道和": {"道和", "道和小绿瓶", "道和小红瓶"},
    "Kosliv 可氏利夫": {"Kosliv", "Kosliv可氏利夫", "Kosliv 可氏利夫", "可氏利夫"},
    "拾宓": {"拾宓", "拾宓shimi", "拾宓 SHIMI"},
    "L'OCCITANE 欧舒丹": {"欧舒丹", "L'OCCITANE欧舒丹", "L'OCCITANE 欧舒丹"},
    "DHC 蝶翠诗": {"DHC", "DHC蝶翠诗", "DHC 蝶翠诗", "蝶翠诗"},
    "韩芊雅": {"韩芊雅", "韩芊雅 Hanqianya", "Hanqianya"},
    "OLAY 玉兰油": {"OLAY", "玉兰油", "OLAY玉兰油", "OLAY 玉兰油"},
    "KLORANE 康如": {"KLORANE", "康如", "KLORANE 康如"},
    "多潘 DORPANG": {"多潘", "DORPANG", "多潘DORPANG", "多潘 DORPANG"},
    "依思佩尔": {"依思佩尔", "DHDH依思佩尔", "DHDH 依思佩尔", "EASPEER", "EASPEER 依思佩尔"},
    "VCAURORA 极光": {"VCAURORA", "VCAURORA极光", "VCAURORA 极光"},
    "澳白汀 OHBT": {"澳白汀", "OHBT", "澳白汀（OHBT", "澳白汀 OHBT"},
    "Aromatherapy Associates": {"AA", "Aromatherapy Associates", "AROMATHERAPY ASSOCIATES"},
    "康王": {"康王", "康王拜耳"},
})
BRAND_CANONICAL_BY_KEY = {
    _compact(alias): canonical
    for canonical, aliases in BRAND_CANONICAL_GROUPS.items()
    for alias in aliases | {canonical}
}
PRODUCT_CANONICAL_GROUPS = {
    "奇焕润发护发精油": {
        "奇焕润发护发精油", "奇焕润发护发精油小金瓶", "奇焕润发护发精油套装",
    },
    "盈萃韧养护发精油": {"盈萃韧养护发精油", "盈萃韧养护发精油沐光瓶"},
    "山茶花护发精油": {"山茶花", "山茶花精油", "山茶花护发精油"},
}
PRODUCT_CANONICAL_BY_KEY = {
    _compact(alias): canonical
    for canonical, aliases in PRODUCT_CANONICAL_GROUPS.items()
    for alias in aliases | {canonical}
}


def canonical_brand_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -_/·（）()")
    if not valid_brand(text):
        return ""
    return BRAND_CANONICAL_BY_KEY.get(_compact(text), text)


@lru_cache(maxsize=2)
def _content_index(stamp: int) -> dict[str, Any]:
    del stamp
    try:
        value = json.loads(CONTENT_INDEX_PATH.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def content_index() -> dict[str, Any]:
    try:
        stamp = CONTENT_INDEX_PATH.stat().st_mtime_ns
    except OSError:
        stamp = 0
    value = _content_index(stamp)
    entries = value.get("entries")
    return entries if isinstance(entries, dict) else value


def brand_vocabulary() -> list[dict[str, Any]]:
    try:
        import doubao_brand_settings
        return list(doubao_brand_settings.vocabulary())
    except (ImportError, OSError, ValueError):
        return []


def owned_brand_vocabulary() -> list[dict[str, Any]]:
    return [item for item in brand_vocabulary() if item.get("group") == "owned"]


def canonical_question(value: str) -> str:
    from monitor_core.recommendation_questions import canonical_recommendation_question
    recommendation = canonical_recommendation_question(value) if "推荐" in str(value or "") else ""
    if recommendation:
        return recommendation
    try:
        from doubao_question_aliases import canonical_question_name
        return canonical_question_name(value) or str(value or "未知问题")
    except ImportError:
        return str(value or "未知问题")


RELATED_CONTENT_HEADERS = (
    "相关视频", "相关推荐", "相关文章", "相关笔记", "相关问答",
    "相关资讯", "相关搜索", "相关内容", "参考资料", "参考链接",
    "延伸阅读", "相关视频推荐",
)


def recommendation_body(value: str) -> str:
    """Return only the assistant's recommendation prose, excluding source cards."""
    text = str(value or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    cleaned: list[str] = []
    skipping_leading_reference_titles = True
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if skipping_leading_reference_titles and re.search(r"(?:#\S+\s*)+$", stripped):
            continue
        skipping_leading_reference_titles = False
        if any(stripped.startswith(marker) for marker in RELATED_CONTENT_HEADERS):
            break
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    return result or text


def valid_brand(value: str) -> bool:
    text = str(value or "").strip()
    compact = _compact(text)
    if not compact or compact in {_compact(item) for item in BRAND_STOP} or compact in INVALID_BRAND_KEYS:
        return False
    if len(compact) < 2 or len(compact) > 40:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?(?:ml|g|mg|%)?", compact, re.I):
        return False
    if re.search(r"[>→]|\s\|\s", text):
        return False
    if any(token in compact for token in ("拼多多", "淘宝", "天猫", "京东")):
        return False
    if re.search(r"(?:优选|百货(?:店|商行)?|旗舰店|专卖店|妆品(?:小店)?|彩妆小店|甄选|严选)$", text):
        return False
    return True


def canonical_url(value: str) -> str:
    try:
        parsed = urlparse(str(value or "").strip())
        query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING and not key.lower().startswith("utm_")]
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        # HTTP and HTTPS identify the same public document for analytics.  A
        # leading www host alias is likewise presentation, not identity.
        if scheme in {"http", "https"}:
            scheme = "https"
            if netloc.startswith("www."):
                netloc = netloc[4:]
        return urlunparse((scheme, netloc, parsed.path.rstrip("/") or "/", parsed.params, urlencode(sorted(query)), ""))
    except ValueError:
        return str(value or "").strip()


def beijing_day(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BEIJING)
        return parsed.astimezone(BEIJING).date().isoformat()
    except ValueError:
        match = re.match(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else ""


def source_type(domain: str, title: str = "") -> str:
    haystack = f"{domain} {title}".lower()
    if any(item in haystack for item in VIDEO_DOMAINS) or "视频" in title:
        return "视频"
    if any(item in haystack for item in SHOP_DOMAINS):
        return "电商"
    if any(item in haystack for item in SOCIAL_DOMAINS):
        return "社交"
    return "文章"


def media_name(domain: str) -> str:
    clean = domain.lower().removeprefix("www.")
    for suffix, name in MEDIA_NAMES.items():
        if clean == suffix or clean.endswith("." + suffix):
            return name
    return clean or "未知媒体"


def normalize_source(raw: dict[str, Any]) -> dict[str, Any]:
    url = str(raw.get("url") or raw.get("href") or "")
    stable = canonical_url(url)
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    title = str(raw.get("title") or "").strip()
    kind = str(raw.get("type") or raw.get("source_type") or "").strip() or source_type(domain, title)
    return {"title": title, "url": url, "canonical_url": stable, "domain": domain,
            "media": str(raw.get("media") or "").strip() or media_name(domain), "type": kind,
            "brand_mentions": list(raw.get("brand_mentions") or []),
            "owned_brands": list(raw.get("owned_brands") or []),
            "own_products": list(raw.get("own_products") or []),
            "own_brand": bool(raw.get("own_brand")), "brand_match_scope": str(raw.get("brand_match_scope") or "")}


def source_title_key(source: dict[str, Any]) -> str:
    title = _compact(source.get("title") or "")
    if len(title) < 8:
        return ""
    generic = {
        _compact(source.get("domain") or ""),
        _compact(source.get("media") or ""),
        _compact(urlparse(str(source.get("url") or "")).netloc),
    }
    if title in generic:
        return ""
    return f"{str(source.get('domain') or '').casefold()}:{title}"


def usable_source_title(source: dict[str, Any]) -> str:
    """Return a real human title, excluding URL/domain placeholders."""
    title = str(source.get("title") or "").strip()
    if not title:
        return ""
    if title in {"标题未获取", "未获取标题", "未知标题"}:
        return ""
    compact_title = _compact(title)
    generic = {
        _compact(source.get("domain") or ""),
        _compact(source.get("media") or ""),
        _compact(urlparse(str(source.get("url") or "")).netloc),
    }
    if compact_title in generic:
        return ""
    if re.fullmatch(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/.*)?", title, re.I):
        return ""
    return title


def source_group_key(source: dict[str, Any]) -> str:
    title_key = source_title_key(source)
    if title_key:
        return f"title:{title_key}"
    return f"url:{source.get('canonical_url') or source.get('url') or ''}"


def load_doubao_runs(refs_path: Path, answers_path: Path, products_path: Path | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    source_urls: dict[str, set[str]] = {}
    source_titles: dict[str, set[str]] = {}

    def run_number(row: dict[str, Any]) -> int | None:
        try:
            value = int(str(row.get("run_no") or "").strip())
            return value if value >= 0 else None
        except (TypeError, ValueError):
            # A power loss can leave a partially written CSV row containing NUL
            # bytes. Ignore only that broken row so one interrupted write cannot
            # take the whole dashboard analytics API offline.
            return None

    if answers_path.exists():
        with answers_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sequence = run_number(row)
                if sequence is None:
                    continue
                key = str(sequence)
                grouped[key] = {"model_id": "doubao", "run_id": f"doubao-{key}", "sequence": sequence,
                    "question": canonical_question(row.get("question") or "未知问题"), "finished_at": str(row.get("captured_at") or row.get("run_time") or row.get("extracted_at") or ""),
                    "day": beijing_day(row.get("captured_at") or row.get("run_time") or ""), "serial": str(row.get("source_device") or row.get("mumu_serial") or "doubao-web"),
                    "answer": str(row.get("answer_text") or ""), "status": "success",
                    "product_review_status": str(row.get("review_status") or ""),
                    "product_analysis_model": str(row.get("model") or ""),
                    "product_reviewed_at": str(row.get("reviewed_at") or ""),
                    "sources": [], "products": [], "brands": []}
                source_urls[key] = set()
                source_titles[key] = set()
    if refs_path.exists():
        with refs_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sequence = run_number(row)
                if sequence is None:
                    continue
                key = str(sequence)
                run = grouped.setdefault(key, {"model_id": "doubao", "run_id": f"doubao-{key}", "sequence": sequence,
                    "question": canonical_question(row.get("question") or "未知问题"), "finished_at": str(row.get("captured_at") or row.get("run_time") or row.get("extracted_at") or ""),
                    "day": beijing_day(row.get("captured_at") or row.get("run_time") or ""), "serial": str(row.get("source_device") or row.get("mumu_serial") or "doubao-web"),
                    "answer": "", "status": "success", "sources": [], "products": [], "brands": []})
                source = normalize_source(row)
                seen = source_urls.setdefault(key, set())
                seen_titles = source_titles.setdefault(key, set())
                title_key = source_title_key(source)
                if not source["canonical_url"] or source["canonical_url"] in seen:
                    continue
                if title_key and title_key in seen_titles:
                    continue
                seen.add(source["canonical_url"])
                if title_key:
                    seen_titles.add(title_key)
                run["sources"].append(source)
    if products_path and products_path.exists():
        with products_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                sequence = run_number(row)
                if sequence is None:
                    continue
                run = grouped.get(str(sequence))
                if not run:
                    continue
                try:
                    product_rank = int(row.get("product_index") or 0)
                except (TypeError, ValueError):
                    product_rank = 0
                product = {"brand": str(row.get("brand_name") or "").strip(),
                           "product_name": str(row.get("product_name") or "").strip(),
                           "rank": product_rank,
                           "evidence": str(row.get("evidence") or "").strip()}
                if product["brand"] or product["product_name"]:
                    run["products"].append(product)
                    if product["brand"] and product["brand"] not in run["brands"]:
                        run["brands"].append(product["brand"])
    return sorted(grouped.values(), key=lambda item: item["sequence"])


def load_generic_runs(model_id: str, stats: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for raw in stats.get("runs") or []:
        seen = set()
        seen_titles = set()
        sources = []
        for item in raw.get("sources") or []:
            source = normalize_source(item)
            title_key = source_title_key(source)
            if not source["canonical_url"] or source["canonical_url"] in seen:
                continue
            if title_key and title_key in seen_titles:
                continue
            seen.add(source["canonical_url"])
            if title_key:
                seen_titles.add(title_key)
            sources.append(source)
        output.append({"model_id": model_id, "run_id": str(raw.get("run_id") or f"{model_id}-{len(output)+1}"),
            "sequence": int(raw.get("sequence") or raw.get("round") or len(output)+1), "question": canonical_question(raw.get("question") or "未知问题"),
            "finished_at": str(raw.get("finished_at") or ""), "day": str(raw.get("day") or beijing_day(raw.get("finished_at") or "")),
            "serial": str(raw.get("serial") or model_id), "answer": str(raw.get("web_body") or raw.get("reply") or ""),
            "status": str(raw.get("status") or "success"), "sources": sources,
            "products": list(raw.get("products") or []), "brands": list(raw.get("brands") or [])})
    return output


@lru_cache(maxsize=50000)
def _title_terms(title: str) -> tuple[str, ...]:
    terms = {item.lower() for item in re.findall(r"[A-Za-z][A-Za-z0-9+-]{2,}", title)
             if item.lower() not in ENGLISH_STOP}
    if jieba is not None:
        for token in jieba.lcut(title, cut_all=False):
            token = token.strip().casefold()
            if re.fullmatch(r"[\u4e00-\u9fff]{2,8}", token):
                terms.add(token)
    else:
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", title):
            if len(chunk) <= 8:
                terms.add(chunk)
            else:
                for width in (2, 3, 4):
                    terms.update(chunk[index:index+width] for index in range(len(chunk)-width+1))
    return tuple(terms)


def keyword_counts(titles: Iterable[str], limit: int = 18) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for title in titles:
        per_title = _title_terms(str(title or ""))
        for term in per_title:
            if term not in KEYWORD_STOP and not any(stop in term for stop in ("推荐一", "排行榜", "怎么样")):
                counts[term] += 1
    dominated: dict[str, int] = {}
    for longer, longer_count in counts.items():
        length = len(longer)
        for start in range(length):
            for end in range(start + 2, length + 1):
                fragment = longer[start:end]
                if fragment != longer and longer_count > dominated.get(fragment, 0):
                    dominated[fragment] = longer_count
    result = []
    for term, count in counts.most_common():
        if count < 2 or term[0] in PARTICLES or term[-1] in PARTICLES:
            continue
        if dominated.get(term, 0) >= count:
            continue
        result.append({"term": term, "count": count})
        if len(result) >= limit:
            break
    return result


def _top_sources(runs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for run in runs:
        for source in run["sources"]:
            normalized_kind = "视频" if source["type"] == "视频" else "文章"
            if normalized_kind != kind:
                continue
            row = grouped.setdefault(source_group_key(source), {**source, "count": 0, "run_ids": set()})
            if run["run_id"] not in row["run_ids"]:
                row["run_ids"].add(run["run_id"]); row["count"] += 1
    ranked = sorted(grouped.values(), key=lambda item: (-item["count"], item["title"], item["canonical_url"]))
    rows = []
    displayed_titles = set()
    for row in ranked:
        display_key = _compact(row.get("title") or "")
        if display_key and display_key in displayed_titles:
            continue
        if display_key:
            displayed_titles.add(display_key)
        rows.append(row)
        if len(rows) == 25:
            break
    for row in rows:
        row.pop("run_ids", None)
    return rows


def _owned_sources(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every unique owned-brand source in scope without a Top-N cutoff."""
    grouped: dict[str, dict[str, Any]] = {}
    for run in runs:
        for source in run.get("sources") or []:
            if not source.get("own_brand"):
                continue
            key = source_group_key(source)
            row = grouped.setdefault(key, {**source, "count": 0, "run_ids": set()})
            if run["run_id"] not in row["run_ids"]:
                row["run_ids"].add(run["run_id"])
                row["count"] += 1
            row["owned_brands"] = sorted(
                set(row.get("owned_brands") or []) | set(source.get("owned_brands") or [])
            )
            row["own_products"] = sorted(
                set(row.get("own_products") or []) | set(source.get("own_products") or [])
            )
            scopes = {row.get("brand_match_scope"), source.get("brand_match_scope")}
            row["brand_match_scope"] = (
                "标题+正文" if "标题+正文" in scopes or {"标题", "正文"} <= scopes
                else "标题" if "标题" in scopes else "正文" if "正文" in scopes else ""
            )
    rows = sorted(
        grouped.values(),
        key=lambda item: (-item["count"], item.get("title") or "", item.get("canonical_url") or ""),
    )
    for row in rows:
        row.pop("run_ids", None)
    return rows


COMMON_SOURCE_MODELS = ("doubao", "yuanbao", "wenxin", "deepseek", "quark")


def common_owned_source_links(
    runs_by_model: dict[str, list[dict[str, Any]]],
    *,
    question: str = "",
    date: str = "",
    exact_models: int = len(COMMON_SOURCE_MODELS),
) -> list[dict[str, Any]]:
    """Owned links cited by an exact number of the configured web models.

    A citation counts at most once per successful run. Rows are deliberately
    grouped by canonical URL rather than title so cosmetic title differences
    across models do not split one link into several records.
    """
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for model_id in COMMON_SOURCE_MODELS:
        for run in runs_by_model.get(model_id, []):
            day = str(run.get("day") or "")
            product_question = str(run.get("question") or "")
            if (question and product_question != question) or (date and day != date):
                continue
            seen_in_run: set[str] = set()
            for source in run.get("sources") or []:
                if not source.get("own_brand"):
                    continue
                url = str(source.get("canonical_url") or source.get("url") or "").strip()
                if not url or url in seen_in_run:
                    continue
                seen_in_run.add(url)
                key = (day, product_question, url)
                row = grouped.setdefault(
                    key,
                    {
                        "date": day,
                        "question": product_question,
                        "title": str(source.get("title") or url),
                        "url": str(source.get("url") or url),
                        "canonical_url": url,
                        "media": str(source.get("media") or ""),
                        "type": str(source.get("type") or "文章"),
                        "owned_brands": set(),
                        "own_products": set(),
                        "model_counts": {item: 0 for item in COMMON_SOURCE_MODELS},
                    },
                )
                row["model_counts"][model_id] += 1
                row["owned_brands"].update(source.get("owned_brands") or [])
                row["own_products"].update(source.get("own_products") or [])
    rows = []
    for row in grouped.values():
        counts = row["model_counts"]
        matched_models = [
            model_id for model_id in COMMON_SOURCE_MODELS
            if counts.get(model_id, 0) > 0
        ]
        if len(matched_models) != exact_models:
            continue
        row["matched_models"] = matched_models
        row["owned_brands"] = sorted(row["owned_brands"])
        row["own_products"] = sorted(row["own_products"])
        row["total_count"] = sum(counts.values())
        rows.append(row)
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("question") or ""),
            int(item.get("total_count") or 0),
            str(item.get("canonical_url") or ""),
        ),
        reverse=True,
    )


def competitor_brand_catalog(
    runs_by_model: dict[str, list[dict[str, Any]]],
    *,
    question: str = "",
    date: str = "",
    eligible_brands: set[str] | None = None,
) -> list[str]:
    """Relevant product brands found in successfully parsed article bodies."""
    brands: set[str] = set()
    for model_id in COMMON_SOURCE_MODELS:
        for run in runs_by_model.get(model_id, []):
            if question and str(run.get("question") or "") != question:
                continue
            if date and str(run.get("day") or "") != date:
                continue
            for source in run.get("sources") or []:
                if source.get("type") == "视频" or not source.get("body_analysis_ready"):
                    continue
                owned = set(source.get("owned_brands") or [])
                brands.update(
                    brand for brand in source.get("body_brand_mentions") or []
                    if brand and brand not in owned
                    and (eligible_brands is None or brand in eligible_brands)
                )
    return sorted(brands)


def structured_product_brand_catalog(
    runs_by_model: dict[str, list[dict[str, Any]]],
    *,
    question: str = "",
    date: str = "",
) -> set[str]:
    """Brands with an actual structured product under the active filters."""
    brands: set[str] = set()
    for runs in runs_by_model.values():
        for run in runs:
            if question and str(run.get("question") or "") != question:
                continue
            if date and str(run.get("day") or "") != date:
                continue
            for product in run.get("products") or []:
                brand, _product, _rank = _product_fields(product)
                brand = canonical_brand_name(brand)
                if brand:
                    brands.add(brand)
    return brands


def common_competitor_source_links(
    runs_by_model: dict[str, list[dict[str, Any]]],
    *,
    question: str = "",
    date: str = "",
    exact_models: int = len(COMMON_SOURCE_MODELS),
    eligible_brands: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Article links whose parsed body mentions a competitor and are cited by N models.

    Grouping includes the competitor brand. This keeps the counts correct when
    one article body discusses multiple competitors. A citation counts at most
    once per successful run, competitor and canonical URL.
    """
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for model_id in COMMON_SOURCE_MODELS:
        for run in runs_by_model.get(model_id, []):
            day = str(run.get("day") or "")
            product_question = str(run.get("question") or "")
            if (question and product_question != question) or (date and day != date):
                continue
            seen_in_run: set[tuple[str, str]] = set()
            for source in run.get("sources") or []:
                if source.get("type") == "视频" or not source.get("body_analysis_ready"):
                    continue
                url = str(source.get("canonical_url") or source.get("url") or "").strip()
                if not url:
                    continue
                owned = set(source.get("owned_brands") or [])
                competitor_brands = {
                    brand for brand in source.get("body_brand_mentions") or []
                    if brand and brand not in owned
                    and (eligible_brands is None or brand in eligible_brands)
                }
                for competitor_brand in competitor_brands:
                    citation_key = (competitor_brand, url)
                    if citation_key in seen_in_run:
                        continue
                    seen_in_run.add(citation_key)
                    key = (day, product_question, competitor_brand, url)
                    row = grouped.setdefault(
                        key,
                        {
                            "date": day,
                            "question": product_question,
                            "competitor_brand": competitor_brand,
                            "title": str(source.get("title") or url),
                            "url": str(source.get("url") or url),
                            "canonical_url": url,
                            "media": str(source.get("media") or ""),
                            "type": str(source.get("type") or "文章"),
                            "body_match_scope": "正文",
                            "model_counts": {item: 0 for item in COMMON_SOURCE_MODELS},
                        },
                    )
                    row["model_counts"][model_id] += 1
    rows = []
    for row in grouped.values():
        counts = row["model_counts"]
        matched_models = [
            model_id for model_id in COMMON_SOURCE_MODELS
            if counts.get(model_id, 0) > 0
        ]
        if len(matched_models) != exact_models:
            continue
        row["matched_models"] = matched_models
        row["total_count"] = sum(counts.values())
        rows.append(row)
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("question") or ""),
            int(item.get("total_count") or 0),
            str(item.get("competitor_brand") or ""),
            str(item.get("canonical_url") or ""),
        ),
        reverse=True,
    )


def common_all_competitor_source_links(
    runs_by_model: dict[str, list[dict[str, Any]]],
    *,
    question: str = "",
    date: str = "",
    exact_models: int = len(COMMON_SOURCE_MODELS),
    eligible_brands: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Competitor articles grouped once per URL, with all matched brands merged."""
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for model_id in COMMON_SOURCE_MODELS:
        for run in runs_by_model.get(model_id, []):
            day = str(run.get("day") or "")
            product_question = str(run.get("question") or "")
            if (question and product_question != question) or (date and day != date):
                continue
            seen_in_run: set[str] = set()
            for source in run.get("sources") or []:
                if source.get("type") == "视频" or not source.get("body_analysis_ready"):
                    continue
                url = str(source.get("canonical_url") or source.get("url") or "").strip()
                if not url or url in seen_in_run:
                    continue
                owned = set(source.get("owned_brands") or [])
                competitor_brands = {
                    brand for brand in source.get("body_brand_mentions") or []
                    if brand and brand not in owned
                    and (eligible_brands is None or brand in eligible_brands)
                }
                if not competitor_brands:
                    continue
                seen_in_run.add(url)
                key = (day, product_question, url)
                row = grouped.setdefault(
                    key,
                    {
                        "date": day,
                        "question": product_question,
                        "competitor_brand": "",
                        "competitor_brands": set(),
                        "title": str(source.get("title") or url),
                        "url": str(source.get("url") or url),
                        "canonical_url": url,
                        "media": str(source.get("media") or ""),
                        "type": str(source.get("type") or "文章"),
                        "body_match_scope": "正文",
                        "model_counts": {item: 0 for item in COMMON_SOURCE_MODELS},
                    },
                )
                row["competitor_brands"].update(competitor_brands)
                row["model_counts"][model_id] += 1
    rows = []
    for row in grouped.values():
        counts = row["model_counts"]
        matched_models = [
            model_id for model_id in COMMON_SOURCE_MODELS if counts.get(model_id, 0) > 0
        ]
        if len(matched_models) != exact_models:
            continue
        row["matched_models"] = matched_models
        row["competitor_brands"] = sorted(row["competitor_brands"])
        row["competitor_brand"] = "、".join(row["competitor_brands"])
        row["total_count"] = sum(counts.values())
        rows.append(row)
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("question") or ""),
            int(item.get("total_count") or 0),
            str(item.get("canonical_url") or ""),
        ),
        reverse=True,
    )


@lru_cache(maxsize=50000)
def canonical_product_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -_/·（）()")
    text = re.sub(r"[（(][^）)]*(?:[）)]|$)", "", text).strip()
    text = re.sub(r"护发精油乳$", "护发精油", text)
    text = re.sub(r"(?:新款|新版|升级版|升级款|升级|套装)$", "", text).strip()
    text = re.sub(r"\s*\d+(?:\.\d+)?\s*(?:版)?$", "", text).strip()
    return PRODUCT_CANONICAL_BY_KEY.get(_compact(text), text)


@lru_cache(maxsize=4096)
def _brand_product_prefix(compact_brand: str) -> re.Pattern[str] | None:
    if not compact_brand:
        return None
    return re.compile(
        r"^\s*" + r"[^0-9A-Za-z\u4e00-\u9fff]*".join(
            re.escape(char) for char in compact_brand
        ),
        re.I,
    )


@lru_cache(maxsize=100000)
def _normalized_product_fields(brand: str, product: str, rank_value: str) -> tuple[str, str, int]:
    raw_brand = str(brand or "").strip()
    try:
        rank = int(rank_value or 0)
    except (TypeError, ValueError):
        rank = 0
    if re.search(r"[>→]|\s\|\s", raw_brand):
        return "", "", rank
    brand = canonical_brand_name(raw_brand)
    product = str(product or "").strip()
    # Comparison chains are not one product.  They occasionally arrive as a
    # single upstream row ("卡诗 > 欧莱雅 > 爱茉莉") and must not enter trends.
    if re.search(r"[>→]|\s\|\s", product) or re.match(r"^(?:或|以及|与|及)\s*\S", product):
        product = ""
    brand_prefixes = [raw_brand, brand]
    if brand:
        brand_prefixes.extend(BRAND_CANONICAL_GROUPS.get(brand, set()))
    for brand_prefix in dict.fromkeys(brand_prefixes):
        prefix = _brand_product_prefix(_compact(brand_prefix))
        if prefix is None:
            continue
        for _ in range(4):
            match = prefix.match(product)
            if not match:
                break
            product = product[match.end():].lstrip(" ·-_/～|（）()")
    if brand:
        words = re.findall(r"[A-Za-z0-9]+", brand)
        if len(words) > 1 and len(words[-1]) >= 3:
            product = re.sub(r"^\s*" + re.escape(words[-1]), "", product, count=1, flags=re.I).lstrip(" ·-_/～|（）()")
    product = canonical_product_name(product.strip(" ·-_/～|（）()"))
    return brand, product, rank


def _product_fields(raw: dict[str, Any]) -> tuple[str, str, int]:
    brand = str(raw.get("brand") or raw.get("brand_name") or "").strip()
    product = str(raw.get("product_name") or raw.get("name") or "").strip()
    rank_value = str(raw.get("rank") or raw.get("product_index") or 0)
    return _normalized_product_fields(brand, product, rank_value)


def _catalogs(runs_by_model: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, set[str]], dict[str, tuple[str, str]]]:
    brand_aliases: dict[str, set[str]] = defaultdict(set)
    product_catalog: dict[str, tuple[str, str]] = {}
    canonical_by_key: dict[str, str] = {}
    raw_brands: list[str] = []

    for item in brand_vocabulary():
        canonical = canonical_brand_name(item.get("name") or "")
        if not valid_brand(canonical):
            continue
        aliases = {
            str(alias or "").strip()
            for alias in item.get("aliases") or []
            if str(alias or "").strip()
        } | {canonical}
        brand_aliases[canonical].update(aliases)
        for alias in aliases:
            key = _compact(alias)
            if key:
                canonical_by_key[key] = canonical

    # Canonical groups are reviewed vocabulary, not merely normalization rules.
    # Seed them even when a date/question-scoped query has no completed product
    # rows yet; otherwise explicit answer text such as "欧莱雅、滋源" silently
    # disappears from that question's brand coverage.
    for raw_canonical, raw_aliases in BRAND_CANONICAL_GROUPS.items():
        canonical = canonical_brand_name(raw_canonical)
        if not valid_brand(canonical):
            continue
        aliases = {
            str(alias or "").strip() for alias in raw_aliases | {raw_canonical}
            if str(alias or "").strip()
        }
        brand_aliases[canonical].update(aliases)
        for alias in aliases | {canonical}:
            key = _compact(alias)
            if key:
                canonical_by_key[key] = canonical

    for runs in runs_by_model.values():
        for run in runs:
            # A raw brand list can include advice text, source-card hashtags,
            # ingredients and product-series names. Only structured product
            # brands (plus the curated vocabulary above) may seed the matcher.
            for raw in run.get("products") or []:
                brand, _product, _rank = _product_fields(raw)
                if brand:
                    raw_brands.append(brand)

    variants: dict[str, Counter[str]] = defaultdict(Counter)
    for name in raw_brands:
        normalized = canonical_brand_name(name)
        if normalized and _compact(normalized) not in canonical_by_key:
            variants[_compact(normalized)][normalized] += 1
    for key, counts in variants.items():
        canonical_by_key[key] = sorted(
            counts,
            key=lambda name: (-counts[name], name.count(" "), len(name), name.casefold()),
        )[0]

    def add_brand(value: str) -> str:
        raw_name = str(value or "").strip()
        name = canonical_brand_name(raw_name)
        if not name:
            return ""
        key = _compact(name)
        canonical = canonical_by_key.setdefault(key, name)
        brand_aliases[canonical].add(raw_name)
        brand_aliases[canonical].add(name)
        brand_aliases[canonical].update(BRAND_CANONICAL_GROUPS.get(canonical, set()))
        return canonical
    for runs in runs_by_model.values():
        for run in runs:
            for raw in run.get("products") or []:
                brand, product, _ = _product_fields(raw)
                brand = add_brand(brand)
                label = " ".join(part for part in (brand, product) if part).strip()
                if brand and product and label:
                    product_catalog[_compact(label)] = (brand, product)
    return brand_aliases, product_catalog


def _brand_matcher(brand_aliases: dict[str, set[str]]) -> tuple[re.Pattern[str] | None, dict[str, set[str]]]:
    aliases: dict[str, set[str]] = defaultdict(set)
    for brand, values in brand_aliases.items():
        for value in values | {brand}:
            token = _compact(value)
            if len(token) >= 2:
                aliases[token].add(brand)
    if not aliases:
        return None, aliases
    pattern = re.compile("|".join(re.escape(token) for token in sorted(aliases, key=len, reverse=True)))
    return pattern, aliases


def _mentions(text: str, matcher: tuple[re.Pattern[str] | None, dict[str, set[str]]]) -> list[str]:
    pattern, aliases = matcher
    if pattern is None:
        return []
    compact_text = _compact(text)
    found: set[str] = set()
    for match in pattern.finditer(compact_text):
        token = match.group(0)
        if not re.search(r"[\u3400-\u9fff]", token):
            # The matcher already runs over compact text. Check boundaries at
            # the matched position without compacting the full answer again.
            left = compact_text[match.start() - 1:match.start()]
            right = compact_text[match.end():match.end() + 1]
            occurs = not re.match(r"[a-z0-9]", left) and not re.match(r"[a-z0-9]", right)
        elif token not in AMBIGUOUS_BRAND_CONTEXTS:
            occurs = True
        else:
            occurs = brand_alias_occurs(text, token)
        if occurs:
            found.update(aliases.get(token, ()))
    return sorted(found)


def _enrich_runs(runs_by_model: dict[str, list[dict[str, Any]]], brand_aliases: dict[str, set[str]], product_catalog: dict[str, tuple[str, str]], matcher: tuple[re.Pattern[str] | None, dict[str, set[str]]], *, enrich_sources: bool = True) -> None:
    canonical_by_key = {_compact(alias): brand for brand, aliases in brand_aliases.items() for alias in aliases | {brand}}
    owned_brand_names = {
        canonical_by_key.get(_compact(name), name)
        for item in owned_brand_vocabulary()
        if (name := canonical_brand_name(item.get("name")))
    }
    index = content_index()
    source_enrichment_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
    for runs in runs_by_model.values():
        for run in runs:
            answer = recommendation_body(run.get("answer") or "")
            # Recompute正文 brand mentions from the actual answer. Upstream
            # extractors may have produced substring collisions such as 道和
            # from 渠道和, so their cached brand list is not authoritative.
            mentioned_brands = set(_mentions(answer, matcher))
            review_status = str(run.get("product_review_status") or "").strip()
            products = [] if review_status == "ai_pending" else list(run.get("products") or [])
            if not products and review_status != "ai_pending":
                compact_answer = _compact(answer)
                for token, (brand, product) in product_catalog.items():
                    if token and token in compact_answer:
                        products.append({"brand": brand, "product_name": product, "rank": 0,
                                         "evidence": "跨模型产品词表精确命中"})
            normalized_products = []
            structured_brands: set[str] = set()
            for raw in products:
                item = dict(raw)
                brand, _product, _rank = _product_fields(item)
                brand = canonical_brand_name(brand)
                canonical = canonical_by_key.get(_compact(brand), brand) if brand else ""
                item["brand"] = canonical
                item["brand_name"] = canonical
                if canonical:
                    structured_brands.add(canonical)
                normalized_products.append(item)
            run["products"] = normalized_products
            # Brand coverage is a deterministic text metric: an explicit,
            # boundary-checked alias in the recommendation body is sufficient.
            # Do not make it depend on asynchronous product review.  Otherwise
            # ai_pending rows disappear from brand coverage and today's rate
            # changes merely because the product worker is backlogged.  Product
            # metrics remain grounded by the structured rows above.
            analysis_ready = review_status != "ai_pending"
            run["brand_analysis_ready"] = analysis_ready
            # Explicit brand mentions are deterministic answer evidence and do
            # not depend on the asynchronous product-review queue.  Keep those
            # mentions visible while structured products remain fail-closed.
            run["brands"] = sorted(mentioned_brands | structured_brands)
            if not enrich_sources:
                # Source title/body labels are normalized at ingestion and by
                # the content-index worker. Reusing them makes the selected-day
                # brand board cheap; the detached historical trend refresh
                # still performs a complete source relabel.
                continue
            for source in run.get("sources") or []:
                cache_key = (
                    str(source.get("canonical_url") or source.get("url") or ""),
                    str(source.get("title") or ""),
                    str(source.get("type") or ""),
                )
                cached_source = source_enrichment_cache.get(cache_key)
                if cached_source is not None:
                    source.update({
                        key: list(value) if isinstance(value, list) else value
                        for key, value in cached_source.items()
                    })
                    continue
                title_matches = set(_mentions(source.get("title") or "", matcher))
                body_matches: set[str] = set()
                title_products = set(own_product_mentions(source.get("title") or ""))
                body_products: set[str] = set()
                entry = index.get(source.get("url")) or index.get(source.get("canonical_url")) or {}
                is_article = source.get("type") != "视频"
                body_ready = bool(
                    is_article
                    and entry.get("status") == "ok"
                    and entry.get("extraction_quality") in {"high", "medium"}
                )
                if is_article:
                    body_matches.update(canonical_by_key.get(_compact(normalized), normalized) for item in entry.get("brand_mentions") or [] if (normalized := canonical_brand_name(item)))
                    body_matches.update(canonical_by_key.get(_compact(normalized), normalized) for item in entry.get("owned_brand_mentions") or [] if (normalized := canonical_brand_name(item)))
                    if not body_matches and entry.get("excerpt"):
                        body_matches.update(_mentions(str(entry.get("excerpt")), matcher))
                    if (
                        entry.get("status") == "ok"
                        and entry.get("extraction_quality") in {"high", "medium"}
                    ):
                        if entry.get("own_product_mentions"):
                            body_products.update(entry.get("own_product_mentions") or [])
                        elif int(entry.get("own_product_schema_version") or 0) == OWN_PRODUCT_SCHEMA_VERSION:
                            pass
                        else:
                            body_products.update(own_product_mentions(entry.get("excerpt") or ""))
                matches = title_matches | body_matches
                product_matches = sorted(title_products | body_products)
                title_owned_brands = title_matches & owned_brand_names
                body_owned_brands = body_matches & owned_brand_names
                product_owned_brands = {
                    canonical_by_key.get(_compact(brand), brand)
                    for brand in brands_for_products(product_matches)
                }
                owned = sorted(
                    product_owned_brands | title_owned_brands | body_owned_brands
                )
                source["brand_mentions"] = sorted(matches)
                source["title_brand_mentions"] = sorted(title_matches)
                source["body_brand_mentions"] = sorted(body_matches)
                source["own_products"] = product_matches
                source["title_product_mentions"] = sorted(title_products)
                source["body_product_mentions"] = sorted(body_products)
                source["owned_brands"] = owned
                source["own_brand"] = bool(owned)
                source["content_analysis_status"] = (
                    "title_only" if not is_article else
                    "complete" if body_ready else
                    "pending" if not entry or entry.get("status") not in {"error", "blocked", "unsupported"} else
                    "failed"
                )
                source["body_analysis_ready"] = body_ready
                title_owned_hit = bool(title_products or title_owned_brands)
                body_owned_hit = bool(body_products or body_owned_brands)
                source["brand_match_scope"] = "标题+正文" if title_owned_hit and body_owned_hit else "标题" if title_owned_hit else "正文" if body_owned_hit else ""
                source_enrichment_cache[cache_key] = {
                    field: list(source.get(field) or []) if isinstance(source.get(field), list) else source.get(field)
                    for field in (
                        "brand_mentions", "title_brand_mentions", "body_brand_mentions",
                        "own_products", "title_product_mentions", "body_product_mentions",
                        "owned_brands", "own_brand", "content_analysis_status",
                        "body_analysis_ready", "brand_match_scope",
                    )
                }


def prepare_analytics(
    runs_by_model: dict[str, list[dict[str, Any]]],
    *, enrich_sources: bool = True,
) -> tuple[
    dict[str, set[str]],
    dict[str, tuple[str, str]],
    tuple[re.Pattern[str] | None, dict[str, set[str]]],
]:
    """Enrich one versioned run snapshot for reuse across dashboard filters."""
    brand_aliases, product_catalog = _catalogs(runs_by_model)
    matcher = _brand_matcher(brand_aliases)
    _enrich_runs(
        runs_by_model, brand_aliases, product_catalog, matcher,
        enrich_sources=enrich_sources,
    )
    return brand_aliases, product_catalog, matcher


def _dense_ranks(counts: Counter[str]) -> dict[str, int]:
    rank_map: dict[str, int] = {}
    previous = None
    rank = 0
    for position, (name, count) in enumerate(sorted(counts.items(), key=lambda item: (-item[1], item[0])), 1):
        if count != previous:
            rank = position
            previous = count
        rank_map[name] = rank
    return rank_map


def _daily_mentions(runs: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    days = sorted({run.get("day") for run in runs if run.get("day")})
    result = []
    previous_ranks: dict[str, int] = {}
    for day in days:
        day_runs = [run for run in runs if run.get("day") == day]
        # Brand coverage can be evaluated directly from every answer body.
        # Product coverage requires completed structured product review, so a
        # queued review is unknown and must not be counted as a negative run.
        eligible_runs = (
            day_runs if field == "brands" else
            [run for run in day_runs if bool(run.get("brand_analysis_ready", True))]
        )
        counts: Counter[str] = Counter()
        rank_values: dict[str, list[int]] = defaultdict(list)
        for run in eligible_runs:
            seen = set()
            if field == "brands":
                values = [(str(name), 0) for name in run.get("brands") or []]
            else:
                values = []
                for raw in run.get("products") or []:
                    brand, product, rank = _product_fields(raw)
                    if brand and product:
                        values.append((" ".join(part for part in (brand, product) if part).strip(), rank))
            for name, rank in values:
                if not name or name in seen:
                    continue
                seen.add(name); counts[name] += 1
                if rank:
                    rank_values[name].append(rank)
        ranks = _dense_ranks(counts)
        rows = []
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            current_rank = ranks[name]
            previous_rank = previous_ranks.get(name)
            rows.append({"name": name, "mentions": count, "mention_rate": round(count * 100 / len(eligible_runs), 2) if eligible_runs else 0,
                         "rank": current_rank, "rank_change": (previous_rank - current_rank) if previous_rank else None,
                         "average_position": round(sum(rank_values[name]) / len(rank_values[name]), 2) if rank_values[name] else None})
        result.append({
            "date": day,
            "runs": len(eligible_runs),
            "total_runs": len(day_runs),
            "pending_runs": len(day_runs) - len(eligible_runs),
            "items": rows,
        })
        previous_ranks = ranks
    return result


def daily_owned_product_recommendations(
    runs_by_model: dict[str, list[dict[str, Any]]],
    model_ids: Iterable[str],
    *,
    question: str = "",
    date: str = "",
    day_limit: int = 7,
) -> list[dict[str, Any]]:
    """Build a fail-closed daily owned-product recommendation matrix.

    A single explicit brand + product mention in the recommendation prose, or
    a verified structured product row, is enough to mark a product as listed.
    Raw prose matching is intentionally limited to the owned products configured
    for that exact question.  Zero recommendations become authoritative only
    after every eligible run is reviewed.  Missing collection and pending review
    are separate states so the dashboard never turns missing evidence into a
    negative conclusion.
    """
    selected_ids = list(model_ids)
    scoped_by_model: dict[str, list[dict[str, Any]]] = {}
    all_days: set[str] = set()
    for model_id in selected_ids:
        scoped = [
            run for run in runs_by_model.get(model_id, [])
            if str(run.get("status") or "success").casefold() == "success"
            and run.get("body_capture_complete") is not False
            if (not question or str(run.get("question") or "") == question)
            and (not date or str(run.get("day") or "") == date)
            and owned_products_for_question(str(run.get("question") or ""))
        ]
        scoped_by_model[model_id] = scoped
        all_days.update(str(run.get("day") or "") for run in scoped if run.get("day"))
    visible_days = sorted(all_days, reverse=True)[:max(1, day_limit)]

    rows: list[dict[str, Any]] = []
    for day in visible_days:
        questions = sorted({
            str(run.get("question") or "")
            for runs in scoped_by_model.values() for run in runs
            if str(run.get("day") or "") == day
        })
        for product_question in questions:
            for owned_product in owned_products_for_question(product_question):
                model_statuses: dict[str, dict[str, Any]] = {}
                for model_id in selected_ids:
                    eligible = [
                        run for run in scoped_by_model.get(model_id, [])
                        if str(run.get("day") or "") == day
                        and str(run.get("question") or "") == product_question
                    ]
                    reviewed = [
                        run for run in eligible
                        if bool(run.get("brand_analysis_ready", True))
                    ]
                    body_match_run_ids: set[str] = set()
                    structured_match_run_ids: set[str] = set()
                    # The answer body is primary evidence for this board.  It
                    # must not disappear merely because asynchronous product AI
                    # review is backlogged.  own_product_mentions is conservative:
                    # both the owned brand and a configured product descriptor
                    # must occur together.  The question mapping above prevents
                    # a product from being credited under the wrong category.
                    for run in eligible:
                        body_mentions = set(owned_product_recommendations(
                            recommendation_body(run.get("answer") or ""),
                            owned_products_for_question(product_question),
                        ))
                        if owned_product in body_mentions:
                            body_match_run_ids.add(str(run.get("run_id") or id(run)))
                    for run in reviewed:
                        mentioned: set[str] = set()
                        for raw_product in run.get("products") or []:
                            brand, product_name, _rank = _product_fields(raw_product)
                            mentioned.update(own_product_mentions(f"{brand} {product_name}"))
                        if owned_product in mentioned:
                            structured_match_run_ids.add(str(run.get("run_id") or id(run)))
                    listed_run_ids = body_match_run_ids | structured_match_run_ids
                    listed_runs = len(listed_run_ids)
                    eligible_count = len(eligible)
                    reviewed_count = len(reviewed)
                    pending_count = eligible_count - reviewed_count
                    if listed_runs:
                        state = "listed"
                    elif not eligible_count:
                        state = "not_collected"
                    elif pending_count:
                        state = "pending"
                    else:
                        state = "not_listed"
                    model_statuses[model_id] = {
                        "state": state,
                        "eligible_runs": eligible_count,
                        "reviewed_runs": reviewed_count,
                        "pending_runs": pending_count,
                        "recommendation_runs": listed_runs,
                        "body_match_runs": len(body_match_run_ids),
                        "structured_match_runs": len(structured_match_run_ids),
                        "recommendation_rate": round(
                            listed_runs * 100 / eligible_count, 2
                        ) if eligible_count else 0,
                    }
                rows.append({
                    "date": day,
                    "question": product_question,
                    "product": owned_product,
                    "brand": owned_product_brand(owned_product),
                    "models": model_statuses,
                    "listed_model_count": sum(
                        status["state"] == "listed" for status in model_statuses.values()
                    ),
                })
    return rows


def _daily_source_analysis(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for day in sorted({run.get("day") for run in runs if run.get("day")}):
        day_runs = [run for run in runs if run.get("day") == day]
        sources = [source for run in day_runs for source in run.get("sources") or []]
        articles = [source for source in sources if source.get("type") != "视频"]
        videos = [source for source in sources if source.get("type") == "视频"]
        branded = [source for source in sources if source.get("brand_mentions")]
        title_branded = [source for source in sources if source.get("title_brand_mentions")]
        owned = [source for source in sources if source.get("own_brand")]
        body_ready = [source for source in sources if source.get("type") == "视频" or source.get("body_analysis_ready")]
        body_failed = [
            source for source in sources
            if source.get("type") != "视频" and source.get("content_analysis_status") == "failed"
        ]
        body_pending = [
            source for source in sources
            if source.get("type") != "视频"
            and not source.get("body_analysis_ready")
            and source.get("content_analysis_status") != "failed"
        ]
        brand_eligible = [
            source for source in sources
            if source.get("type") == "视频"
            or source.get("body_analysis_ready")
            or source.get("title_brand_mentions")
        ]
        owned_eligible = [
            source for source in sources
            if source.get("type") == "视频"
            or source.get("body_analysis_ready")
            or source.get("own_brand")
        ]
        titled = [source for source in sources if usable_source_title(source)]
        output.append({"date": day, "runs": len(day_runs), "sources": len(sources),
                       "body_ready_sources": len(body_ready),
                       "body_pending_sources": len(body_pending),
                       "body_failed_sources": len(body_failed),
                       "branded_eligible_sources": len(brand_eligible),
                       "branded_sources": len(branded), "branded_source_rate": round(len(branded) * 100 / len(brand_eligible), 2) if brand_eligible else 0,
                       "title_eligible_sources": len(titled),
                       "title_branded_sources": len(title_branded), "title_branded_source_rate": round(len(title_branded) * 100 / len(titled), 2) if titled else 0,
                       "owned_eligible_sources": len(owned_eligible),
                       "owned_sources": len(owned), "owned_source_rate": round(len(owned) * 100 / len(owned_eligible), 2) if owned_eligible else 0,
                       "article_keywords": keyword_counts((usable_source_title(item) for item in articles), 12),
                       "video_keywords": keyword_counts((usable_source_title(item) for item in videos), 12)})
    return output


def daily_brand_source_mentions(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Daily per-brand coverage across unique cited source links."""
    output = []
    for day in sorted({run.get("day") for run in runs if run.get("day")}):
        day_runs = [run for run in runs if run.get("day") == day]
        unique_sources: dict[str, dict[str, Any]] = {}
        for run in day_runs:
            for source in run.get("sources") or []:
                key = str(source.get("canonical_url") or source.get("url") or "").strip()
                if key:
                    unique_sources.setdefault(key, source)
        counts: Counter[str] = Counter()
        for source in unique_sources.values():
            counts.update(set(str(name) for name in source.get("brand_mentions") or [] if str(name).strip()))
        total = len(unique_sources)
        def per_brand_coverage(name: str) -> tuple[int, int, int]:
            eligible = 0
            failed = 0
            for source in unique_sources.values():
                evaluable = bool(
                    source.get("type") == "视频"
                    or source.get("body_analysis_ready")
                    or name in set(source.get("title_brand_mentions") or [])
                )
                if evaluable:
                    eligible += 1
                elif source.get("content_analysis_status") == "failed":
                    failed += 1
            return eligible, max(0, total - eligible - failed), failed
        output.append({
            "date": day,
            "sources": total,
            "items": [
                {
                    "name": name,
                    "mentions": count,
                    # A video is fully evaluable from its title.  An article is
                    # evaluable after its body archive is ready, or immediately
                    # for a brand already present in its title.  Pending/failed
                    # article bodies with no title hit are unknown, not negative.
                    "eligible_sources": (coverage := per_brand_coverage(name))[0],
                    "pending_sources": coverage[1],
                    "failed_sources": coverage[2],
                    "mention_rate": round(count * 100 / coverage[0], 2) if coverage[0] else 0,
                }
                for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            ],
        })
    return output


def _daily_source_top(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"date": day, "top_articles": _top_sources([run for run in runs if run.get("day") == day], "文章"),
             "top_videos": _top_sources([run for run in runs if run.get("day") == day], "视频")}
            for day in sorted({run.get("day") for run in runs if run.get("day")}, reverse=True)]


def doubao_owned_video_category_share(
    runs: list[dict[str, Any]], *, question: str = "", date: str = "",
) -> dict[str, Any]:
    """Measure owned-brand video URLs against all unique URLs per category."""
    buckets: dict[str, dict[str, Any]] = {}
    observed_days: set[str] = set()
    for run in runs:
        run_question = str(run.get("question") or "未知问题")
        run_day = str(run.get("day") or "")
        if question and run_question != question:
            continue
        if date and run_day != date:
            continue
        if run_day:
            observed_days.add(run_day)
        bucket = buckets.setdefault(run_question, {
            "all_urls": set(),
            "video_urls": set(),
            "owned_video_urls": set(),
            "owned_video_refs": 0,
            "owned_brands": set(),
        })
        for source in run.get("sources") or []:
            url = str(source.get("canonical_url") or source.get("url") or "").strip()
            if not url:
                continue
            bucket["all_urls"].add(url)
            if "视频" not in str(source.get("type") or ""):
                continue
            bucket["video_urls"].add(url)
            owned = bool(
                source.get("own_brand")
                or source.get("owned_brands")
                or source.get("own_products")
            )
            if not owned:
                continue
            bucket["owned_video_urls"].add(url)
            bucket["owned_video_refs"] += 1
            bucket["owned_brands"].update(
                str(item).strip()
                for item in source.get("owned_brands") or []
                if str(item).strip()
            )

    rows = []
    for category, bucket in buckets.items():
        all_links = len(bucket["all_urls"])
        video_links = len(bucket["video_urls"])
        owned_video_links = len(bucket["owned_video_urls"])
        rows.append({
            "category": category,
            "all_unique_links": all_links,
            "video_unique_links": video_links,
            "owned_video_unique_links": owned_video_links,
            "owned_video_refs": bucket["owned_video_refs"],
            "owned_video_link_share": round(owned_video_links / all_links * 100, 2) if all_links else 0,
            "owned_within_video_link_share": round(owned_video_links / video_links * 100, 2) if video_links else 0,
            "owned_brands": sorted(bucket["owned_brands"]),
        })
    rows.sort(key=lambda item: (-item["owned_video_link_share"], item["category"]))
    return {
        "rows": rows,
        "first_date": min(observed_days) if observed_days else "",
        "last_date": max(observed_days) if observed_days else "",
        "definitions": {
            "primary": "命中自有品牌或自有产品的视频唯一链接数 ÷ 该品类全部唯一信源链接数。",
            "within_video": "命中自有品牌或自有产品的视频唯一链接数 ÷ 该品类全部视频唯一链接数。",
            "deduplication": "链接按标准化URL去重；同一链接跨轮重复出现只计一个链接。",
        },
    }


def build_analytics(
    model_meta: dict[str, dict[str, Any]],
    runs_by_model: dict[str, list[dict[str, Any]]],
    *,
    question: str = "",
    date: str = "",
    model: str = "",
    view: str = "",
    prepared: tuple[
        dict[str, set[str]],
        dict[str, tuple[str, str]],
        tuple[re.Pattern[str] | None, dict[str, set[str]]],
    ] | None = None,
) -> dict[str, Any]:
    full = not view
    need_daily = full or view == "overview"
    need_source_types = full or view in {"overview", "compare"}
    need_media = full or view == "compare"
    need_top = full or view in {"compare", "sources"}
    need_keywords = full or view in {"sources", "brands"}
    need_brand = full or view in {"brands", "brand-trends"}
    need_runs = full or view == "runs"
    if prepared is None:
        brand_aliases, product_catalog = _catalogs(runs_by_model)
        matcher = _brand_matcher(brand_aliases)
        _enrich_runs(runs_by_model, brand_aliases, product_catalog, matcher)
    else:
        brand_aliases, product_catalog, matcher = prepared
    selected_models = [model] if model and model in runs_by_model else list(model_meta)
    def production_complete(run: dict[str, Any]) -> bool:
        """Keep auditable raw rows, but exclude known partial captures from KPIs."""
        # Wenxin answers are source-backed.  Older collectors could incorrectly
        # label a selector miss as a complete 0/0 capture; retain those raw rows
        # for audit, but never let them distort production dashboards.
        wenxin_has_sources = not (
            str(run.get("model_id") or "").casefold() == "wenxin"
            and not (run.get("sources") or [])
            and run.get("source_capture_complete") is True
        )
        return (
            str(run.get("status") or "success").casefold() == "success"
            and run.get("body_capture_complete") is not False
            and run.get("source_capture_complete") is not False
            and wenxin_has_sources
        )

    scope_runs = [
        run
        for model_id in selected_models
        for run in runs_by_model.get(model_id, [])
        if production_complete(run)
    ]
    all_questions = sorted({run["question"] for run in scope_runs})
    date_scope = [
        run for run in scope_runs
        if not question or run["question"] == question
    ]
    all_dates = sorted(
        {run["day"] for run in date_scope if run["day"]},
        reverse=True,
    )
    models = []
    for model_id in selected_models:
        raw_runs = [
            run for run in runs_by_model.get(model_id, [])
            if production_complete(run)
        ]
        runs = [run for run in raw_runs if (not question or run["question"] == question) and (not date or run["day"] == date)]
        trend_runs = [run for run in raw_runs if not question or run["question"] == question]
        sources = [source for run in runs for source in run["sources"]]
        article_titles = [usable_source_title(source) for source in sources if source["type"] != "视频" and usable_source_title(source)] if need_keywords else []
        video_titles = [usable_source_title(source) for source in sources if source["type"] == "视频" and usable_source_title(source)] if need_keywords else []
        by_question = []
        if full:
            for item in sorted({run["question"] for run in runs}):
                question_runs = [run for run in runs if run["question"] == item]
                question_sources = [source for run in question_runs for source in run["sources"]]
                by_question.append({"question": item, "runs": len(question_runs), "sources": len(question_sources),
                    "unique_sources": len({source["canonical_url"] for source in question_sources}),
                    "avg_sources": round(len(question_sources)/len(question_runs), 2) if question_runs else 0})
        daily = []
        if need_daily:
            for day in sorted({run["day"] for run in runs if run["day"]}, reverse=True):
                day_runs = [run for run in runs if run["day"] == day]
                day_sources = [source for run in day_runs for source in run["sources"]]
                daily.append({"date": day, "runs": len(day_runs), "sources": len(day_sources), "unique_sources": len({source["canonical_url"] for source in day_sources})})
        brand_trend_daily = _daily_mentions(trend_runs, "brands") if need_brand else []
        product_trend_daily = _daily_mentions(trend_runs, "products") if need_brand else []
        # With no date filter, current-scope and trend rows are identical. Reuse
        # the calculation; the client projection omits the duplicate arrays on
        # dedicated brand views while the core result keeps its stable shape.
        brand_daily = (_daily_mentions(runs, "brands") if date else brand_trend_daily) if need_brand else []
        product_daily = (_daily_mentions(runs, "products") if date else product_trend_daily) if need_brand else []
        source_brand_daily = _daily_source_analysis(runs) if need_brand else []
        source_body_ready_count = sum(row["body_ready_sources"] for row in source_brand_daily)
        source_body_pending_count = sum(row["body_pending_sources"] for row in source_brand_daily)
        source_body_failed_count = sum(row["body_failed_sources"] for row in source_brand_daily)
        owned_source_eligible_count = sum(row["owned_eligible_sources"] for row in source_brand_daily)
        branded_source_eligible_count = sum(row["branded_eligible_sources"] for row in source_brand_daily)
        models.append({**model_meta[model_id], "runs": len(runs), "sources": len(sources), "unique_sources": len({source["canonical_url"] for source in sources}),
            "analysis_ready_runs": sum(bool(run.get("brand_analysis_ready", True)) for run in runs),
            "analysis_pending_runs": sum(not bool(run.get("brand_analysis_ready", True)) for run in runs),
            "question_count": len({run["question"] for run in runs}), "device_count": len({run["serial"] for run in runs}),
            "source_types": ([{"name": name, "count": count} for name, count in Counter(source["type"] for source in sources).most_common()] if need_source_types else []),
            "media": ([{"name": name, "count": count} for name, count in Counter(source["media"] for source in sources).most_common(15)] if need_media else []),
            "daily": daily, "questions": by_question,
            "top_articles": _top_sources(runs, "文章") if need_top else [], "top_videos": _top_sources(runs, "视频") if need_top else [],
            "owned_sources": _owned_sources(runs) if need_top else [],
            "article_keywords": keyword_counts(article_titles) if need_keywords else [], "video_keywords": keyword_counts(video_titles) if need_keywords else [],
            "brand_daily": brand_daily, "product_daily": product_daily,
            "brand_trend_daily": brand_trend_daily,
            "product_trend_daily": product_trend_daily,
            "brand_source_daily": daily_brand_source_mentions(trend_runs) if need_brand else [],
            "source_brand_daily": source_brand_daily,
            "daily_source_top": _daily_source_top(runs) if (full or view == "sources") else [],
            "owned_source_count": sum(bool(source.get("own_brand")) for source in sources),
            "owned_source_eligible_count": owned_source_eligible_count,
            "branded_source_count": sum(bool(source.get("brand_mentions")) for source in sources),
            "branded_source_eligible_count": branded_source_eligible_count,
            "source_body_ready_count": source_body_ready_count,
            "source_body_pending_count": source_body_pending_count,
            "source_body_failed_count": source_body_failed_count,
            "recent_runs": sorted(runs, key=lambda item: item["finished_at"], reverse=True)[:30] if need_runs else []})
    # The all-date source view fetches its exact full-history intersections
    # from the indexed `/source-intersections` endpoint.  Recomputing them here
    # over the bounded latest-day detail window was both slow and misleading.
    need_inline_source_intersections = full or (view == "sources" and bool(date))
    common_links = (
        common_owned_source_links(runs_by_model, question=question, date=date, exact_models=len(COMMON_SOURCE_MODELS))
        if need_inline_source_intersections
        else []
    )
    two_model_links = (
        common_owned_source_links(runs_by_model, question=question, date=date, exact_models=2)
        if need_inline_source_intersections
        else []
    )
    owned_product_daily = (
        daily_owned_product_recommendations(
            runs_by_model, selected_models, question=question, date=date,
        )
        if full or view == "overview"
        else []
    )
    eligible_competitor_brands = (
        structured_product_brand_catalog(runs_by_model, question=question, date=date)
        if need_inline_source_intersections
        else set()
    )
    competitor_brands = (
        competitor_brand_catalog(
            runs_by_model, question=question, date=date,
            eligible_brands=eligible_competitor_brands,
        )
        if need_inline_source_intersections
        else []
    )
    common_competitor_links = (
        common_competitor_source_links(
            runs_by_model, question=question, date=date, exact_models=len(COMMON_SOURCE_MODELS),
            eligible_brands=eligible_competitor_brands,
        )
        if need_inline_source_intersections
        else []
    )
    two_model_competitor_links = (
        common_competitor_source_links(
            runs_by_model, question=question, date=date, exact_models=2,
            eligible_brands=eligible_competitor_brands,
        )
        if need_inline_source_intersections
        else []
    )
    common_all_competitor_links = (
        common_all_competitor_source_links(
            runs_by_model, question=question, date=date, exact_models=len(COMMON_SOURCE_MODELS),
            eligible_brands=eligible_competitor_brands,
        )
        if need_inline_source_intersections
        else []
    )
    two_model_all_competitor_links = (
        common_all_competitor_source_links(
            runs_by_model, question=question, date=date, exact_models=2,
            eligible_brands=eligible_competitor_brands,
        )
        if need_inline_source_intersections
        else []
    )
    owned_video_category_share = (
        doubao_owned_video_category_share(
            runs_by_model.get("doubao", []), question=question, date=date,
        )
        if model in {"", "doubao"}
        else {"rows": [], "first_date": "", "last_date": "", "definitions": {}}
    )
    return {"generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"), "filters": {"model": model, "question": question, "date": date, "view": view},
            "models": models, "model_catalog": list(model_meta.values()), "questions": all_questions, "dates": all_dates,
            "common_owned_sources": common_links,
            "two_model_owned_sources": two_model_links,
            "owned_product_daily": owned_product_daily,
            "competitor_brands": competitor_brands,
            "common_competitor_sources": common_competitor_links,
            "two_model_competitor_sources": two_model_competitor_links,
            "common_all_competitor_sources": common_all_competitor_links,
            "two_model_all_competitor_sources": two_model_all_competitor_links,
            "doubao_owned_video_category_share": owned_video_category_share,
            "analysis_method": {"brand_product": "结构化结果优先，跨模型词表精确补全", "source_brand": "视频仅标题；文章标题加已归档正文", "keywords": "本地确定性分词与停用词过滤", "llm_tokens": 0}}
