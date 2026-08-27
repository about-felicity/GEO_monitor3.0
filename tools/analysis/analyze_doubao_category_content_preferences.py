import csv
import json
import re
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

import doubao_dashboard_server as dashboard
from .analyze_doubao_category_preferences import category_for, fmt_pct, md_table, pct


BASE_DIR = Path(__file__).resolve().parents[2]
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)


FEATURES = OrderedDict([
    ("榜单推荐", ("推荐", "排行", "榜单", "十大", "top", "第一名", "红黑榜", "清单", "合集", "精选")),
    ("测评对比", ("测评", "评测", "实测", "横评", "对比", "体验", "测试", "测一测")),
    ("安全成分", ("安全", "成分", "植物", "无氨", "无添加", "刺激", "副作用", "温和", "过敏", "配方")),
    ("选择问答", ("哪个", "哪款", "怎么选", "如何选", "好用吗", "怎么样", "是否", "值得买吗", "有用吗")),
    ("年份时效", ("2024", "2025", "2026", "最新", "年度", "今年", "新款", "升级")),
    ("专业权威", ("医生", "专家", "国家", "药监", "研究", "论文", "临床", "权威", "科学", "专业")),
    ("个人体验", ("亲测", "自用", "真实", "分享", "回购", "我用", "用后", "新手", "姐妹", "空瓶")),
    ("避坑强结论", ("避坑", "避雷", "别买", "曝光", "零差评", "顶流", "封神", "闭眼入", "踩雷", "红榜", "黑榜", "公认")),
    ("价格性价比", ("价格", "平价", "性价比", "预算", "元", "便宜", "贵不贵")),
    ("功效场景", ("有效", "见效", "持久", "显白", "遮白", "控油", "蓬松", "防脱", "增长", "修护", "保湿", "祛痘", "美白", "敏感肌")),
])


CONCERNS = OrderedDict([
    ("安全", ("安全", "无害", "放心")),
    ("植物/天然", ("植物", "天然", "植萃")),
    ("温和不刺激", ("温和", "不刺激", "低刺激", "敏感")),
    ("成分配方", ("成分", "配方", "原料")),
    ("实测效果", ("实测", "测评", "评测", "有效", "效果")),
    ("排行榜", ("排行", "榜单", "十大", "top", "第一名")),
    ("避坑", ("避坑", "避雷", "踩雷", "别买", "黑榜")),
    ("显白/遮白", ("显白", "遮白", "白发")),
    ("控油蓬松", ("控油", "蓬松", "清爽")),
    ("防脱强韧", ("防脱", "防断", "强韧", "固发")),
    ("增长滋养", ("增长", "滋养", "纤长", "浓密")),
    ("修护保湿", ("修护", "保湿", "屏障", "滋润")),
    ("美白提亮", ("美白", "提亮", "淡斑")),
    ("祛痘控痘", ("祛痘", "控痘", "痘痘", "闭口")),
    ("防晒防护", ("防晒", "spf", "pa+", "防护")),
    ("价格性价比", ("价格", "平价", "性价比", "预算")),
])


def contains_any(text, tokens):
    lowered = str(text or "").casefold()
    return any(token.casefold() in lowered for token in tokens)


def feature_flags(title):
    text = str(title or "")
    flags = {name: contains_any(text, tokens) for name, tokens in FEATURES.items()}
    flags["数字清单"] = bool(re.search(r"(?:\d+|[一二三四五六七八九十]+)\s*(?:款|个|大|种)", text, re.IGNORECASE))
    flags["话题标签"] = "#" in text
    return flags


FEATURE_NAMES = list(FEATURES) + ["数字清单", "话题标签"]


def concern_flags(title):
    return {name: contains_any(title, tokens) for name, tokens in CONCERNS.items()}


def intent_for(question):
    return "评价类" if "评价" in question or "怎么样" in question else "推荐类"


def confidence(unique_links):
    if unique_links >= 30:
        return "高"
    if unique_links >= 10:
        return "中"
    return "低"


