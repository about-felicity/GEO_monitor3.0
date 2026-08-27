from __future__ import annotations

import re


ERROR_MARKERS = (
    "系统异常", "服务异常", "网络异常", "出了点问题", "请稍后重试",
    "生成失败", "回答失败", "设备环境有风险", "重新尝试请求",
    "server error", "system error", "try again later",
)

REFUSAL_MARKERS = (
    "不在我的医疗健康服务范围内", "无法为你推荐", "不能为你推荐",
    "无法提供相关产品推荐", "不能提供相关产品推荐",
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def invalid_answer_reason(value: str, *, minimum_length: int = 12) -> str:
    text = str(value or "").strip()
    compact = normalize_text(text)
    if not compact:
        return "模型回答为空"
    if any(normalize_text(marker) in compact for marker in ERROR_MARKERS):
        return "模型返回系统或服务异常"
    if any(normalize_text(marker) in compact for marker in REFUSAL_MARKERS):
        return "模型拒绝或无法完成产品推荐"
    # Yuanbao can leave only its recommendation chips / download footer in the
    # captured message container while the real answer failed to hydrate.  The
    # old minimum-length check accepted that shell as a successful answer.
    footer_markers = ("下载元宝电脑版", "前往下载中心", "推荐问题")
    if any(normalize_text(marker) in compact for marker in footer_markers) and (
        len(compact) < 300
        or not re.search(r"(?:核心|成分|功效|适合|推荐|首选).{2,}", text)
    ):
        return "只抓到页面推荐词或下载页尾，未抓到模型正文"
    if len(compact) < minimum_length:
        return f"模型回答过短（{len(compact)} 字）"
    return ""


def expected_topic(question: str) -> str:
    text = re.sub(r"\s+", "", str(question or ""))
    text = re.sub(r"^(?:请|麻烦|帮我|给我|想要|需要)*推荐(?:一款|一个|一种)?", "", text)
    text = re.sub(r"推荐$", "", text)
    return text.strip("，。！？?：:")

def topic_matches(topic: str, body: str) -> bool:
    # 同一品类在网页回答里经常使用商品通用名，而不是逐字复述问题。
    # 例如“眉毛增长液”通常写作“眉毛精华液/育眉液”；这应视为同主题。
    semantic_aliases = {
        "护发精油": ("护发油", "润发油", "发油", "护发精华油"),
        "沐浴精油": ("沐浴油", "精油沐浴露", "油基沐浴露"),
        "眉毛增长液": ("眉毛生长液", "眉毛精华液", "眉毛护理液", "眉毛植萃精华液", "育眉液", "养眉液", "密眉精华"),
        "眉毛生长液": ("眉毛增长液", "眉毛精华液", "眉毛护理液", "育眉液", "养眉液", "密眉精华"),
        "睫毛增长液": ("睫毛生长液", "睫毛精华液", "睫毛护理液", "育睫液", "养睫液"),
        "睫毛生长液": ("睫毛增长液", "睫毛精华液", "睫毛护理液", "育睫液", "养睫液"),
        "祛痘精华液": ("祛痘精华", "抗痘精华", "净痘精华", "痘痘精华"),
        "祛痘精华": ("祛痘精华液", "抗痘精华", "净痘精华", "痘痘精华"),
    }
    if any(normalize_text(alias) in body for alias in semantic_aliases.get(topic, ())):
        return True

    # 祛痘产品常按主要酸类或具体痘型命名，例如“三酸精华”，回答中未必
    # 逐字写出“祛痘精华液”。同时要求产品形态和祛痘证据，避免仅凭“精华”
    # 或仅凭“痘”放行美白精华、洁面等跨品类回答。
    semantic_evidence_groups = {
        "祛痘精华液": (
            ("祛痘", "抗痘", "净痘", "痘痘", "痘肌", "痤疮", "闭口", "粉刺", "红肿痘", "爆痘"),
            ("精华", "水杨酸", "壬二酸", "果酸", "杏仁酸", "三酸"),
        ),
        "祛痘精华": (
            ("祛痘", "抗痘", "净痘", "痘痘", "痘肌", "痤疮", "闭口", "粉刺", "红肿痘", "爆痘"),
            ("精华", "水杨酸", "壬二酸", "果酸", "杏仁酸", "三酸"),
        ),
    }
    evidence_groups = semantic_evidence_groups.get(topic, ())
    if evidence_groups and all(
        any(normalize_text(marker) in body for marker in group)
        for group in evidence_groups
    ):
        return True
    variants = {topic}
    if "增长" in topic:
        variants.add(topic.replace("增长", "生长"))
    if "生长" in topic:
        variants.add(topic.replace("生长", "增长"))
    if any(value in body for value in variants):
        return True
    bigrams = {value[index:index + 2] for value in variants for index in range(len(value) - 1)}
    required = 1 if len(topic) <= 3 else 2
    aliases = {"控油": ("油头", "去油", "清爽"), "增长": ("生长",), "生长": ("增长",)}
    matched = sum(
        1 for value in bigrams
        if value in body or any(alias in body for alias in aliases.get(value, ()))
    )
    return matched >= required

def answer_quality_reason(question: str, answer: str, *, minimum_length: int = 12) -> str:
    """Reject empty/error replies and obvious cross-topic conversation mix-ups."""
    reason = invalid_answer_reason(answer, minimum_length=minimum_length)
    if reason:
        return reason
    topic = normalize_text(expected_topic(question))
    body = normalize_text(answer)
    if len(topic) >= 2:

        if not topic_matches(topic, body):

            return f"回答主题与问题不一致（未能确认“{expected_topic(question)}”相关内容）"
    return ""
