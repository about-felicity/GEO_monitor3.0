"""Unified question normalization aliases for capture and dashboard.

Both save_doubao_refs.py and doubao_dashboard_server.py load from here so
that capture-time normalization and display-time normalization never drift.
"""
import re


# Surface form -> canonical question name.
# Keep canonicals stable; they are the keys used by the dashboard dropdowns.
QUESTION_ALIASES = {
    # 染发剂
    "推荐一款染发剂": "染发剂推荐",
    "染发剂推荐": "染发剂推荐",
    "推荐染发剂": "染发剂推荐",

    # 面膜
    "推荐面膜": "面膜推荐",
    "面膜推荐": "面膜推荐",

    # 沐浴精油
    "沐浴精油推荐": "推荐沐浴精油",
    "沐浴油推荐": "推荐沐浴精油",
    "推荐沐浴油": "推荐沐浴精油",
    "推荐沐浴精油": "推荐沐浴精油",

    # 防脱精华液
    "推荐防脱精华液": "防脱精华液推荐",
    "防脱精华液推荐": "防脱精华液推荐",

    # 护发素
    "推荐护发素": "护发素推荐",
    "护发素推荐": "护发素推荐",

    # 祛痘精华
    "推荐祛痘精华": "推荐祛痘精华液",
    "祛痘精华推荐": "推荐祛痘精华液",
    "祛痘精华液推荐": "推荐祛痘精华液",
    "推荐祛痘精华液": "推荐祛痘精华液",

    # 控油蓬松洗发水
    "推荐控油蓬松洗发水": "推荐控油蓬松洗发水",

    # 眉毛增长液
    "推荐眉毛增长液": "推荐眉毛增长液",
    "推荐眉毛增长液 1": "推荐眉毛增长液",
    "推荐一款眉毛增长液": "推荐眉毛增长液",
    "眉毛增长液推荐": "推荐眉毛增长液",

    # 睫毛增长液
    "推荐睫毛增长液": "睫毛增长液推荐",
    "推荐一款睫毛增长液": "睫毛增长液推荐",
    "睫毛增长液推荐": "睫毛增长液推荐",

    # 眼霜
    "推荐眼霜": "推荐眼霜",
    "眼霜推荐": "推荐眼霜",

    # 二硫化硒洗发水
    "推荐二硫化硒洗发水": "二硫化硒洗发水推荐",
    "推荐一款二硫化硒洗发水": "二硫化硒洗发水推荐",
    "二硫化硒洗发水推荐": "二硫化硒洗发水推荐",

    # 护发精油
    "推荐护发精油": "护发精油推荐",
    "护发精油推荐": "护发精油推荐",

    # 产品评价类
    "梵玢染发剂评价": "梵玢染发剂怎么样",
    "首迷染发剂评价": "首迷染发剂评价",
    "首迷染发剂评价 1": "首迷染发剂评价",
    "首迷染发剂怎么样": "首迷染发剂评价",
    "JSV染发剂评价": "JSV染发剂评价",
    "JSV 染发剂怎么样": "JSV染发剂评价",
    "JSV染发剂怎么样": "JSV染发剂评价",
}


# Canonical keywords for questions that appear in both "推荐XX" and "XX推荐" forms.
# Used by the generic normalizer below when an exact alias is not registered.
_CANONICAL_QUESTION_KEYWORDS = {
    "染发剂推荐": ("染发", "染发剂", "染发膏", "染发霜"),
    "面膜推荐": ("面膜",),
    "推荐沐浴精油": ("沐浴", "沐浴油", "沐浴精油"),
    "防脱精华液推荐": ("防脱", "防脱精华"),
    "护发素推荐": ("护发素",),
    "推荐祛痘精华液": ("祛痘", "祛痘精华", "祛痘精华液"),
    "推荐控油蓬松洗发水": ("控油", "蓬松", "控油蓬松", "控油洗发水"),
    "推荐眉毛增长液": ("眉毛增长", "眉毛精华液"),
    "睫毛增长液推荐": ("睫毛增长", "睫毛精华液", "睫毛精华"),
    "推荐眼霜": ("眼霜",),
    "二硫化硒洗发水推荐": ("二硫化硒", "二硫化硒洗发水"),
    "护发精油推荐": ("护发精油", "护发精华油"),
    "洗面奶推荐": ("洗面奶", "洁面乳"),
    "防晒霜推荐": ("防晒", "防晒霜", "防晒乳"),
    "爽肤水推荐": ("爽肤水", "化妆水"),
    "推荐美白面霜": ("美白面霜", "美白霜"),
    "推荐身体乳": ("身体乳",),
    "护手霜推荐": ("护手霜",),
    "防脱洗发水推荐": ("防脱", "防脱洗发水"),
    "防断洗发水推荐": ("防断", "防断洗发水"),
    "推荐造型喷雾": ("造型喷雾", "定型喷雾", "发胶"),
}


def _clean_question_text(text):
    text = str(text or "").strip()
    if not text:
        return ""
    bad_fragments = ("Ctrl K", "搜索…", "搜索...", "豆包", "Doubao", "AI", "字节")
    if any(fragment.lower() in text.lower() for fragment in bad_fragments):
        return ""
    return text


def canonical_question_name(value):
    """Return the stable canonical question name for a surface form."""
    question = _clean_question_text(value)
    if not question:
        return ""

    if "推荐" in question:
        try:
            from monitor_core.recommendation_questions import canonical_recommendation_question
            recommendation = canonical_recommendation_question(question)
        except ImportError:
            recommendation = ""
        if recommendation:
            return recommendation

    # Exact alias first.
    if question in QUESTION_ALIASES:
        return QUESTION_ALIASES[question]

    # Strip trailing numeric suffix added by Doubao/ShadowBot UI (e.g. "推荐XX 1").
    suffix_match = re.fullmatch(r"(.+?)\s+([1-9]\d*)", question)
    if suffix_match:
        base = suffix_match.group(1).strip()
        if base in QUESTION_ALIASES:
            return QUESTION_ALIASES[base]

    # Remove "一款" and whitespace/punctuation for generic matching.
    compact = re.sub(r"[\s，。！？、：:；;（）()【】\[\]《》<>‘’“”\"']+", "", question)
    compact = compact.replace("一款", "")

    # Generic word-order + "推荐" normalization.
    has_recommend = "推荐" in compact
    for canonical, keywords in _CANONICAL_QUESTION_KEYWORDS.items():
        if has_recommend and "推荐" in canonical and not has_recommend:
            continue
        if all(kw in compact for kw in keywords):
            # Make sure the canonical direction matches what the user asked.
            if has_recommend or "推荐" not in canonical:
                return canonical

    # Fallback: just remove the generic "一款" prefix so "推荐一款XX" -> "推荐XX".
    no_yikuan = question.replace("一款", "").strip()
    if no_yikuan != question:
        return no_yikuan

    return question


def normalize_question_for_capture(value, chat_title=""):
    """Normalize a question at capture time.

    If the raw question has a numeric suffix that matches the chat title's
    canonical form, drop the suffix.  This handles Doubao creating titles like
    "推荐眉毛增长液 1" when a previous chat already exists.
    """
    question = str(value or "").strip()
    title = str(chat_title or "").strip()
    if not question:
        return question

    mapped = canonical_question_name(question)
    mapped_title = canonical_question_name(title)

    suffix_match = re.fullmatch(r"(.+?)\s+([1-9]\d*)", question)
    if suffix_match:
        base = canonical_question_name(suffix_match.group(1).strip())
        if mapped_title and mapped_title == base:
            return base

    return mapped
