from __future__ import annotations

import argparse

from .collector import create_collector
from .config import SITES


def main() -> int:
    parser = argparse.ArgumentParser(description="用临时 Cookie 验证五模型隐身网页会话")
    parser.add_argument("--model", choices=("all", *SITES), default="all")
    args = parser.parse_args()
    selected = SITES.values() if args.model == "all" else (SITES[args.model],)
    failed = False
    for site in selected:
        collector = create_collector(site.id, headless=False)
        try:
            result = collector.check_ready()
            print(f"{site.name}: {result.get('message')}")
            failed = failed or not bool(result.get("ok"))
        finally:
            collector.close()
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
