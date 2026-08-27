from __future__ import annotations

import argparse
import time

from .browser import launch_browser
from .config import SITES


def main() -> int:
    parser = argparse.ArgumentParser(description="打开五模型独立网页登录窗口")
    parser.add_argument("--model", choices=("all", *SITES), default="all")
    args = parser.parse_args()
    selected = SITES.values() if args.model == "all" else (SITES[args.model],)
    for site in selected:
        if site.id == "yuanbao":
            from .scrapling_yuanbao import YuanbaoScraplingCollector
            print("正在打开腾讯元宝 Scrapling 可见浏览器，请在 10 分钟内完成登录……")
            collector = YuanbaoScraplingCollector(headless=False)
            try:
                result = collector.wait_for_login(600)
                print("腾讯元宝登录状态已保存。" if result.get("ok") else "腾讯元宝登录等待超时。")
            finally:
                collector.close()
            continue
        launch_browser(site, headless=False)
        print(f"已打开 {site.name}：请完成登录后保留窗口或直接关闭；登录状态会保存。")
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
