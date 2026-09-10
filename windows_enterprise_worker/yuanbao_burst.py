"""Fast-submit Yuanbao rounds on the emulator, then harvest bound web chats."""

from __future__ import annotations

import argparse
import json
import sys
import time
from types import MethodType
from pathlib import Path


def _install_complete_body_extractor(collector) -> None:
    legacy_extract = collector.extract_body

    def extract_complete_body(self, message):
        try:
            pieces = self.driver.execute_script("""
                const root = arguments[0];
                const selectors = [
                    '.hyc-common-markdown', '[class*="markdown"]',
                    '[class*="rich-text"]', '[class*="message-content"]',
                    '[class*="answer-body"]'
                ];
                const nodes = Array.from(root.querySelectorAll(selectors.join(',')));
                const topLevel = nodes.filter((node) => !nodes.some(
                    (other) => other !== node && other.contains(node)
                ));
                return topLevel.map((node) => (node.innerText || '').trim()).filter(Boolean);
            """, message) or []
        except Exception:
            pieces = []
        unique = []
        for piece in pieces:
            text = str(piece or "").strip()
            if text and text not in unique:
                unique.append(text)
        combined = "\n".join(unique).strip()
        fallback = str(legacy_extract(message) or "").strip()
        return combined if len(combined) >= len(fallback) else fallback

    collector.extract_body = MethodType(extract_complete_body, collector)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--questions-json", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--chrome-port", type=int, default=9222)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--settle-seconds", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    questions = json.loads(args.questions_json)
    if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise ValueError("元宝批量问题必须为 1 到 3 项")
    questions = [str(item or "").strip() for item in questions]
    if any(not item for item in questions):
        raise ValueError("元宝批量问题不能为空")
    sys.path.insert(0, str(args.legacy_root.resolve()))
    from yuanbao_monitor.collector import YuanbaoSourceCollector
    from yuanbao_monitor.controller import YuanbaoController

    controller = YuanbaoController(serial=args.serial)
    collector = YuanbaoSourceCollector(
        debug_port=args.chrome_port, user_data_dir=str(args.profile), debug=False,
    )
    _install_complete_body_extractor(collector)
    sessions: list[dict] = []
    # The production scheduler guarantees a single active diagnosis. This helper
    # must not depend on a legacy device-lock module outside the deployed package.
    for index, question in enumerate(questions, 1):
        print(f"元宝第 {index} 轮正在发送问题", flush=True)
        previous = collector.latest_conversation_reference(refresh=True)
        sent = controller.submit_only(question)
        print(f"元宝第 {index} 轮问题已发送，等待回答完成", flush=True)
        completion_xml = controller._wait_for_reply(
            question=question, max_wait=args.timeout,
        )
        if controller.PLUS_ID not in completion_xml:
            raise RuntimeError(f"元宝第 {index} 轮未检测到停止方块恢复为＋")
        if len(controller.extract_visible_reply(completion_xml, question)) < 30:
            raise RuntimeError(f"元宝第 {index} 轮完成信号出现，但可见回答仍不完整")
        print(f"元宝第 {index} 轮回答已生成，正在同步网页正文", flush=True)
        reference = collector.click_new_conversation(previous, question, timeout=90)
        url = str(collector.driver.current_url or "")
        if not reference or reference == previous or reference not in url:
            raise RuntimeError(f"元宝第 {index} 轮未绑定到唯一网页会话")
        if any(item["reference"] == reference or item["url"] == url for item in sessions):
            raise RuntimeError(f"元宝第 {index} 轮会话身份重复，拒绝继续")
        sessions.append({
            "round": index, "question": question, "reference": reference,
            "url": url, "submitted_at": sent["submitted_at"],
        })
        print(f"元宝第 {index} 轮网页会话已同步", flush=True)

    time.sleep(max(1, min(3, args.settle_seconds)))
    results = []
    for session in sessions:
        print(f"元宝第 {session['round']} 轮正在回收完整正文与信源", flush=True)
        row = collector.collect_bound(
            session["question"], session["reference"], session["url"],
            wait_reply_timeout=args.timeout,
            extra={"submitted_at": session["submitted_at"],
                   "capture_strategy": "burst_submit_then_harvest"},
        )
        if row.get("error") and not str(row["error"]).startswith("incomplete_sources:"):
            raise RuntimeError(f"元宝第 {session['round']} 轮回收失败：{row['error']}")
        results.append(row)
        print(f"元宝第 {session['round']} 轮正文与信源已回收", flush=True)
    print("GEO_YUANBAO_BURST_JSON=" + json.dumps({"results": results}, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
