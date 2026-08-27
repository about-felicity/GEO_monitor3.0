from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs", "eyebrow_eyelash_source_mix_20260723")
PYDEPS_DIR = os.path.join(OUTPUT_DIR, "pydeps")
if PYDEPS_DIR not in sys.path:
    sys.path.insert(0, PYDEPS_DIR)

import jieba  # type: ignore

import doubao_dashboard_server as dashboard


TARGETS = {
    "推荐眉毛增长液": {
        "category": "眉毛精华液",
        "product": "梵玢眉毛精华液",
    },
    "睫毛增长液推荐": {
        "category": "睫毛精华液",
        "product": "梵玢睫毛精华液",
    },
}

THEME_PATTERNS = {
    "榜单/推荐": ("推荐", "榜单", "排行", "排名", "top", "好物", "好用", "首选", "值得买", "闭眼入"),
    "测评/实测": ("测评", "实测", "亲测", "试用", "体验", "真实", "对比", "横评", "效果验证"),
    "安全/温和": ("安全", "温和", "不刺激", "无刺激", "敏感", "无激素", "无前列腺素", "副作用"),
    "功效/增长": ("增长", "生长", "增密", "浓密", "变长", "强韧", "改善稀疏", "养护", "滋养"),
    "成分/科学": ("成分", "配方", "肽", "pdrn", "科学", "研究", "临床", "原理", "机制"),
    "教程/周期": ("怎么用", "用法", "教程", "正确使用", "坚持", "几天", "周期", "多久", "早晚"),
    "避雷/风险": ("避雷", "别买", "踩雷", "智商税", "风险", "慎用", "停用", "拔草"),
    "价格/性价比": ("平价", "价格", "性价比", "便宜", "贵", "大牌平替", "学生党"),
    "年份/新鲜度": ("2026", "2025", "最新", "新款", "今年", "年度"),
    "痛点场景": ("稀疏", "秃眉", "短睫毛", "断裂", "脱落", "空缺", "天生少", "手残党"),
}

DOMAIN_PHRASES = sorted(
    {
        phrase
        for phrases in THEME_PATTERNS.values()
        for phrase in phrases
    }
    | {
        "梵玢眉毛精华液",
        "梵玢睫毛精华液",
        "眉毛精华液",
        "睫毛精华液",
        "眉毛增长液",
        "睫毛增长液",
        "眉毛生长液",
        "睫毛生长液",
        "前列腺素",
        "多肽",
        "鱼子酱",
        "敏感眼",
        "敏感肌",
    },
    key=len,
    reverse=True,
)

STOPWORDS = {
    "的", "了", "和", "与", "及", "或", "是", "在", "有", "用", "也", "都", "就", "让",
    "一个", "一种", "这些", "这个", "什么", "怎么", "为什么", "哪些", "哪款", "真的",
    "产品", "品牌", "精华", "液", "推荐", "热门", "分享", "合集", "盘点", "介绍",
    "眉毛", "睫毛", "增长液", "生长液", "精华液", "眉毛精华液", "睫毛精华液",
    "眉毛增长液", "睫毛增长液", "眉毛生长液", "睫毛生长液",
    "梵玢", "梵玢眉毛精华液", "梵玢睫毛精华液",
    "梵玢眉毛增长液", "梵玢睫毛增长液", "fbcy",
    "可以", "没有", "使用", "效果", "实用", "相关", "视频", "文章",
    "资讯", "综合", "日报", "新闻网", "健康网", "大河", "咸宁",
    "看着", "几点", "不会",
    "2024", "2023", "2022", "2021", "2020",
}

for phrase in DOMAIN_PHRASES:
    jieba.add_word(phrase, freq=500000)


def read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def compact_text(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\u200b\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def title_tokens(title: str) -> list[str]:
    text = compact_text(title).casefold()
    hashtags = [
        token.strip("#＃ ")
        for token in re.findall(r"[#＃][^#＃\s，。！？、；：,.;:!?]{2,20}", text)
    ]
    cleaned = re.sub(r"[#＃]", " ", text)
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9+.-]+", " ", cleaned)
    tokens: list[str] = []
    for token in list(jieba.cut(cleaned, cut_all=False)) + hashtags:
        token = token.strip(" .-_+")
        if not token or token in STOPWORDS:
            continue
        if token.isdigit() and token not in {"2025", "2026"}:
            continue
        if re.fullmatch(r"[a-z]", token):
            continue
        if len(token) < 2:
            continue
        tokens.append(token)
    return tokens


