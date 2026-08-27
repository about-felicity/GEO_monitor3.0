"""Low-token, evidence-grounded product analysis.

The fast path deliberately stays conservative: it reuses a previously verified
identical answer or accepts a deterministic extraction only when two independent
extractors agree and every product is grounded in the captured answer.  Anything
ambiguous is returned to the caller for model fallback instead of being guessed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import copy
import hashlib
import json
import os
import re
import urllib.request
from typing import Any, Iterable


INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
COMPACT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+", re.I)


def clean_text(value: Any) -> str:
    return INVISIBLE_RE.sub("", str(value or "")).strip()


def compact(value: Any) -> str:
    return COMPACT_RE.sub("", clean_text(value)).casefold()


def answer_key(question: Any, answer: Any) -> tuple[str, str]:
    # Whitespace and invisible UI characters are not analytical differences.
    # Keeping every full normalized answer as a dict key duplicated the whole
    # verified corpus in the long-running worker. A SHA-256 identity preserves
    # exact reuse semantics while using a fixed 64 bytes per answer.
    normalized_answer = compact(answer)
    return compact(question), hashlib.sha256(
        normalized_answer.encode("utf-8")
    ).hexdigest()


def product_key(item: dict[str, Any]) -> str:
    return compact(item.get("product_name") or item.get("product") or "")


def equivalent_name(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 4 and shorter in longer


def equivalent_sets(left: Iterable[dict[str, Any]], right: Iterable[dict[str, Any]]) -> bool:
    left_keys = [product_key(item) for item in left if product_key(item)]
    right_keys = [product_key(item) for item in right if product_key(item)]
    return (
        len(left_keys) == len(right_keys)
        and all(any(equivalent_name(value, other) for other in right_keys) for value in left_keys)
        and all(any(equivalent_name(value, other) for other in left_keys) for value in right_keys)
    )


@dataclass
class AnalysisKnowledge:
    exact: dict[tuple[str, str], list[dict[str, Any]]]
    catalog: dict[str, list[dict[str, Any]]]


def build_knowledge(rows: Iterable[dict[str, Any]]) -> AnalysisKnowledge:
    """Build reusable knowledge from already model-verified database rows."""
    exact: dict[tuple[str, str], list[dict[str, Any]]] = {}
    observations: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        question = clean_text(row.get("question"))
        answer = clean_text(row.get("answer"))
        products = [copy.deepcopy(item) for item in row.get("products") or [] if isinstance(item, dict)]
        if not question or not answer:
            continue
        exact[answer_key(question, answer)] = products
        for item in products:
            key = product_key(item)
            if key:
                observations[compact(question)][key].append(item)

    catalog: dict[str, list[dict[str, Any]]] = {}
    for question, groups in observations.items():
        values = []
        for key, items in groups.items():
            # The most frequently verified spelling/brand becomes canonical.
            signatures = Counter(
                (clean_text(item.get("product_name")), clean_text(item.get("brand_name")))
                for item in items
            )
            (name, brand), frequency = signatures.most_common(1)[0]
            values.append({"product_name": name, "brand_name": brand, "key": key,
                           "frequency": len(items) + frequency / 1000})
        catalog[question] = sorted(values, key=lambda item: (-len(item["key"]), -item["frequency"]))
    return AnalysisKnowledge(exact=exact, catalog=catalog)


def _line_evidence(answer: str, name: str) -> str:
    target = compact(name)
    for line in answer.splitlines():
        line = clean_text(line)
        if target and target in compact(line):
            return line[:240]
    return name[:240]


def catalog_products(answer: str, question: str, knowledge: AnalysisKnowledge) -> list[dict[str, Any]]:
    """Find previously verified products literally present in this answer."""
    normalized_answer = compact(answer)
    candidates = []
    for item in knowledge.catalog.get(compact(question), []):
        if item["key"] and item["key"] in normalized_answer:
            candidates.append(item)

    # Collapse short/long aliases of the same mention. Prefer the longest exact
    # name, then the historically most frequent spelling.
    selected: list[dict[str, Any]] = []
    for item in candidates:
        if any(equivalent_name(item["key"], existing["key"]) for existing in selected):
            continue
        selected.append(item)
    selected.sort(key=lambda item: normalized_answer.find(item["key"]))
    products = []
    for rank, item in enumerate(selected, 1):
        products.append({
            "product_name": item["product_name"],
            "brand_name": item["brand_name"],
            "brand_identified": bool(item["brand_name"]),
            "evidence": _line_evidence(answer, item["product_name"]),
            "rank": rank,
            "rank_type": "appearance_order",
        })
    return products


def deterministic_review(answer: str, question: str, knowledge: AnalysisKnowledge,
                         rule_products: list[dict[str, Any]], expected_blocks: int = 0
                         ) -> tuple[list[dict[str, Any]] | None, str]:
    """Return a certified result, or ``None`` when model review is required."""
    key = answer_key(question, answer)
    if key in knowledge.exact:
        return copy.deepcopy(knowledge.exact[key]), "verified_answer_reuse"

    known = catalog_products(answer, question, knowledge)
    if not known or not rule_products or not equivalent_sets(known, rule_products):
        return None, "ambiguous"
    if expected_blocks and len(known) != expected_blocks:
        return None, "incomplete_structure"
    return known, "dual_parser_consensus"


def compact_model_text(answer: str, question: str, known: list[dict[str, Any]] | None = None) -> str:
    """Keep recommendation-bearing lines while dropping verbose usage prose."""
    import save_doubao_refs as saver

    text = clean_text(saver.strip_reference_prefix(answer))
    if len(text) <= 1800:
        return text
    lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
    known_keys = [product_key(item) for item in known or [] if product_key(item)]
    question_terms = [term for term in re.findall(r"[\u4e00-\u9fff]{2,}", clean_text(question))]
    anchors: set[int] = set()
    for index, line in enumerate(lines):
        folded = compact(line)
        structural = bool(
            re.match(r"^(?:\d{1,2}|[一二三四五六七八九十]+)[.、：:）)]", line)
            or re.search(r"(?:首选|推荐|优选|平价|高端|核心成分|参考价|适合人群|产品名)\s*[：:]", line)
        )
        grounded = any(key in folded for key in known_keys)
        category = len(line) <= 140 and any(term in line for term in question_terms)
        if structural or grounded or category:
            anchors.update(range(max(0, index - 1), min(len(lines), index + 3)))
    excerpt = "\n".join(line[:500] for index, line in enumerate(lines) if index in anchors)
    # Unstructured answers need a bounded complete prefix rather than a lossy
    # excerpt. Most product names appear before detailed usage advice.
    if len(excerpt) < 80:
        excerpt = text[:3500]
    return excerpt[:5000]


def merge_explicit_owned_products(
    answer: str,
    question: str,
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge conservative owned-product body matches into a reviewed result.

    Paid extraction remains the authority for arbitrary competitors.  Owned
    products are a small configured vocabulary, however, and require both the
    owned brand and the question-specific product descriptor in positive
    recommendation prose.  Keeping this deterministic reconciliation after
    model review prevents a valid model response from silently omitting an
    owned recommendation while still excluding related-video/source titles.
    """
    from monitor_core.analytics import recommendation_body
    from monitor_core.owned_products import (
        own_product_mentions,
        owned_product_brand,
        owned_product_recommendations,
        owned_products_for_question,
    )

    body = recommendation_body(answer)
    candidates = owned_products_for_question(question)
    # Do not compact the complete answer before owned-product reconciliation:
    # a category heading ending in “沐浴油” followed by an unrelated owned
    # brand on the next line used to become one synthetic match.  A product
    # recommendation must be present in one literal body line.
    explicit = []
    for line in body.splitlines():
        for product in owned_product_recommendations(line, candidates):
            if product not in explicit:
                explicit.append(product)
    merged = [copy.deepcopy(item) for item in products if isinstance(item, dict)]
    if not explicit:
        return merged

    def item_brand(item: dict[str, Any]) -> str:
        return clean_text(item.get("brand_name") or item.get("brand"))

    def item_name(item: dict[str, Any]) -> str:
        return clean_text(item.get("product_name") or item.get("product") or item.get("name"))

    def matching_evidence(product: str) -> str:
        for line in body.splitlines():
            if owned_product_recommendations(line, [product]):
                return clean_text(line)[:240]
        return ""

    for product in explicit:
        already_present = any(
            product in own_product_mentions(f"{item_brand(item)} {item_name(item)}")
            for item in merged
        )
        if already_present:
            continue
        brand = owned_product_brand(product)
        evidence = matching_evidence(product)
        if not evidence:
            continue
        # If the reviewer returned a generic name for the same owned brand,
        # make that row precise instead of creating a duplicate product.
        same_brand = next((
            item for item in merged
            if compact(brand) and compact(item_brand(item)) and (
                compact(brand) in compact(item_brand(item))
                or compact(item_brand(item)) in compact(brand)
            )
        ), None)
        if same_brand is not None:
            same_brand.update({
                "brand": brand,
                "brand_name": brand,
                "brand_identified": True,
                "product": product,
                "product_name": product,
                "evidence": evidence,
                "owned_product_reconciled": True,
            })
            continue
        merged.append({
            "brand": brand,
            "brand_name": brand,
            "brand_identified": True,
            "product": product,
            "product_name": product,
            "evidence": evidence,
            "rank_type": "appearance_order",
            "owned_product_reconciled": True,
        })

    # Keep appearance order stable, including reconciled rows that the model
    # omitted. Evidence is always a literal body excerpt for these additions.
    folded_body = compact(body)
    merged.sort(key=lambda item: (
        folded_body.find(compact(item.get("evidence") or item_name(item)))
        if compact(item.get("evidence") or item_name(item)) in folded_body
        else len(folded_body) + int(item.get("rank") or 0),
    ))
    for rank, item in enumerate(merged, 1):
        item["rank"] = rank
        item.setdefault("rank_type", "appearance_order")
    return merged