def source_profile(rows, source_type):
    selected = [row for row in rows if row["_type"] == source_type]
    refs = len(selected)
    by_url = defaultdict(list)
    for row in selected:
        by_url[row["_href"]].append(row)
    unique_rows = [items[0] for items in by_url.values()]
    feature_weighted = Counter()
    feature_unique = Counter()
    concern_counter = Counter()
    media_counter = Counter(row["_media"] for row in selected)
    title_lengths = []
    for row in selected:
        title = row["_title"]
        title_lengths.append(len(title))
        for name, value in feature_flags(title).items():
            if value:
                feature_weighted[name] += 1
        for name, value in concern_flags(title).items():
            if value:
                concern_counter[name] += 1
    for row in unique_rows:
        for name, value in feature_flags(row["_title"]).items():
            if value:
                feature_unique[name] += 1
    url_counter = Counter(row["_href"] for row in selected if row["_href"])
    title_by_url = {}
    for row in selected:
        title_by_url.setdefault(row["_href"], row["_title"])
    top_links = [
        {"title": title_by_url.get(url, "") or url, "url": url, "count": count, "share": pct(count, refs)}
        for url, count in url_counter.most_common(5)
    ]
    feature_shares = {name: pct(feature_weighted.get(name, 0), refs) for name in FEATURE_NAMES}
    unique_feature_shares = {name: pct(feature_unique.get(name, 0), len(unique_rows)) for name in FEATURE_NAMES}
    return {
        "refs": refs,
        "unique_links": len(by_url),
        "avg_title_length": round(sum(title_lengths) / len(title_lengths), 1) if title_lengths else 0,
        "feature_shares": feature_shares,
        "unique_feature_shares": unique_feature_shares,
        "top_features": sorted(feature_shares.items(), key=lambda item: (-item[1], item[0]))[:5],
        "top_concerns": [(name, pct(count, refs)) for name, count in concern_counter.most_common(6)],
        "top_media": media_counter.most_common(5),
        "top_links": top_links,
        "top10_share": pct(sum(count for _url, count in url_counter.most_common(10)), refs),
        "confidence": confidence(len(by_url)),
    }


def profile_sentence(category, source_type, profile):
    if not profile["refs"]:
        return f"当前没有可分析的{source_type}信源。"
    if profile["refs"] < 20 or profile["unique_links"] < 5:
        prefix = f"样本较少（{profile['refs']}条、{profile['unique_links']}个链接），当前只能作方向性判断："
    else:
        prefix = ""
    media = profile["top_media"][0][0] if profile["top_media"] else "未知来源"
    features = "、".join(name for name, share in profile["top_features"][:3] if share > 0) or "直接品类描述"
    concerns = "、".join(name for name, share in profile["top_concerns"][:3] if share > 0) or "产品功效"
    concentration = "固定核心内容明显" if profile["top10_share"] >= 55 else ("存在核心内容池" if profile["top10_share"] >= 30 else "内容来源较分散")
    if source_type == "视频":
        template = f"豆包偏好来自{media}、标题直接出现“{category}”需求，并采用{features}的短内容；高频关注{concerns}"
    else:
        template = f"豆包偏好来自{media}等可抓取页面，文章标题和结构更常采用{features}；高频论点是{concerns}"
    return f"{prefix}{template}；标题平均{profile['avg_title_length']:.1f}字，{concentration}。"


def daily_snapshot(rows):
    result = {"urls": {row["_href"] for row in rows if row["_href"]}}
    total = len(rows)
    result["video_share"] = pct(sum(1 for row in rows if row["_type"] == "视频"), total)
    for source_type in ("视频", "文章"):
        type_rows = [row for row in rows if row["_type"] == source_type]
        counter = Counter()
        for row in type_rows:
            for name, value in feature_flags(row["_title"]).items():
                if value:
                    counter[name] += 1
        result[source_type] = {
            "refs": len(type_rows),
            "features": {name: pct(counter.get(name, 0), len(type_rows)) for name in FEATURE_NAMES},
        }
    return result


def transition(category, previous_day, day, previous, current, latest_day):
    union = previous["urls"] | current["urls"]
    jaccard = len(previous["urls"] & current["urls"]) / len(union) if union else 1.0
    changes = {}
    for source_type in ("视频", "文章"):
        for feature in FEATURE_NAMES:
            key = f"{source_type}-{feature}"
            changes[key] = round(current[source_type]["features"][feature] - previous[source_type]["features"][feature], 1)
    largest = sorted(changes.items(), key=lambda item: -abs(item[1]))[:5]
    max_feature_shift = max((abs(value) for value in changes.values()), default=0)
    video_shift = round(current["video_share"] - previous["video_share"], 1)
    content_pool_turnover = (1 - jaccard) * 100
    score = round(0.45 * max_feature_shift + 0.30 * abs(video_shift) + 0.25 * content_pool_turnover, 1)
    change_types = []
    if max_feature_shift >= 20:
        change_types.append("内容风格变化")
    if abs(video_shift) >= 15:
        change_types.append("视频/文章结构变化")
    if jaccard <= 0.35:
        change_types.append("信源池剧烈换血")
    level = "剧烈" if change_types else ("明显" if max_feature_shift >= 12 or abs(video_shift) >= 8 or jaccard <= 0.55 else "平稳")
    feature_text = "、".join(f"{name}{value:+.1f}pct" for name, value in largest[:3] if abs(value) >= 5)
    reasons = change_types[:]
    if feature_text:
        reasons.append(feature_text)
    if day == latest_day:
        reasons.append("最新日未收盘")
    return {
        "category": category,
        "from_day": previous_day,
        "to_day": day,
        "level": level,
        "score": score,
        "jaccard": round(jaccard, 3),
        "video_shift": video_shift,
        "max_feature_shift": round(max_feature_shift, 1),
        "largest_changes": largest,
        "change_types": "、".join(change_types) or "常规波动",
        "reason": "；".join(reasons) or "主要内容特征稳定",
        "latest_unclosed": day == latest_day,
    }