def title_themes(title: str) -> list[str]:
    folded = compact_text(title).casefold()
    return [
        theme
        for theme, patterns in THEME_PATTERNS.items()
        if any(pattern.casefold() in folded for pattern in patterns)
    ]


def source_kind(source_type: str, href: str) -> str:
    folded_type = str(source_type or "")
    folded_url = str(href or "").casefold()
    if "视频" in folded_type or any(
        token in folded_url
        for token in ("douyin", "iesdouyin", "tiktok", "bilibili", "kuaishou", "youtube", "xigua")
    ):
        return "视频"
    if "文章" in folded_type:
        return "文章"
    return "其他"


def content_entry(content_index: dict, href: str) -> dict:
    entries = content_index.get("entries") if isinstance(content_index, dict) else {}
    if not isinstance(entries, dict):
        return {}
    return entries.get(str(href or "").strip()) or {}


def body_matches_product(entry: dict, product: str) -> bool:
    if (
        not isinstance(entry, dict)
        or entry.get("status") != "ok"
        or entry.get("extraction_quality") not in ("high", "medium")
    ):
        return False
    if dashboard.safe_int(entry.get("own_product_schema_version")) == dashboard.OWN_PRODUCT_SCHEMA_VERSION:
        return product in (entry.get("own_product_mentions") or [])
    return product in dashboard.own_product_mentions(entry.get("excerpt") or "")


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def two_proportion_stats(a: int, n_a: int, b: int, n_b: int) -> tuple[float, float, float]:
    if n_a <= 0 or n_b <= 0:
        return 0.0, 1.0, 0.0
    p_a = a / n_a
    p_b = b / n_b
    pooled = (a + b) / (n_a + n_b)
    variance = pooled * (1.0 - pooled) * (1.0 / n_a + 1.0 / n_b)
    z = (p_a - p_b) / math.sqrt(variance) if variance > 0 else 0.0
    p_value = 2.0 * (1.0 - normal_cdf(abs(z)))
    log_odds = math.log((a + 0.5) / (n_a - a + 0.5)) - math.log(
        (b + 0.5) / (n_b - b + 0.5)
    )
    return z, max(0.0, min(1.0, p_value)), log_odds


def js_divergence(left: Counter, right: Counter) -> float:
    vocab = set(left) | set(right)
    total_left = sum(left.values())
    total_right = sum(right.values())
    if not vocab or total_left <= 0 or total_right <= 0:
        return 0.0
    p = {word: left[word] / total_left for word in vocab}
    q = {word: right[word] / total_right for word in vocab}
    result = 0.0
    for word in vocab:
        midpoint = 0.5 * (p[word] + q[word])
        if p[word] > 0:
            result += 0.5 * p[word] * math.log2(p[word] / midpoint)
        if q[word] > 0:
            result += 0.5 * q[word] * math.log2(q[word] / midpoint)
    return result


def top_distribution_shifts(left: Counter, right: Counter, limit: int = 5) -> tuple[str, str]:
    vocab = set(left) | set(right)
    total_left = sum(left.values()) or 1
    total_right = sum(right.values()) or 1
    changes = [
        (word, right[word] / total_right - left[word] / total_left)
        for word in vocab
    ]
    rises = sorted(changes, key=lambda item: item[1], reverse=True)[:limit]
    falls = sorted(changes, key=lambda item: item[1])[:limit]
    rise_text = "；".join(f"{word} +{delta:.1%}" for word, delta in rises if delta > 0)
    fall_text = "；".join(f"{word} {delta:.1%}" for word, delta in falls if delta < 0)
    return rise_text, fall_text


def tfidf_rows(records: list[dict]) -> list[dict]:
    rows: list[dict] = []
    groups: dict[tuple[str, str], list[list[str]]] = defaultdict(list)
    for record in records:
        groups[(record["品类"], record["信源类型"])].append(record["_tokens"])
    for (category, medium), docs in sorted(groups.items()):
        document_count = len(docs)
        df = Counter()
        tf = Counter()
        score_sum = Counter()
        for tokens in docs:
            token_counts = Counter(tokens)
            token_total = sum(token_counts.values()) or 1
            df.update(token_counts.keys())
            tf.update(token_counts)
            for token, count in token_counts.items():
                score_sum[token] += count / token_total
        candidates = []
        for token, doc_freq in df.items():
            if doc_freq < 2:
                continue
            inverse_doc_freq = math.log((document_count + 1) / (doc_freq + 1)) + 1
            mean_tfidf = score_sum[token] * inverse_doc_freq / document_count
            candidates.append((token, mean_tfidf, doc_freq, tf[token]))
        candidates.sort(key=lambda item: (item[1], item[2], item[3]), reverse=True)
        for rank, (token, score, doc_freq, term_freq) in enumerate(candidates[:40], start=1):
            rows.append(
                {
                    "品类": category,
                    "信源类型": medium,
                    "排名": rank,
                    "关键词": token,
                    "TF-IDF均值": score,
                    "标题覆盖数": doc_freq,
                    "标题覆盖率": doc_freq / document_count if document_count else 0,
                    "总词频": term_freq,
                    "标题样本数": document_count,
                }
            )
    return rows


