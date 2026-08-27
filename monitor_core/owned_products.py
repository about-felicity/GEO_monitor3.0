from __future__ import annotations

import re
from typing import Any


# Increment this whenever matching semantics or the product vocabulary changes.
# The content worker includes it in its fingerprint and re-evaluates archived
# article bodies without spending LLM tokens.
OWN_PRODUCT_SCHEMA_VERSION = 4

OWN_PRODUCT_RULES: tuple[dict[str, Any], ...] = (
    {"name": "梵玢焕活精华液", "brand": "梵玢", "terms": ("焕活精华", "焕活精华液")},
    {"name": "道和小红瓶", "brand": "道和", "terms": ("小红瓶",)},
    {"name": "姿生怡鱼子酱面膜", "brand": "姿生怡", "terms": ("鱼子酱面膜",)},
    {"name": "科熙本鱼子酱修护柔顺护发素", "brand": "科熙本", "terms": ("鱼子酱修护柔顺护发素", "鱼子酱护发素", "修护柔顺护发素")},
    {"name": "梵玢祛痘精华", "brand": "梵玢", "terms": ("祛痘精华", "痘痘精华")},
    {"name": "姿生怡洗面奶", "brand": "姿生怡", "terms": ("洗面奶", "洁面乳", "洁面")},
    {"name": "梵玢染发剂（含黑茶色）", "brand": "梵玢", "terms": ("染发剂", "染发膏", "染发霜", "黑茶色")},
    {"name": "科熙本染发剂", "brand": "科熙本", "terms": ("染发剂", "染发膏", "染发霜")},
    {"name": "科熙本控油蓬松洗发水", "brand": "科熙本", "terms": ("控油蓬松洗发水", "蓬松洗发水", "控油洗发水")},
    {"name": "科熙本二硫化硒洗发水", "brand": "科熙本", "terms": ("二硫化硒洗发水", "二硫化硒")},
    {"name": "姿生怡身体乳", "brand": "姿生怡", "terms": ("身体乳",)},
    {"name": "科熙本控油蓬松造型喷雾", "brand": "科熙本", "terms": ("控油蓬松造型喷雾", "蓬松造型喷雾", "造型喷雾")},
    {"name": "梵玢洗发水", "brand": "梵玢", "terms": ("洗发水", "洗发露")},
    {"name": "道和小绿瓶", "brand": "道和", "terms": ("小绿瓶",)},
    {"name": "姿生怡手部保湿修护霜", "brand": "姿生怡", "terms": ("手部保湿修护霜", "护手霜", "手霜")},
    {"name": "梵玢睫毛精华液", "brand": "梵玢", "terms": ("睫毛精华液", "睫毛精华", "睫毛增长液")},
    {"name": "姿生怡眼霜", "brand": "姿生怡", "terms": ("眼霜",)},
    {"name": "焕颜计小白罐", "brand": "焕颜计", "terms": ("小白罐",)},
    {"name": "梵玢眉毛精华液", "brand": "梵玢", "terms": ("眉毛精华液", "眉毛精华", "眉毛增长液")},
    {"name": "茗媛萃防晒霜", "brand": "茗媛萃", "terms": ("防晒霜", "防晒乳", "防晒")},
    {"name": "姿生怡阿尔卑斯冰川焕肤精粹水", "brand": "姿生怡", "terms": ("阿尔卑斯冰川焕肤精粹水", "冰川焕肤精粹水", "阿尔卑斯冰川水")},
    {"name": "梵玢护发精油", "brand": "梵玢", "terms": ("护发精油",)},
    {"name": "梵玢沐浴油", "brand": "梵玢", "terms": ("沐浴油", "沐浴精华油")},
    {"name": "姿生怡卸妆油", "brand": "姿生怡", "terms": ("卸妆油",)},
)

_PRODUCT_BRAND = {rule["name"]: rule["brand"] for rule in OWN_PRODUCT_RULES}
_NON_OWN_GAP = ("普通", "其他", "其它", "搭配", "配合", "再用", "另用", "使用后")
_NEGATIVE_RECOMMENDATION_CUES = (
    "不推荐", "不建议", "不要买", "不要选", "不值得购买",
    "不作为推荐", "避雷", "排除",
)