def analyze():
    rows = [row for row in dashboard.read_csv_rows() if not dashboard.is_quarantined_source_row(row)]
    ai_cache = dashboard.read_json(dashboard.AI_CACHE_PATH)
    meta_cache = dashboard.read_json(dashboard.META_CACHE_PATH)
    enriched = []
    for row in rows:
        question = dashboard.question_for(row)
        source_type, media, host, _note = dashboard.source_for(row, ai_cache, meta_cache)
        enriched.append({
            "_category": category_for(question),
            "_question": question,
            "_intent": intent_for(question),
            "_day": dashboard.date_for(row),
            "_type": source_type,
            "_media": media,
            "_host": host,
            "_href": str(row.get("href") or "").strip(),
            "_title": str(row.get("title") or "").strip(),
            "run_no": dashboard.safe_int(row.get("run_no")),
        })
    latest_day = max((row["_day"] for row in enriched if row["_day"]), default="")
    grouped = defaultdict(list)
    for row in enriched:
        grouped[row["_category"]].append(row)
    categories = []
    events = []
    for category, category_rows in grouped.items():
        runs = {row["run_no"] for row in category_rows if row["run_no"]}
        video = source_profile(category_rows, "视频")
        article = source_profile(category_rows, "文章")
        intent_stats = {}
        for intent in ("推荐类", "评价类"):
            intent_rows = [row for row in category_rows if row["_intent"] == intent]
            if intent_rows:
                intent_stats[intent] = {
                    "refs": len(intent_rows),
                    "video_share": pct(sum(1 for row in intent_rows if row["_type"] == "视频"), len(intent_rows)),
                    "article_share": pct(sum(1 for row in intent_rows if row["_type"] == "文章"), len(intent_rows)),
                }
        by_day = defaultdict(list)
        for row in category_rows:
            if row["_day"]:
                by_day[row["_day"]].append(row)
        daily = {day: daily_snapshot(day_rows) for day, day_rows in sorted(by_day.items())}
        category_events = []
        days = sorted(daily)
        for previous_day, day in zip(days, days[1:]):
            event = transition(category, previous_day, day, daily[previous_day], daily[day], latest_day)
            category_events.append(event)
            events.append(event)
        categories.append({
            "category": category,
            "questions": sorted({row["_question"] for row in category_rows}),
            "runs": len(runs),
            "refs": len(category_rows),
            "video_share": pct(video["refs"], len(category_rows)),
            "article_share": pct(article["refs"], len(category_rows)),
            "video": video,
            "article": article,
            "video_sentence": profile_sentence(category, "视频", video),
            "article_sentence": profile_sentence(category, "文章", article),
            "intent_stats": intent_stats,
            "daily": daily,
            "events": category_events,
            "severe_events": [event for event in category_events if event["level"] == "剧烈"],
        })
    categories.sort(key=lambda item: (-item["runs"], item["category"]))
    events.sort(key=lambda item: (-item["score"], item["category"], item["to_day"]))
    snapshot = datetime.now(dashboard.CST).strftime("%Y-%m-%d %H:%M:%S")
    return enriched, categories, events, snapshot, latest_day


def compact_features(profile):
    return "；".join(f"{name}{share:.1f}%" for name, share in profile["top_features"][:4])