def batch_model_review(items: list[dict[str, Any]], knowledge: AnalysisKnowledge
                       ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Review several ambiguous answers in one compact DeepSeek request."""
    if not items:
        return {}, {"calls": 0}
    import save_doubao_refs as saver

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("missing ANTHROPIC_API_KEY for product fallback")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic").rstrip("/")
    model = os.environ.get("DOUBAO_PRODUCT_AI_MODEL", "deepseek-v4-flash").strip() or "deepseek-v4-flash"
    prompt_items = []
    original_chars = 0
    compact_chars = 0
    for item in items:
        answer = clean_text(item.get("answer"))
        question = clean_text(item.get("question"))
        known = catalog_products(answer, question, knowledge)
        if item.get("full_context"):
            # A row rejected by the first grounded pass gets one richer view.
            # Only retry rows pay this token cost; references remain excluded.
            excerpt = clean_text(saver.strip_reference_prefix(answer))[:8000]
        else:
            excerpt = compact_model_text(answer, question, known)
        original_chars += len(answer)
        compact_chars += len(excerpt)
        prompt_items.append({
            "id": str(item["id"]), "question": question, "text": excerpt,
            "review": "full_context_retry" if item.get("full_context") else "compact_first_pass",
        })
    prompt = {
        "task": "逐条提取回答正文明确推荐的、且属于问题品类的全部商品。不要推测。",
        "rules": [
            "忽略参考资料、相关文章/视频、购买卡片中的额外推荐和使用建议。",
            "不推荐、不建议、避雷、排除、慎选或仅作反例的商品绝不能返回。",
            "同一商品正文与卡片重复时只保留一次；按正文首次出现排序。",
            "evidence必须是该条text中的最短原文；品牌不确定就留空。",
            "不要把黄金浓度、黄金组合等描述词当成品牌。",
            "每个输入id都必须返回；确无明确商品才返回空products。",
        ],
        "schema": {"results": [{"id": "原id", "products": [
            {"product_name": "原文商品名", "brand": "主品牌或空", "evidence": "最短原文"}
        ]}]},
        "items": prompt_items,
    }
    body = {
        "model": model,
        "max_tokens": min(8000, max(800, 180 + len(items) * 600)),
        "temperature": 0,
        "system": "只返回一个合法JSON对象，不要Markdown和解释。",
        "messages": [{"role": "user", "content": json.dumps(
            prompt, ensure_ascii=False, separators=(",", ":"))}],
    }
    body.update(saver.deepseek_thinking_options(base_url))
    request = urllib.request.Request(
        base_url + "/v1/messages",
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=saver.env_int("DOUBAO_AI_PRODUCT_TIMEOUT", 90)) as response:
        data = json.loads(response.read().decode("utf-8", errors="ignore"))
    parsed = saver.parse_json_object(saver.model_response_text(data))
    raw_results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(raw_results, list):
        raise ValueError("batch product response has no results list")
    by_id = {str(item["id"]): item for item in items}
    raw_by_id = {
        str(raw.get("id")): raw
        for raw in raw_results
        if isinstance(raw, dict) and str(raw.get("id")) in by_id
    }
    results: dict[str, list[dict[str, Any]]] = {}
    rejected: dict[str, str] = {}
    for item_id, source in by_id.items():
        raw = raw_by_id.get(item_id)
        if raw is None:
            rejected[item_id] = "model omitted this id"
            continue
        try:
            answer = clean_text(source.get("answer"))
            question = clean_text(source.get("question"))
            products = saver.normalize_ai_products({"products": raw.get("products") or []})
            products = saver.filter_products_for_question(question, products)
            structured = saver.filter_products_for_question(
                question,
                saver.extract_structured_product_headings(answer, question),
            )
            for candidate in structured:
                candidate_key = product_key(candidate)
                if candidate_key and not any(
                    equivalent_name(candidate_key, product_key(existing))
                    for existing in products
                    if product_key(existing)
                ):
                    products.append(candidate)
            products = saver.ground_product_brands(answer, products)
            products = merge_explicit_owned_products(answer, question, products)
            products = saver.ensure_complete_ai_products(answer, raw, products)
            saver.validate_grounded_ai_products(answer, products)
            if not products and not saver.credible_empty_product_result(answer):
                raise ValueError("implausible empty product result")
            results[item_id] = products
        except (KeyError, TypeError, ValueError) as exc:
            # One uncertain answer must not discard the other seven verified
            # answers from the same paid request. Keep only this row pending.
            rejected[item_id] = str(exc)[:240]
    usage = data.get("usage") or {}
    return results, {
        "calls": 1,
        "model": model,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "original_chars": original_chars,
        "compact_chars": compact_chars,
        "items": len(items),
        "accepted_items": len(results),
        "rejected_items": len(rejected),
        "rejected": rejected,
    }
