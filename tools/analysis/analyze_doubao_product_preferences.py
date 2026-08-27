import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import doubao_dashboard_server as dashboard
from .analyze_doubao_category_preferences import category_for, fmt_pct, md_table, pct


BASE_DIR = Path(__file__).resolve().parents[2]
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)


CATEGORY_GENERIC_TOKENS = {
    "染发剂": ("泡沫染发剂", "染发剂", "染发霜", "染发膏", "染发乳"),
    "眉毛精华液": ("眉毛增长液", "眉毛滋养液", "眉毛精华液", "眉毛", "精华液"),
    "睫毛精华液": ("睫毛增长液", "睫毛滋养液", "睫毛精华液", "睫毛液", "睫毛", "精华液"),
    "美白面霜": ("美白面霜", "美白霜", "面霜"),
    "控油蓬松洗发水": ("控油蓬松洗发水", "蓬松洗发水", "洗发水", "洗发液"),
    "二硫化硒洗发水": ("二硫化硒洗发水", "二硫化硒洗剂", "洗发水", "洗发液", "洗剂"),
    "防断洗发水": ("防脱防断洗发水", "防断洗发水", "洗发水", "洗发液"),
    "防脱洗发水": ("防脱洗发水", "洗发水", "洗发液"),
    "防脱精华液": ("防脱固发精华液", "防脱精华液", "头皮精华液", "精华液"),
    "护发精油": ("护发精油", "护发油", "精油"),
    "护发素": ("护发素", "护发霜"),
    "防晒霜": ("防晒霜", "防晒乳", "防晒"),
    "沐浴精油": ("沐浴精油", "沐浴油"),
    "面膜": ("涂抹面膜", "片状面膜", "面膜"),
    "祛痘精华液": ("祛痘精华液", "祛痘精华", "精华液", "精华"),
    "护手霜": ("护手霜", "手霜"),
    "洗面奶": ("洁面泡沫", "洁面啫喱", "洁面乳", "洗面霜", "洗面奶"),
    "身体乳": ("身体乳液", "身体乳", "润肤乳"),
    "造型喷雾": ("造型喷雾", "定型喷雾", "蓬松喷雾"),
    "爽肤水": ("爽肤水", "润肤水", "化妆水"),
    "眼霜": ("眼部精华液", "眼部精华", "眼霜"),
    "卸妆油": ("卸妆油",),
}


def report_brand(value):
    text = dashboard.canonical_brand_name(value)
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.casefold())
    if compact in {"依思佩尔", "依斯佩尔", "especial", "especialofficial"}:
        return "依斯佩尔 eSpecial"
    return text


def product_core_key(category, product, brand):
    text = str(product or "").strip()
    text = re.sub(r"[（(][^）)]*(?:ml|g|克|毫升|片|支|瓶|盒)[^）)]*[）)]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ml|g|kg)\b", "", text, flags=re.IGNORECASE)
    compact = re.sub(r"[\s\-_—·•/\\（）()【】\[\],，.。:：]+", "", text).casefold()
    compact = compact.replace("依思佩尔", "依斯佩尔especial").replace("e-special", "especial")
    for country in ("瑞士", "法国", "德国", "日本", "韩国", "美国", "英国", "意大利", "澳洲", "澳大利亚"):
        compact = compact.replace(country, "")
    compact = compact.replace("三分钟", "3分钟").replace("两分钟", "2分钟").replace("二分钟", "2分钟")
    strengths = re.findall(r"\d+(?:\.\d+)?%", compact)
    if strengths:
        compact = re.sub(r"\d+(?:\.\d+)?%", "", compact) + "".join(sorted(set(strengths)))
    for token in sorted(CATEGORY_GENERIC_TOKENS.get(category, ()), key=len, reverse=True):
        compact = compact.replace(re.sub(r"\s+", "", token).casefold(), "")
    compact = re.sub(r"(?:正装|官方|旗舰店|同款|推荐)$", "", compact)
    brand_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(brand or "").casefold())
    if not compact:
        compact = brand_key or re.sub(r"\s+", "", str(product or "")).casefold()
    return f"{category}|{brand_key}|{compact}"