def write_summary_csv(categories, path):
    fields = ["品类", "运行轮次", "信源条数", "视频占比", "文章占比", "视频画像", "文章画像", "视频高频特征", "文章高频特征", "视频高频关注点", "文章高频关注点", "剧烈变化次数"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in categories:
            writer.writerow({
                "品类": item["category"], "运行轮次": item["runs"], "信源条数": item["refs"],
                "视频占比": item["video_share"], "文章占比": item["article_share"],
                "视频画像": item["video_sentence"], "文章画像": item["article_sentence"],
                "视频高频特征": compact_features(item["video"]), "文章高频特征": compact_features(item["article"]),
                "视频高频关注点": "；".join(f"{name}{share:.1f}%" for name, share in item["video"]["top_concerns"]),
                "文章高频关注点": "；".join(f"{name}{share:.1f}%" for name, share in item["article"]["top_concerns"]),
                "剧烈变化次数": len(item["severe_events"]),
            })


def write_events_csv(events, path):
    fields = ["品类", "前一观测日", "当前观测日", "等级", "变化类型", "波动分", "链接相似度", "视频占比变化pct", "最大内容特征变化pct", "主要变化", "最新日未收盘"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for event in events:
            # 文件名和报告口径都是“剧烈变化”，不要混入仅为“明显”的波动。
            if event["level"] != "剧烈":
                continue
            writer.writerow({
                "品类": event["category"], "前一观测日": event["from_day"], "当前观测日": event["to_day"],
                "等级": event["level"], "变化类型": event["change_types"], "波动分": event["score"],
                "链接相似度": event["jaccard"], "视频占比变化pct": event["video_shift"],
                "最大内容特征变化pct": event["max_feature_shift"], "主要变化": event["reason"],
                "最新日未收盘": "是" if event["latest_unclosed"] else "否",
            })


def feature_table(profile):
    return md_table(
        ["内容特征", "按引用次数加权", "按独立链接计算"],
        [[name, fmt_pct(profile["feature_shares"].get(name, 0)), fmt_pct(profile["unique_feature_shares"].get(name, 0))] for name in FEATURE_NAMES],
    )


def links_table(profile):
    if not profile["top_links"]:
        return "暂无。"
    return md_table(
        ["典型标题", "引用次数", "该类型内占比"],
        [[item["title"][:100], item["count"], fmt_pct(item["share"])] for item in profile["top_links"]],
    )


def write_markdown(rows, categories, events, snapshot, latest_day, path):
    severe = [event for event in events if event["level"] == "剧烈"]
    out = [
        "# 豆包各产品品类的视频与文章内容偏好报告",
        "",
        f"**数据快照：** {snapshot}（中国标准时间）  ",
        f"**数据规模：** {len({row['run_no'] for row in rows if row['run_no']}):,} 轮、{len(rows):,} 条信源、{len(categories)} 个产品品类  ",
        f"**最新数据日：** {latest_day}（未收盘）",
        "",
        "## 一、这份报告回答什么",
        "",
        "这份报告不再回答“豆包喜欢哪个产品”，而是回答：对每个产品品类，豆包更愿意引用什么样的视频和文章。具体观察平台、标题结构、榜单/测评/安全成分/个人体验等内容特征、核心链接集中度和内容风格变化。",
        "",
        "- 按引用次数加权：反映用户实际看到的豆包结果，重复出现的核心链接权重更高。",
        "- 按独立链接计算：避免一个高频链接独自决定内容画像。",
        "- “喜欢”表示在当前监控问题中更频繁地被豆包选为信源，不代表内容真实、权威或产品质量更高。",
        "- 推荐类和评价类问法会改变媒体结构，报告在同时存在两类问题的品类中单独列出。",
        "",
        "## 二、全部品类内容画像总表",
        "",
    ]
    out.append(md_table(
        ["品类", "轮次", "视频", "文章", "豆包偏好的视频长相", "豆包偏好的文章长相", "剧烈变化"],
        [[item["category"], item["runs"], fmt_pct(item["video_share"]), fmt_pct(item["article_share"]), item["video_sentence"], item["article_sentence"], len(item["severe_events"])] for item in categories],
    ))
    out.extend(["", "## 三、内容风格变化最剧烈的时段", ""])
    out.append(md_table(
        ["品类", "时段", "变化类型", "链接相似度", "视频占比变化", "最大内容特征变化", "主要变化"],
        [[event["category"], f"{event['from_day']} → {event['to_day']}", event["change_types"], f"{event['jaccard']:.2f}", f"{event['video_shift']:+.1f}pct", f"{event['max_feature_shift']:.1f}pct", event["reason"]] for event in severe[:40]],
    ))
    out.extend(["", "> 终点为最新数据日的变化尚未收盘，需要在当天运行结束后再次确认。", "", "## 四、逐品类详细内容偏好", ""])
    for index, item in enumerate(categories, 1):
        out.extend([
            f"### {index}. {item['category']}", "",
            f"**包含问题：** {'、'.join(item['questions'])}  ",
            f"**数据规模：** {item['runs']:,}轮、{item['refs']:,}条信源；视频{fmt_pct(item['video_share'])}、文章{fmt_pct(item['article_share'])}。", "",
        ])
        if "推荐类" in item["intent_stats"] and "评价类" in item["intent_stats"]:
            rec = item["intent_stats"]["推荐类"]
            eva = item["intent_stats"]["评价类"]
            out.extend([
                f"**问法差异：** 推荐类视频占{fmt_pct(rec['video_share'])}、文章占{fmt_pct(rec['article_share'])}；评价类视频占{fmt_pct(eva['video_share'])}、文章占{fmt_pct(eva['article_share'])}。因此两种问法的内容画像不能直接混为同一种偏好。", "",
            ])
        for source_type, profile_key, sentence_key in (("视频", "video", "video_sentence"), ("文章", "article", "article_sentence")):
            profile = item[profile_key]
            out.extend([
                f"#### 豆包喜欢什么样的{item['category']}{source_type}", "",
                item[sentence_key], "",
                f"样本：{profile['refs']:,}条引用、{profile['unique_links']:,}个独立链接；前10个链接占该类型{fmt_pct(profile['top10_share'])}；画像置信度：{profile['confidence']}。", "",
                "**主要媒体/平台**", "",
                md_table(["媒体/平台", "引用次数", "该类型内占比"], [[name, count, fmt_pct(pct(count, profile['refs']))] for name, count in profile["top_media"]]) if profile["top_media"] else "暂无。", "",
                "**内容特征**", "", feature_table(profile), "",
                "**高频关注点：** " + ("、".join(f"{name}（{share:.1f}%）" for name, share in profile["top_concerns"]) or "样本不足"), "",
                "**最典型的标题/信源**", "", links_table(profile), "",
            ])
        notable = [event for event in item["events"] if event["level"] != "平稳"]
        out.extend(["#### 内容风格变化", ""])
        if notable:
            for event in sorted(notable, key=lambda value: -value["score"]):
                out.append(f"- {event['from_day']} → {event['to_day']}：**{event['level']}**，{event['change_types']}。{event['reason']}。")
        elif len(item["daily"]) <= 1:
            out.append("- 当前只有一个观测日，暂时无法判断变化。")
        else:
            out.append("- 当前相邻观测日未出现明显内容风格变化。")
        out.append("")
    out.extend([
        "## 五、如何把内容画像用于业务", "",
        "1. 先按品类选择内容结构。例如染发剂更需要安全、植物、实测和榜单；眉睫精华更需要效果、成分、副作用与真实周期。", 
        "2. 视频标题应直接出现品类/品牌词，并用榜单、测评、避坑或强结论提高可抽取性；但不能使用无法验证的绝对化承诺。", 
        "3. 文章应提供年份、评价标准、产品清单、成分与安全证据，并保持移动端正文可抓取。", 
        "4. 内容风格剧烈变化时，先判断是核心链接换血、推荐/评价问题组合变化，还是豆包真的改变了偏好。", 
        "5. 高频被引用不等于高质量；应与信源权威性、原创性和商业倾向评分并列使用。", 
    ])
    path.write_text("\n".join(out), encoding="utf-8")


def main():
    rows, categories, events, snapshot, latest_day = analyze()
    stamp = latest_day or datetime.now(dashboard.CST).strftime("%Y-%m-%d")
    md_path = REPORT_DIR / f"豆包各品类视频文章内容偏好报告_{stamp}.md"
    docx_path = md_path.with_suffix(".docx")
    summary_path = REPORT_DIR / f"豆包各品类视频文章内容画像汇总_{stamp}.csv"
    events_path = REPORT_DIR / f"豆包各品类内容风格剧烈变化_{stamp}.csv"
    write_markdown(rows, categories, events, snapshot, latest_day, md_path)
    write_summary_csv(categories, summary_path)
    write_events_csv(events, events_path)
    from .analyze_doubao_category_preferences import markdown_to_docx
    markdown_to_docx(md_path, docx_path)
    print(json.dumps({
        "snapshot": snapshot,
        "latest_day": latest_day,
        "categories": len(categories),
        "severe_events": sum(1 for event in events if event["level"] == "剧烈"),
        "markdown": str(md_path),
        "docx": str(docx_path),
        "summary_csv": str(summary_path),
        "events_csv": str(events_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
