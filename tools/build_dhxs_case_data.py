from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "runtime" / "case_dhxs_report.json"
OUTPUT = ROOT.parent / "anli" / "src" / "data" / "dhxsModelDashboardData.json"
TARGET = "大海鲜生"
MODEL_IDS = ("doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi")
MODEL_NAMES = {
    "doubao": "豆包", "yuanbao": "腾讯元宝", "wenxin": "文心一言",
    "quark": "千问", "deepseek": "DeepSeek", "kimi": "Kimi",
}


def compact(value: object) -> str:
    return re.sub(r"\W+", "", str(value or "")).casefold()


def brand_rank(answer: dict, brand: str, target: str) -> int:
    if brand == target:
        try:
            return max(0, int(answer.get("rank") or 0))
        except (TypeError, ValueError):
            return 0
    needle = compact(brand)
    for index, item in enumerate((answer.get("analysis") or {}).get("products") or [], 1):
        if not isinstance(item, dict):
            continue
        values = (item.get("brand"), item.get("name"))
        if any(needle and (needle in compact(value) or compact(value) in needle) for value in values):
            try:
                return max(0, int(item.get("rank") or index))
            except (TypeError, ValueError):
                return index
    return 0


def brand_mentioned(answer: dict, brand: str, target: str) -> bool:
    if brand == target:
        return bool(answer.get("mentioned") or answer.get("recommended"))
    needle = compact(brand)
    analysis = answer.get("analysis") or {}
    values = list(analysis.get("brands") or [])
    for item in analysis.get("products") or []:
        if isinstance(item, dict):
            values.extend((item.get("brand"), item.get("name")))
    return any(needle and (needle in compact(value) or compact(value) in needle) for value in values)


def answer_score(answer: dict, brand: str, target: str) -> float:
    rank = brand_rank(answer, brand, target)
    if rank == 1:
        return 92.0
    if rank == 2:
        return 80.0
    if rank == 3:
        return 68.0
    if rank > 3:
        return max(38.0, 58.0 - (rank - 4) * 5.0)
    if brand_mentioned(answer, brand, target):
        return 30.0
    return min(18.0, 6.0 + len(answer.get("sources") or []) * .6)


def competitor_row(name: str, answers: list[dict], target: str) -> dict:
    hits = [answer for answer in answers if brand_mentioned(answer, name, target)]
    ranks = [brand_rank(answer, name, target) for answer in hits]
    ranks = [rank for rank in ranks if rank > 0]
    top3 = sum(rank <= 3 for rank in ranks)
    total = max(1, len(answers))
    return {
        "name": name,
        "mentions": len(hits),
        "mentionRate": round(len(hits) / total, 4),
        "top3": top3,
        "top3Rate": round(top3 / total, 4),
        "avgRank": round(sum(ranks) / len(ranks), 2) if ranks else 0,
        "bestRank": min(ranks) if ranks else 0,
        "rounds": sorted({int(answer.get("round") or 0) for answer in hits}),
        "snippet": next((str(answer.get("answer") or "")[:350] for answer in hits), "本次真实抽样回答中未出现该品牌。"),
    }


