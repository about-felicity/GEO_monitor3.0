from __future__ import annotations

import re


PRODUCTS: tuple[str, ...] = (
    "护发精油",
    "护发素",
    "控油蓬松洗发水",
    "沐浴精油",
    "眉毛增长液",
    "祛痘精华液",
    "美白面霜",
    "造型喷雾",
    "染发剂",
    "睫毛增长液",
    "防脱洗发水",
    "防脱精华液",
    "面膜",
)

PROMPTS: tuple[str, ...] = tuple(f"推荐一款{product}" for product in PRODUCTS)
CANONICAL_QUESTIONS: tuple[str, ...] = tuple(f"{product}推荐" for product in PRODUCTS)

_ALIASES = {
    "沐浴油": "沐浴精油",
    "定型喷雾": "造型喷雾",
    "眉毛精华液": "眉毛增长液",
    "睫毛精华液": "睫毛增长液",
    "祛痘精华": "祛痘精华液",
}


def _compact(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def canonical_recommendation_question(value: str) -> str:
    """Map prompts and generated chat titles to one of the 13 allowed buckets."""
    text = _compact(value)
    for alias, product in _ALIASES.items():
        text = text.replace(_compact(alias), _compact(product))
    # Longest first prevents 护发素/洗发水 fragments from stealing a match.
    for product in sorted(PRODUCTS, key=len, reverse=True):
        if _compact(product) in text:
            return f"{product}推荐"
    return ""


def prompt_for_question(value: str) -> str:
    canonical = canonical_recommendation_question(value)
    if canonical not in CANONICAL_QUESTIONS:
        raise ValueError(f"问题不在允许的 13 个推荐问题中：{value}")
    return "推荐一款" + canonical.removesuffix("推荐")


def validate_prompt_list(values: list[str]) -> list[str]:
    prompts = [prompt_for_question(value) for value in values if str(value or "").strip()]
    unknown = [value for value in values if str(value or "").strip() and not canonical_recommendation_question(value)]
    if unknown:
        raise ValueError("只允许配置既定的 13 个推荐问题：" + "、".join(unknown))
    return list(dict.fromkeys(prompts))
