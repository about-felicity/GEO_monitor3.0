from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SiteConfig:
    id: str
    name: str
    home_url: str
    port: int
    internal_hosts: tuple[str, ...]
    input_selectors: tuple[str, ...]
    answer_selectors: tuple[str, ...]
    login_markers: tuple[str, ...] = ()


COMMON_INPUTS = (
    "textarea:not([disabled])",
    "[contenteditable='true'][role='textbox']",
    "[contenteditable='true']",
    "input[type='text']:not([disabled])",
)

SITES = {
    "doubao": SiteConfig(
        "doubao", "豆包", "https://www.doubao.com/chat/", 9311,
        ("doubao.com", "byteimg.com", "bytedance.com"), COMMON_INPUTS,
        ("[data-message-id]", "[data-testid*='message']", "[class*='message'] [class*='markdown']", "main [class*='markdown']"),
        ("登录", "扫码登录", "验证码登录"),
    ),
    "yuanbao": SiteConfig(
        "yuanbao", "腾讯元宝", "https://yuanbao.tencent.com/chat/", 9312,
        ("yuanbao.tencent.com", "tencent.com", "qq.com"), COMMON_INPUTS,
        ("#chat-content [class*='agent']", "#chat-content [class*='markdown']", "#chat-content [class*='hyc-content']", "#chat-content [class*='message']"),
        ("登录后使用", "微信登录", "手机号登录"),
    ),
    "wenxin": SiteConfig(
        "wenxin", "文心一言", "https://wenxin.baidu.com/", 9313,
        ("baidu.com",), COMMON_INPUTS,
        ("main [class*='answer']", "main [class*='markdown']", "[class*='chat-message']"),
        ("登录", "扫码登录"),
    ),
    "deepseek": SiteConfig(
        "deepseek", "DeepSeek", "https://chat.deepseek.com/", 9314,
        ("deepseek.com",), COMMON_INPUTS,
        (".ds-markdown", "[class*='ds-markdown']", "main [class*='markdown']", "[class*='message'] [class*='markdown']"),
        ("登录", "Sign in", "Log in"),
    ),
    "quark": SiteConfig(
        "quark", "夸克", "https://ai.quark.cn/", 9315,
        ("quark.cn", "uc.cn", "alibaba.com"), COMMON_INPUTS,
        ("main [class*='answer']", "main [class*='markdown']", "[class*='message'] [class*='content']", "[class*='result'] [class*='content']"),
        ("登录", "扫码登录", "手机号登录"),
    ),
}


def site_config(model: str) -> SiteConfig:
    try:
        return SITES[str(model).strip().casefold()]
    except KeyError as exc:
        raise ValueError(f"不支持的网页模型：{model}") from exc