def choose_display(counter):
    if not counter:
        return ""
    return sorted(counter.items(), key=lambda item: (-item[1], len(item[0]), item[0]))[0][0]


def preference_level(rate, avg_rank, runs):
    if runs >= 20 and rate >= 70 and avg_rank and avg_rank <= 2.2:
        return "核心首选"
    if runs >= 15 and rate >= 50:
        return "高频偏好"
    if runs >= 8 and rate >= 20:
        return "中度偏好"
    if runs >= 3 and rate >= 5:
        return "低频候选"
    return "长尾/偶发"


def confidence_for(runs):
    if runs >= 30:
        return "高"
    if runs >= 10:
        return "中"
    return "低"


def media_preference(video, article, product):
    if video >= 85:
        return "强视频驱动"
    if article >= 35 and video < 65:
        return "视频与文章共同驱动"
    if product >= 10:
        return "视频为主，商品页影响明显"
    if video >= 60:
        return "视频主导"
    return "混合信源驱动"


def analyze():
    source_rows = [row for row in dashboard.read_csv_rows() if not dashboard.is_quarantined_source_row(row)]
    ai_cache = dashboard.read_json(dashboard.AI_CACHE_PATH)
    meta_cache = dashboard.read_json(dashboard.META_CACHE_PATH)
    sources_by_run = defaultdict(list)
    for row in source_rows:
        run_no = dashboard.safe_int(row.get("run_no"))
        if not run_no:
            continue
        source_type, media, host, _note = dashboard.source_for(row, ai_cache, meta_cache)
        sources_by_run[run_no].append({
            "type": source_type,
            "media": media,
            "host": host,
            "href": str(row.get("href") or "").strip(),
            "title": str(row.get("title") or "").strip(),
        })

    product_rows = dashboard.dedupe_product_rows_for_stats(dashboard.read_product_rows())
    valid_rows = []
    for row in product_rows:
        question = dashboard.product_question_for(row)
        if dashboard.is_excluded_product_stat_row(row, question):
            continue
        if not dashboard.is_recommendation_question(question):
            continue
        if not dashboard.is_ai_verified_product_row(row):
            continue
        product = dashboard.normalize_product_for_stats(row.get("product_name"))
        if not product or dashboard.is_noisy_product_name(product):
            continue
        category = category_for(question)
        brand = report_brand(dashboard.brand_for_row(row, product))
        run_no = dashboard.safe_int(row.get("run_no"))
        day = dashboard.date_for(row)
        rank = dashboard.safe_int(row.get("product_index"))
        key = product_core_key(category, product, brand)
        valid_rows.append({
            "key": key,
            "category": category,
            "question": question,
            "brand": brand,
            "product": product,
            "run_no": run_no,
            "day": day,
            "rank": rank if rank > 0 else None,
            "evidence": str(row.get("evidence") or "").strip(),
        })

    # 当模型只返回品牌简称时，仅在该品类/品牌存在唯一或绝对主导的
    # 详细产品写法时合并。这样可修复“GeraX”与完整产品名被拆开，
    # 同时避免把一个多产品品牌的所有产品粗暴合并。
    detailed_keys = defaultdict(Counter)
    for row in valid_rows:
        parts = row["key"].split("|", 2)
        brand_key = parts[1] if len(parts) > 1 else ""
        core = parts[2] if len(parts) > 2 else ""
        if brand_key and core and core != brand_key:
            detailed_keys[(row["category"], brand_key)][row["key"]] += 1
    for row in valid_rows:
        parts = row["key"].split("|", 2)
        brand_key = parts[1] if len(parts) > 1 else ""
        core = parts[2] if len(parts) > 2 else ""
        if not brand_key or core != brand_key:
            continue
        candidates = detailed_keys.get((row["category"], brand_key), Counter()).most_common()
        if len(candidates) == 1:
            row["key"] = candidates[0][0]
        elif candidates and candidates[0][1] >= 5 and candidates[0][1] >= candidates[1][1] * 3:
            row["key"] = candidates[0][0]

    audited_runs = defaultdict(set)
    audited_runs_by_day = defaultdict(lambda: defaultdict(set))
    for row in valid_rows:
        audited_runs[row["category"]].add(row["run_no"])
        audited_runs_by_day[row["category"]][row["day"]].add(row["run_no"])

    groups = {}
    seen_product_run = set()
    for row in valid_rows:
        product_run_key = (row["key"], row["run_no"])
        if product_run_key in seen_product_run:
            continue
        seen_product_run.add(product_run_key)
        item = groups.setdefault(row["key"], {
            "category": row["category"],
            "aliases": Counter(),
            "brands": Counter(),
            "questions": Counter(),
            "runs": set(),
            "runs_by_day": defaultdict(set),
            "ranks": [],
            "rank_counts": Counter(),
            "evidence": Counter(),
        })
        item["aliases"][row["product"]] += 1
        if row["brand"]:
            item["brands"][row["brand"]] += 1
        item["questions"][row["question"]] += 1
        item["runs"].add(row["run_no"])
        item["runs_by_day"][row["day"]].add(row["run_no"])
        if row["rank"]:
            item["ranks"].append(row["rank"])
            item["rank_counts"][row["rank"]] += 1
        if row["evidence"]:
            item["evidence"][row["evidence"]] += 1

    latest_day = max((row["day"] for row in valid_rows if row["day"]), default="")
    products = []
    volatility_events = []
    for key, item in groups.items():
        category = item["category"]
        category_runs = audited_runs[category]
        run_count = len(item["runs"])
        rate = pct(run_count, len(category_runs), 2)
        avg_rank = round(sum(item["ranks"]) / len(item["ranks"]), 2) if item["ranks"] else None
        best_rank = min(item["ranks"]) if item["ranks"] else None
        dates = sorted(audited_runs_by_day[category])
        daily = []
        for day in dates:
            denominator = len(audited_runs_by_day[category][day])
            count = len(item["runs_by_day"].get(day, set()))
            daily.append({"day": day, "runs": count, "denominator": denominator, "rate": pct(count, denominator, 2)})
        max_swing = 0.0
        max_event = None
        for previous, current in zip(daily, daily[1:]):
            delta = round(current["rate"] - previous["rate"], 2)
            if abs(delta) > max_swing:
                max_swing = abs(delta)
                max_event = (previous["day"], current["day"], delta)
        previous_rate = daily[-2]["rate"] if len(daily) >= 2 else None
        latest_rate = daily[-1]["rate"] if daily else 0.0
        latest_delta = round(latest_rate - previous_rate, 2) if previous_rate is not None else None
        volatility = "剧烈" if (max_swing >= 30 or (run_count >= 5 and max_swing >= 20)) else ("明显" if max_swing >= 10 else "平稳")

        type_counter = Counter()
        media_counter = Counter()
        link_counter = Counter()
        title_by_link = {}
        source_runs_found = 0
        for run_no in item["runs"]:
            run_sources = sources_by_run.get(run_no, [])
            if run_sources:
                source_runs_found += 1
            for source in run_sources:
                type_counter[source["type"]] += 1
                media_counter[source["media"]] += 1
                if source["href"]:
                    link_counter[source["href"]] += 1
                    title_by_link.setdefault(source["href"], source["title"])
        source_total = sum(type_counter.values())
        video_share = pct(type_counter.get("视频", 0), source_total)
        article_share = pct(type_counter.get("文章", 0), source_total)
        product_share = pct(type_counter.get("商品页", 0), source_total)
        level = preference_level(rate, avg_rank, run_count)
        confidence = confidence_for(run_count)
        media_mode = media_preference(video_share, article_share, product_share)
        trend = ""
        if latest_delta is not None:
            if latest_delta >= 15:
                trend = "快速上升"
            elif latest_delta <= -15:
                trend = "快速下降"
            elif latest_delta >= 5:
                trend = "上升"
            elif latest_delta <= -5:
                trend = "下降"
            else:
                trend = "基本稳定"
        display = choose_display(item["aliases"])
        brand = choose_display(item["brands"])
        top_link, top_link_count = link_counter.most_common(1)[0] if link_counter else ("", 0)
        product_item = {
            "key": key,
            "category": category,
            "brand": brand,
            "product": display,
            "aliases": [name for name, _count in item["aliases"].most_common()],
            "questions": [name for name, _count in item["questions"].most_common()],
            "run_count": run_count,
            "category_runs": len(category_runs),
            "appearance_rate": rate,
            "avg_rank": avg_rank,
            "best_rank": best_rank,
            "rank_counts": dict(sorted(item["rank_counts"].items())),
            "preference_level": level,
            "confidence": confidence,
            "daily": daily,
            "first_day": daily[0]["day"] if daily else "",
            "last_day": daily[-1]["day"] if daily else "",
            "latest_rate": latest_rate,
            "previous_rate": previous_rate,
            "latest_delta": latest_delta,
            "max_swing": round(max_swing, 2),
            "max_event": max_event,
            "volatility": volatility,
            "video_share": video_share,
            "article_share": article_share,
            "product_page_share": product_share,
            "media_mode": media_mode,
            "top_media": media_counter.most_common(3),
            "source_run_coverage": pct(source_runs_found, run_count),
            "top_context_link": top_link,
            "top_context_title": title_by_link.get(top_link, ""),
            "top_context_count": top_link_count,
            "top_evidence": item["evidence"].most_common(1)[0][0] if item["evidence"] else "",
            "trend": trend,
        }
        products.append(product_item)
        if volatility != "平稳" and max_event:
            volatility_events.append({
                "category": category,
                "brand": brand,
                "product": display,
                "level": volatility,
                "from_day": max_event[0],
                "to_day": max_event[1],
                "delta": max_event[2],
                "max_swing": round(max_swing, 2),
                "run_count": run_count,
                "appearance_rate": rate,
                "confidence": confidence,
                "latest_unclosed": max_event[1] == latest_day,
            })

    products.sort(key=lambda item: (item["category"], -item["appearance_rate"], -item["run_count"], item["product"]))
    volatility_events.sort(key=lambda item: (-item["max_swing"], -item["run_count"], item["category"]))
    snapshot = datetime.now(dashboard.CST).strftime("%Y-%m-%d %H:%M:%S")
    return products, volatility_events, snapshot, latest_day


