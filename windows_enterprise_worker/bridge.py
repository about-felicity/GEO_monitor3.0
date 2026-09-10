"""Small subprocess bridge for collectors whose dependency trees conflict."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=("wenxin", "quark", "deepseek", "kimi"))
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--question", default="")
    parser.add_argument("--questions-json", default="")
    parser.add_argument("--ready", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.legacy_root:
        sys.path.insert(0, str(args.legacy_root.resolve()))
    if args.model == "wenxin":
        from wenxin_monitor.controller import WenxinWebCollector

        collector = WenxinWebCollector()
        try:
            if args.ready:
                value = collector.account_identity()
                output = {"ready": bool(value.get("ready", True)), "message": "文心网页直采可用", "identity": value}
            elif args.questions_json:
                questions = json.loads(args.questions_json)
                if not isinstance(questions, list):
                    raise ValueError("--questions-json 必须是 JSON 数组")
                results = collector.collect_batch(
                    [str(item) for item in questions], timeout=150,
                    settle_seconds=15,
                )
                for question, result in zip(questions, results):
                    if isinstance(result, dict):
                        # The collector has already required the live search
                        # box query to match before returning this result.
                        result["captured_question"] = str(question)
                output = {"results": results}
            else:
                output = collector.collect_search(args.question, timeout=150)
                if isinstance(output, dict):
                    output["captured_question"] = args.question
        finally:
            collector.close()
    else:
        if args.ready and args.model in {"deepseek", "kimi"}:
            from web_collectors.browser import session_cookies
            from web_collectors.config import site_config

            if not session_cookies(site_config(args.model)):
                output = {
                    "ready": False,
                    "status": "login_required",
                    "message": f"{site_config(args.model).name} 网页账号需要登录",
                }
                print("GEO_BRIDGE_JSON=" + json.dumps(output, ensure_ascii=False, separators=(",", ":")))
                return 0
        from web_collectors.collector import create_collector

        collector = create_collector(args.model, headless=args.headless)
        try:
            if args.ready:
                output = collector.check_ready()
            elif args.questions_json:
                questions = json.loads(args.questions_json)
                if not isinstance(questions, list):
                    raise ValueError("--questions-json 必须是 JSON 数组")
                output = {"results": collector.collect_batch(
                    [str(item) for item in questions], timeout=240,
                    settle_seconds=15,
                )}
            else:
                output = collector.collect(args.question, timeout=240)
        finally:
            collector.close()
    print("GEO_BRIDGE_JSON=" + json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