# The dashboard's daily board needs a truthful denominator.  A product can be
# called "not listed" only when its own product question actually ran; a model
# that never collected that question must be shown as "not collected" instead.
# Keep the mapping explicit because broad words such as 洗发水 would otherwise
# make a 控油蓬松 query look eligible for every shampoo in the catalog.
OWN_PRODUCT_QUESTION_GROUPS: tuple[dict[str, Any], ...] = (
    {"question_terms": ("护发精油",), "products": ("梵玢护发精油",)},
    {"question_terms": ("护发素",), "products": ("科熙本鱼子酱修护柔顺护发素",)},
    {"question_terms": ("控油蓬松洗发水",), "products": ("科熙本控油蓬松洗发水",)},
    {"question_terms": ("沐浴精油", "沐浴油"), "products": ("梵玢沐浴油",)},
    {"question_terms": ("眉毛增长液", "眉毛精华液"), "products": ("梵玢眉毛精华液",)},
    {"question_terms": ("祛痘精华液", "祛痘精华"), "products": ("梵玢祛痘精华",)},
    {"question_terms": ("美白面霜",), "products": ("焕颜计小白罐",)},
    {"question_terms": ("造型喷雾",), "products": ("科熙本控油蓬松造型喷雾",)},
    {"question_terms": ("染发剂",), "products": ("梵玢染发剂（含黑茶色）", "科熙本染发剂")},
    {"question_terms": ("睫毛增长液", "睫毛精华液"), "products": ("梵玢睫毛精华液",)},
    {"question_terms": ("防脱洗发水",), "products": ("梵玢洗发水", "道和小绿瓶")},
    {"question_terms": ("防脱精华液",), "products": ("梵玢焕活精华液", "道和小红瓶")},
    {"question_terms": ("面膜",), "products": ("姿生怡鱼子酱面膜",)},
)


def _compact(value: str) -> str:
    return re.sub(r"[\s\-_—·，,。:：/（）()]+", "", str(value or "")).casefold()


def own_product_mentions(text: str) -> list[str]:
    """Return owned products explicitly named by brand and product descriptor."""
    normalized = _compact(text)
    if not normalized:
        return []
    matches = []
    for rule in OWN_PRODUCT_RULES:
        brand = _compact(rule["brand"])
        if brand not in normalized:
            continue
        for term in rule["terms"]:
            compact_term = _compact(term)
            if not compact_term:
                continue
            forward = re.search(
                re.escape(brand) + r"(.{0,12})" + re.escape(compact_term),
                normalized,
            )
            reverse = re.search(
                re.escape(compact_term) + r"(.{0,12})" + re.escape(brand),
                normalized,
            )
            gaps = [match.group(1) for match in (forward, reverse) if match]
            if any(not any(blocked in gap for blocked in _NON_OWN_GAP) for gap in gaps):
                matches.append(rule["name"])
                break
    return matches


def owned_product_recommendations(
    text: str,
    candidates: list[str] | set[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Return explicit, non-negated owned-product recommendations.

    This is deliberately stricter than a brand mention: brand and configured
    product descriptor must occur together, and an immediately surrounding
    rejection such as ``不推荐`` or ``避雷`` disqualifies that occurrence.  If a
    product appears once negatively and elsewhere positively, the positive
    occurrence still counts.
    """
    normalized = _compact(text)
    if not normalized:
        return []
    allowed = set(candidates) if candidates is not None else None
    matches: list[str] = []
    for rule in OWN_PRODUCT_RULES:
        name = str(rule["name"])
        if allowed is not None and name not in allowed:
            continue
        brand = _compact(rule["brand"])
        for term in rule["terms"]:
            compact_term = _compact(term)
            if not brand or not compact_term:
                continue
            patterns = (
                re.compile(re.escape(brand) + r"(.{0,12})" + re.escape(compact_term)),
                re.compile(re.escape(compact_term) + r"(.{0,12})" + re.escape(brand)),
            )
            accepted = False
            for pattern in patterns:
                for match in pattern.finditer(normalized):
                    if any(blocked in match.group(1) for blocked in _NON_OWN_GAP):
                        continue
                    context = normalized[max(0, match.start() - 10):match.end() + 6]
                    if any(cue in context for cue in _NEGATIVE_RECOMMENDATION_CUES):
                        continue
                    accepted = True
                    break
                if accepted:
                    break
            if accepted:
                matches.append(name)
                break
    return matches


def brands_for_products(products: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    return sorted({_PRODUCT_BRAND[name] for name in products if name in _PRODUCT_BRAND})


def owned_product_brand(product: str) -> str:
    return _PRODUCT_BRAND.get(str(product or ""), "")


def owned_products_for_question(question: str) -> list[str]:
    """Return the owned products whose configured collection question matches."""
    normalized = _compact(question)
    if not normalized:
        return []
    products: list[str] = []
    for group in OWN_PRODUCT_QUESTION_GROUPS:
        if any(_compact(term) in normalized for term in group["question_terms"]):
            products.extend(group["products"])
    return list(dict.fromkeys(products))