def write_products_csv(products, path):
    fields = [
        "品类", "品牌", "标准产品名", "原始别名", "涉及问题", "出现轮次", "品类有效产品轮次", "品类内出现率",
        "偏好等级", "置信度", "平均排名", "最高排名", "名次分布", "首次出现", "最近出现", "最新日出现率",
        "前一观测日出现率", "最新变化pct", "最大单次波动pct", "波动等级", "关联视频占比", "关联文章占比",
        "关联商品页占比", "信源驱动类型", "主要媒体", "运行级信源覆盖率", "最高频关联信源标题", "最高频关联信源链接",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in products:
            writer.writerow({
                "品类": item["category"], "品牌": item["brand"], "标准产品名": item["product"],
                "原始别名": "；".join(item["aliases"]), "涉及问题": "；".join(item["questions"]),
                "出现轮次": item["run_count"], "品类有效产品轮次": item["category_runs"], "品类内出现率": item["appearance_rate"],
                "偏好等级": item["preference_level"], "置信度": item["confidence"], "平均排名": item["avg_rank"] or "",
                "最高排名": item["best_rank"] or "", "名次分布": json.dumps(item["rank_counts"], ensure_ascii=False),
                "首次出现": item["first_day"], "最近出现": item["last_day"], "最新日出现率": item["latest_rate"],
                "前一观测日出现率": item["previous_rate"] if item["previous_rate"] is not None else "",
                "最新变化pct": item["latest_delta"] if item["latest_delta"] is not None else "",
                "最大单次波动pct": item["max_swing"], "波动等级": item["volatility"],
                "关联视频占比": item["video_share"], "关联文章占比": item["article_share"],
                "关联商品页占比": item["product_page_share"], "信源驱动类型": item["media_mode"],
                "主要媒体": "；".join(f"{name}:{count}" for name, count in item["top_media"]),
                "运行级信源覆盖率": item["source_run_coverage"], "最高频关联信源标题": item["top_context_title"],
                "最高频关联信源链接": item["top_context_link"],
            })


def write_volatility_csv(events, path):
    fields = ["品类", "品牌", "产品", "波动等级", "前一观测日", "当前观测日", "出现率变化pct", "最大波动pct", "总出现轮次", "总体出现率", "置信度", "最新日未收盘"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in events:
            writer.writerow({
                "品类": item["category"], "品牌": item["brand"], "产品": item["product"], "波动等级": item["level"],
                "前一观测日": item["from_day"], "当前观测日": item["to_day"], "出现率变化pct": item["delta"],
                "最大波动pct": item["max_swing"], "总出现轮次": item["run_count"], "总体出现率": item["appearance_rate"],
                "置信度": item["confidence"], "最新日未收盘": "是" if item["latest_unclosed"] else "否",
            })


def write_markdown(products, events, snapshot, latest_day, path):
    categories = defaultdict(list)
    for item in products:
        categories[item["category"]].append(item)
    high_conf = [item for item in products if item["confidence"] == "高"]
    stable_favored = [item for item in products if item["preference_level"] in ("核心首选", "高频偏好") and item["confidence"] != "低"]
    severe = [item for item in events if item["level"] == "剧烈"]
    out = [
        "# 豆包每个具体产品推荐偏好与波动报告",
        "",
        f"**数据快照：** {snapshot}（中国标准时间）  ",
        f"**最新数据日：** {latest_day}（未收盘）  ",
        f"**产品范围：** {len(products)} 个归一化具体产品，覆盖 {len(categories)} 个品类；其中高置信度产品 {len(high_conf)} 个。",
        "",
        "> 重要口径：产品与信源目前只能按同一运行轮次关联。报告中的“关联视频/文章/商品页”表示豆包推荐该产品时，该轮回答同时使用的信源结构，不代表某条链接直接支持该产品。",
        "",
        "## 一、如何理解每个产品的豆包偏好",
        "",
        "- 品类内出现率 = 产品出现轮次 ÷ 该品类已完成产品审核的有效轮次，同一产品每轮最多计算一次。",
        "- 核心首选：样本充足、出现率≥70%，且平均排名通常在前2.2名。",
        "- 高频偏好：样本充足且出现率≥50%；中度偏好为≥20%；低频候选为≥5%；其余为长尾/偶发。",
        "- 置信度按出现轮次划分：≥30轮为高，10—29轮为中，少于10轮为低。",
        "- 波动以相邻有效观测日的出现率差计算；最大变化≥30pct，或样本不低于5轮且变化≥20pct，标为剧烈。",
        "- 同品异写已按品牌、空格、符号和品类通用后缀归并；CSV仍保留全部原始别名，便于复核。",
        "",
        "## 二、总体结果",
        "",
        f"共形成 {len(products)} 个具体产品。稳定达到“核心首选/高频偏好”且至少中置信度的产品有 {len(stable_favored)} 个；识别到 {len(severe)} 个剧烈波动产品。低置信度产品不得直接解释为豆包稳定偏好。",
        "",
        "### 稳定高偏好产品 Top 50",
        "",
    ]
    top_stable = sorted(stable_favored, key=lambda item: (-item["appearance_rate"], item["avg_rank"] or 999, -item["run_count"]))[:50]
    out.append(md_table(
        ["品类", "品牌", "产品", "偏好", "出现轮次", "出现率", "平均排名", "关联信源", "置信度"],
        [[item["category"], item["brand"], item["product"], item["preference_level"], item["run_count"], fmt_pct(item["appearance_rate"]), item["avg_rank"] or "-", item["media_mode"], item["confidence"]] for item in top_stable],
    ))
    out.extend(["", "## 三、变化剧烈的具体产品", ""])
    out.append(md_table(
        ["品类", "品牌", "产品", "时段", "出现率变化", "总体出现率", "轮次", "置信度", "提示"],
        [[item["category"], item["brand"], item["product"], f"{item['from_day']} → {item['to_day']}", f"{item['delta']:+.1f}pct", fmt_pct(item["appearance_rate"]), item["run_count"], item["confidence"], "最新日未收盘" if item["latest_unclosed"] else "完整观测日"] for item in events[:100]],
    ))
    out.extend(["", "## 四、逐品类、逐产品偏好明细", ""])
    ordered_categories = sorted(categories, key=lambda name: (-sum(item["run_count"] for item in categories[name]), name))
    for index, category in enumerate(ordered_categories, 1):
        items = sorted(categories[category], key=lambda item: (-item["appearance_rate"], -item["run_count"], item["product"]))
        out.append(f"### {index}. {category}（{len(items)} 个具体产品）")
        out.append("")
        out.append(md_table(
            ["品牌", "具体产品", "偏好等级", "出现轮次/有效轮次", "出现率", "平均/最高排名", "最新变化", "最大波动", "关联信源结构", "置信度"],
            [[
                item["brand"] or "待确认", item["product"], item["preference_level"], f"{item['run_count']}/{item['category_runs']}",
                fmt_pct(item["appearance_rate"]), f"{item['avg_rank'] or '-'} / {item['best_rank'] or '-'}",
                f"{item['latest_delta']:+.1f}pct" if item["latest_delta"] is not None else "-",
                f"{item['volatility']} {item['max_swing']:.1f}pct", item["media_mode"], item["confidence"],
            ] for item in items],
        ))
        out.append("")
        notable = [item for item in items if item["volatility"] == "剧烈" and item["confidence"] != "低"]
        if notable:
            out.append("**本品类重点波动：**")
            out.append("")
            for item in notable[:20]:
                start, end, delta = item["max_event"]
                out.append(f"- {item['product']}：{start} → {end} 出现率变化 {delta:+.1f}pct；总体 {item['run_count']}/{item['category_runs']} 轮，置信度{item['confidence']}。")
            out.append("")
    out.extend([
        "## 五、使用限制",
        "",
        "1. 具体产品偏好仅基于已完成 AI 商品审核的推荐类回答；历史正文未归档的轮次不能补进产品分母。",
        "2. 最新数据日未结束，最新变化只能作为预警，不能作为最终趋势结论。",
        "3. 单次或低频产品可能是模型偶发写法、规格差异或新进入候选池，必须结合原始别名和证据复核。",
        "4. 产品关联信源属于运行级上下文；要判断哪条链接直接支持哪个产品，还需增加回答引用位置与产品句子的对齐。",
    ])
    path.write_text("\n".join(out), encoding="utf-8")


def markdown_to_docx(md_path, docx_path):
    from .analyze_doubao_category_preferences import markdown_to_docx as convert
    return convert(md_path, docx_path)


def main():
    products, events, snapshot, latest_day = analyze()
    stamp = latest_day or datetime.now(dashboard.CST).strftime("%Y-%m-%d")
    md_path = REPORT_DIR / f"豆包每个具体产品偏好与波动报告_{stamp}.md"
    docx_path = md_path.with_suffix(".docx")
    detail_path = REPORT_DIR / f"豆包每个具体产品偏好明细_{stamp}.csv"
    volatility_path = REPORT_DIR / f"豆包具体产品剧烈变化明细_{stamp}.csv"
    write_markdown(products, events, snapshot, latest_day, md_path)
    write_products_csv(products, detail_path)
    write_volatility_csv(events, volatility_path)
    markdown_to_docx(md_path, docx_path)
    print(json.dumps({
        "snapshot": snapshot,
        "latest_day": latest_day,
        "products": len(products),
        "high_confidence": sum(1 for item in products if item["confidence"] == "高"),
        "severe_products": sum(1 for item in events if item["level"] == "剧烈"),
        "markdown": str(md_path),
        "docx": str(docx_path),
        "details_csv": str(detail_path),
        "volatility_csv": str(volatility_path),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