def source_type(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    return "视频" if any(value in host for value in ("douyin", "bilibili", "kuaishou", "youtube")) else "文章"


def source_rows(answers: list[dict], answer_samples: int, target: str) -> tuple[list[dict], dict]:
    grouped: dict[str, dict] = {}
    for answer in answers:
        model_name = MODEL_NAMES.get(str(answer.get("model") or ""), str(answer.get("model") or ""))
        seen: set[str] = set()
        for item in answer.get("sources") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("href") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = " ".join(str(item.get("title") or "").split()) or (urlparse(url).hostname or url)
            row = grouped.setdefault(url, {"title": title, "models": set(), "occurrences": 0})
            if len(title) > len(row["title"]):
                row["title"] = title
            row["models"].add(model_name)
            row["occurrences"] += 1
    links = []
    for url, row in sorted(grouped.items(), key=lambda pair: (-pair[1]["occurrences"], pair[0])):
        host = urlparse(url).hostname or ""
        owned = compact(target) in compact(row["title"])
        links.append({
            "title": row["title"], "platform": "、".join(sorted(row["models"])),
            "domain": host, "url": url, "type": source_type(url),
            "occurrences": row["occurrences"], "owned": owned,
            "ownedReason": f"标题明确命中{target}" if owned else "", "publishDate": "",
            "restaurants": [target] if owned else [],
            "share": round(row["occurrences"] / max(1, answer_samples), 4),
        })
    article = sum(link["occurrences"] for link in links if link["type"] == "文章")
    video = sum(link["occurrences"] for link in links if link["type"] == "视频")
    owned_links = [link for link in links if link["owned"]]
    metrics = {
        "referenceRecords": article + video, "uniqueSources": len(links),
        "articleReferenceOccurrences": article, "videoReferenceOccurrences": video,
        "ownedSourceCount": len(owned_links),
        "ownedSourceAverageRate": round(sum(link["occurrences"] for link in owned_links) / max(1, answer_samples) / len(owned_links), 4) if owned_links else 0,
        "answerSamples": answer_samples,
    }
    return links, metrics


def trend_points(answers: list[dict], names: list[str], target: str, now: datetime) -> list[dict]:
    rounds = sorted({int(answer.get("round") or 0) for answer in answers if int(answer.get("round") or 0) > 0})
    output = []
    for index, round_number in enumerate(rounds):
        rows = [answer for answer in answers if int(answer.get("round") or 0) == round_number]
        values = {
            f"brand{brand_index + 1}": round(sum(answer_score(answer, name, target) for answer in rows) / max(1, len(rows)), 1)
            for brand_index, name in enumerate(names)
        }
        day = now.date() - timedelta(days=len(rounds) - index - 1)
        output.append({
            "date": day.isoformat(), "label": day.strftime("%m-%d"), "sampleSize": len(rows),
            **values, "probability": values["brand1"], "phase": "采样期",
        })
    if len(output) > 1:
        deltas = [output[index]["brand1"] - output[index - 1]["brand1"] for index in range(1, len(output))]
        signal_index = max(range(1, len(output)), key=lambda index: abs(deltas[index - 1]))
        output[signal_index]["phase"] = "信号跃升"
    return output


def main() -> int:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    report = payload.get("report", payload)
    task = payload.get("task") or {}
    target = str(report.get("brand_name") or TARGET)
    answers = [answer for answer in report.get("answers") or [] if str(answer.get("model") or "") in MODEL_IDS]
    if not answers:
        raise SystemExit("报告中没有可用的大模型回答")
    model_ids = [model for model in MODEL_IDS if any(str(answer.get("model")) == model for answer in answers)]
    if model_ids != list(MODEL_IDS):
        raise SystemExit(f"六个平台数据不完整：{model_ids}")

    report_competitors = [str(item.get("name") or "").strip() for item in report.get("competitors") or []]
    competitor_names = [target] + [name for name in report_competitors if name and compact(name) != compact(target)]
    competitor_names = list(dict.fromkeys(competitor_names))[:10]
    while len(competitor_names) < 3:
        competitor_names.append(f"其他餐饮品牌{len(competitor_names)}")
    trend_names = competitor_names[:3]
    now = datetime.now().astimezone()

    competitors = [competitor_row(name, answers, target) for name in competitor_names]
    competitors.sort(key=lambda row: (row["name"] != target, -row["mentions"], -row["top3"], row["name"]))
    target_row = next(row for row in competitors if row["name"] == target)
    competitors_by_platform = {}
    platform_trends = {}
    source_links_by_platform = {}
    source_metrics_by_platform = {}
    for model in model_ids:
        model_name = MODEL_NAMES[model]
        rows = [answer for answer in answers if str(answer.get("model")) == model]
        platform_competitors = [competitor_row(name, rows, target) for name in competitor_names]
        platform_competitors.sort(key=lambda row: (row["name"] != target, -row["mentions"], -row["top3"], row["name"]))
        competitors_by_platform[model_name] = platform_competitors
        platform_trends[model_name] = trend_points(rows, trend_names, target, now)
        links, metrics = source_rows(rows, len(rows), target)
        source_links_by_platform[model_name] = links
        source_metrics_by_platform[model_name] = metrics

    source_links, source_metrics = source_rows(answers, len(answers), target)
    probability_trend = trend_points(answers, trend_names, target, now)
    recommendation_mentions = sum(row["mentions"] for row in competitors)
    top3_recommendations = sum(row["top3"] for row in competitors)
    metrics = {
        "answerSamples": len(answers), "recommendationMentions": recommendation_mentions,
        "top3Recommendations": top3_recommendations,
        "top3Rate": round(top3_recommendations / recommendation_mentions, 4) if recommendation_mentions else 0,
        **source_metrics, "roundCount": len(answers), "observedRoundCount": len(answers),
        "targetRoundCount": len(answers), "competitorCount": len(competitors),
        "targetMentions": target_row["mentions"], "targetTop3": target_row["top3"],
    }
    output = {
        "query": str(report.get("question") or "文山市最有性价比的海鲜餐厅有哪些？"),
        "generatedAt": now.isoformat(timespec="seconds"), "capturedAt": now.isoformat(timespec="seconds"),
        "asOf": now.date().isoformat(), "targetBrand": target,
        "monitoringPlatforms": [MODEL_NAMES[model] for model in model_ids],
        "actualPlatforms": [MODEL_NAMES[model] for model in model_ids],
        "metrics": metrics,
        "trendSeries": [{"key": f"brand{index + 1}", "name": name} for index, name in enumerate(trend_names)],
        "trend": probability_trend, "probabilityTrend": probability_trend,
        "platformProbabilityTrends": platform_trends, "competitors": competitors,
        "competitorsByPlatform": competitors_by_platform, "rounds": [],
        "sourceLinks": source_links, "sourceLinksByPlatform": source_links_by_platform,
        "sourceMetricsByPlatform": source_metrics_by_platform,
        "taskId": str(task.get("id") or ""),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} from {len(answers)} real model answers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