def build_analysis() -> dict:
    ai_cache = read_json(dashboard.AI_CACHE_PATH)
    meta_cache = read_json(dashboard.META_CACHE_PATH)
    content_index = read_json(dashboard.CONTENT_INDEX_PATH)

    details: list[dict] = []
    data_quality = defaultdict(Counter)
    category_day_total = Counter()

    with open(dashboard.CSV_PATH, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            question = dashboard.question_for(row)
            target = TARGETS.get(question)
            if not target or dashboard.is_quarantined_source_row(row):
                continue

            category = target["category"]
            product = target["product"]
            day = dashboard.date_for(row)
            category_day_total[(category, day)] += 1
            data_quality[category]["品类全部信源行"] += 1

            href = str(row.get("href") or "").strip()
            title = compact_text(row.get("title"))
            entry = content_entry(content_index, href)
            title_match = product in dashboard.own_product_mentions(title)
            body_match = body_matches_product(entry, product)
            body_ok = (
                entry.get("status") == "ok"
                and entry.get("extraction_quality") in ("high", "medium")
            )
            if body_ok:
                data_quality[category]["正文已归档行"] += 1
            else:
                data_quality[category]["正文未可靠归档行"] += 1
            if not title_match and not body_ok:
                data_quality[category]["未验证潜在漏计行"] += 1
            if not title_match and body_match:
                data_quality[category]["正文补充命中行"] += 1
            if not (title_match or body_match):
                continue

            source_type, media, host, source_note = dashboard.source_for(row, ai_cache, meta_cache)
            kind = source_kind(source_type, href)
            canonical_url = dashboard.canonical_source_url(href) or href
            if title_match and body_match:
                match_scope = "标题+正文"
            elif body_match:
                match_scope = "正文"
            else:
                match_scope = "标题"

            details.append(
                {
                    "日期": day,
                    "品类": category,
                    "自有产品": product,
                    "问题": question,
                    "轮次": dashboard.safe_int(row.get("run_no")),
                    "信源序号": dashboard.safe_int(row.get("index")),
                    "信源类型": kind,
                    "原始类型": source_type,
                    "媒体": media,
                    "域名": host,
                    "标题": title,
                    "链接": href,
                    "标准化链接": canonical_url,
                    "命中位置": match_scope,
                    "标题命中": title_match,
                    "正文命中": body_match,
                    "正文状态": str(entry.get("status") or "未进入归档索引"),
                    "正文质量": str(entry.get("extraction_quality") or ""),
                    "正文抓取时间": str(entry.get("fetched_at") or ""),
                    "正文错误": str(entry.get("error") or ""),
                    "来源识别备注": source_note,
                    "_tokens": title_tokens(title),
                    "_themes": title_themes(title),
                }
            )
            data_quality[category]["确认自有产品命中行"] += 1
            data_quality[category][f"{kind}命中行"] += 1

    details.sort(key=lambda row: (row["日期"], row["品类"], row["轮次"], row["信源序号"]))

    unique_seen = set()
    for record in details:
        key = (record["日期"], record["品类"], record["标准化链接"])
        record["当日唯一链接首条"] = key not in unique_seen
        unique_seen.add(key)

    daily_repeated = defaultdict(Counter)
    daily_unique = defaultdict(Counter)
    daily_scope = defaultdict(Counter)
    daily_title_tokens = defaultdict(Counter)
    daily_theme_docs = defaultdict(Counter)
    daily_theme_total = Counter()

    for record in details:
        key = (record["日期"], record["品类"])
        daily_repeated[key]["分母"] += 1
        daily_repeated[key][record["信源类型"]] += 1
        daily_scope[key][record["命中位置"]] += 1
        daily_title_tokens[key].update(record["_tokens"])
        daily_theme_total[key] += 1
        for theme in record["_themes"]:
            daily_theme_docs[key][theme] += 1
        if record["当日唯一链接首条"]:
            daily_unique[key]["分母"] += 1
            daily_unique[key][record["信源类型"]] += 1

    daily_rows = []
    for key in sorted(daily_repeated):
        day, category = key
        repeated = daily_repeated[key]
        unique = daily_unique[key]
        daily_rows.append(
            {
                "日期": day,
                "品类": category,
                "品类全部信源行": category_day_total[(category, day)],
                "自有产品命中行（分母）": repeated["分母"],
                "文章行": repeated["文章"],
                "视频行": repeated["视频"],
                "其他行": repeated["其他"],
                "文章占比": repeated["文章"] / repeated["分母"] if repeated["分母"] else None,
                "视频占比": repeated["视频"] / repeated["分母"] if repeated["分母"] else None,
                "其他占比": repeated["其他"] / repeated["分母"] if repeated["分母"] else None,
                "唯一链接数": unique["分母"],
                "唯一文章链接": unique["文章"],
                "唯一视频链接": unique["视频"],
                "唯一文章占比": unique["文章"] / unique["分母"] if unique["分母"] else None,
                "唯一视频占比": unique["视频"] / unique["分母"] if unique["分母"] else None,
                "标题命中行": daily_scope[key]["标题"] + daily_scope[key]["标题+正文"],
                "正文补充命中行": daily_scope[key]["正文"],
                "标题+正文双命中行": daily_scope[key]["标题+正文"],
            }
        )

    previous_by_category = {}
    for row in daily_rows:
        previous = previous_by_category.get(row["品类"])
        row["文章占比日变动"] = (
            row["文章占比"] - previous["文章占比"]
            if previous and row["文章占比"] is not None and previous["文章占比"] is not None
            else None
        )
        row["视频占比日变动"] = (
            row["视频占比"] - previous["视频占比"]
            if previous and row["视频占比"] is not None and previous["视频占比"] is not None
            else None
        )
        previous_by_category[row["品类"]] = row

    tfidf = tfidf_rows(details)

    theme_rows = []
    for category in sorted({row["品类"] for row in details}):
        article_records = [
            row for row in details if row["品类"] == category and row["信源类型"] == "文章"
        ]
        video_records = [
            row for row in details if row["品类"] == category and row["信源类型"] == "视频"
        ]
        for theme in THEME_PATTERNS:
            article_hits = sum(theme in row["_themes"] for row in article_records)
            video_hits = sum(theme in row["_themes"] for row in video_records)
            z, p_value, log_odds = two_proportion_stats(
                video_hits,
                len(video_records),
                article_hits,
                len(article_records),
            )
            theme_rows.append(
                {
                    "品类": category,
                    "主题": theme,
                    "文章命中标题": article_hits,
                    "文章标题数": len(article_records),
                    "文章命中率": article_hits / len(article_records) if article_records else 0,
                    "视频命中标题": video_hits,
                    "视频标题数": len(video_records),
                    "视频命中率": video_hits / len(video_records) if video_records else 0,
                    "视频-文章差": (
                        video_hits / len(video_records) - article_hits / len(article_records)
                        if article_records and video_records
                        else 0
                    ),
                    "平滑对数优势（视频相对文章）": log_odds,
                    "双比例z值": z,
                    "p值": p_value,
                    "显著性": "显著" if p_value < 0.05 else "不显著",
                }
            )

    daily_theme_rows = []
    for key in sorted(daily_theme_total):
        day, category = key
        total = daily_theme_total[key]
        for theme in THEME_PATTERNS:
            count = daily_theme_docs[key][theme]
            daily_theme_rows.append(
                {
                    "日期": day,
                    "品类": category,
                    "主题": theme,
                    "主题标题数": count,
                    "自有产品命中标题数": total,
                    "主题覆盖率": count / total if total else 0,
                }
            )

    js_rows = []
    for category in sorted({key[1] for key in daily_title_tokens}):
        keys = sorted(key for key in daily_title_tokens if key[1] == category)
        for previous_key, current_key in zip(keys, keys[1:]):
            previous_tokens = daily_title_tokens[previous_key]
            current_tokens = daily_title_tokens[current_key]
            rises, falls = top_distribution_shifts(previous_tokens, current_tokens)
            js_rows.append(
                {
                    "品类": category,
                    "前一日": previous_key[0],
                    "当日": current_key[0],
                    "Jensen-Shannon散度": js_divergence(previous_tokens, current_tokens),
                    "上升关键词": rises,
                    "下降关键词": falls,
                }
            )

    quality_rows = []
    for category, counts in sorted(data_quality.items()):
        quality_rows.append(
            {
                "品类": category,
                **dict(counts),
                "确认命中率（对全部信源行）": (
                    counts["确认自有产品命中行"] / counts["品类全部信源行"]
                    if counts["品类全部信源行"]
                    else 0
                ),
                "正文补充贡献率（对确认命中）": (
                    counts["正文补充命中行"] / counts["确认自有产品命中行"]
                    if counts["确认自有产品命中行"]
                    else 0
                ),
                "正文可靠归档率": (
                    counts["正文已归档行"] / counts["品类全部信源行"]
                    if counts["品类全部信源行"]
                    else 0
                ),
            }
        )

    conclusions = []
    for category in sorted({row["品类"] for row in details}):
        category_records = [row for row in details if row["品类"] == category]
        total = len(category_records)
        article_count = sum(row["信源类型"] == "文章" for row in category_records)
        video_count = sum(row["信源类型"] == "视频" for row in category_records)
        body_only = sum(row["命中位置"] == "正文" for row in category_records)
        category_daily = [row for row in daily_rows if row["品类"] == category]
        latest = category_daily[-1]
        movement_rows = [
            row for row in category_daily if row["视频占比日变动"] is not None
        ]
        largest_movement = max(
            movement_rows,
            key=lambda row: abs(row["视频占比日变动"]),
            default=None,
        )
        significant_themes = [
            row
            for row in theme_rows
            if row["品类"] == category and row["p值"] < 0.05
        ]
        video_theme = max(
            significant_themes,
            key=lambda row: row["视频-文章差"],
            default=None,
        )
        article_theme = min(
            significant_themes,
            key=lambda row: row["视频-文章差"],
            default=None,
        )
        category_js = [row for row in js_rows if row["品类"] == category]
        largest_js = max(
            category_js,
            key=lambda row: row["Jensen-Shannon散度"],
            default=None,
        )
        conclusions.append(
            {
                "品类": category,
                "确认命中信源行": total,
                "文章总占比": article_count / total if total else 0,
                "视频总占比": video_count / total if total else 0,
                "正文补充命中占比": body_only / total if total else 0,
                "最新日期": latest["日期"],
                "最新文章占比": latest["文章占比"],
                "最新视频占比": latest["视频占比"],
                "最大视频占比变动日": largest_movement["日期"] if largest_movement else "",
                "最大视频占比变动": (
                    largest_movement["视频占比日变动"] if largest_movement else None
                ),
                "视频更偏好的标题主题": video_theme["主题"] if video_theme else "无显著差异",
                "视频主题差值": video_theme["视频-文章差"] if video_theme else 0,
                "文章更偏好的标题主题": article_theme["主题"] if article_theme else "无显著差异",
                "文章主题差值": (
                    -article_theme["视频-文章差"] if article_theme else 0
                ),
                "标题关键词变化最剧烈日期": largest_js["当日"] if largest_js else "",
                "最大Jensen-Shannon散度": (
                    largest_js["Jensen-Shannon散度"] if largest_js else 0
                ),
                "剧烈变化时上升关键词": largest_js["上升关键词"] if largest_js else "",
                "剧烈变化时下降关键词": largest_js["下降关键词"] if largest_js else "",
            }
        )

    export_details = []
    for record in details:
        export_details.append(
            {key: value for key, value in record.items() if not key.startswith("_")}
        )

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scope": {
            "questions": list(TARGETS),
            "products": [target["product"] for target in TARGETS.values()],
            "primary_denominator": "每个品类、每天确认标题或可靠归档正文命中自有产品的信源行数；重复引用保留",
            "secondary_denominator": "同品类、同日期按标准化URL去重后的确认命中链接数",
        },
        "conclusions": conclusions,
        "daily": daily_rows,
        "quality": quality_rows,
        "tfidf": tfidf,
        "themes": theme_rows,
        "daily_themes": daily_theme_rows,
        "js_changes": js_rows,
        "details": export_details,
    }
    return payload


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = build_analysis()
    output_path = os.path.join(OUTPUT_DIR, "analysis_payload.json")
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "output": output_path,
                "detail_rows": len(payload["details"]),
                "daily_rows": len(payload["daily"]),
                "tfidf_rows": len(payload["tfidf"]),
                "theme_rows": len(payload["themes"]),
                "js_rows": len(payload["js_changes"]),
                "conclusions": payload["conclusions"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
