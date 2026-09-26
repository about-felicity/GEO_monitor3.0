"""Run the legacy Doubao UI pipeline with production-grade partial-source recovery.

Doubao's visible reference total can exceed the number of distinct external
links exposed by its DOM/backend payload.  The legacy grabber correctly retries
hydration first, but historically discarded an otherwise complete answer when
the final distinct-link count remained below that display total.  This wrapper
keeps the original retry path and only accepts its final payload when the body
is substantial and the captured source coverage is high enough.  The record is
still marked source-incomplete so auditing remains mathematically honest.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = ROOT.parent / "DouBao_Monitor_v2.0"
INCOMPLETE_PREFIX = "抓取未完整："


def recover_partial_payload(
    error: BaseException,
    latest_href: str = "",
    *,
    minimum_coverage: float | None = None,
    minimum_sources: int | None = None,
    minimum_body_chars: int | None = None,
) -> dict[str, Any] | None:
    """Return an auditable partial payload after the normal retries are exhausted."""
    message = str(error or "")
    marker = message.find(INCOMPLETE_PREFIX)
    if marker < 0:
        return None
    try:
        payload = json.loads(message[marker + len(INCOMPLETE_PREFIX):].strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    sources = [item for item in payload.get("items") or [] if isinstance(item, dict)]
    count = max(int(payload.get("count") or 0), len(sources))
    expected = max(count, int(payload.get("expectedCount") or 0))
    body = str(payload.get("answerText") or payload.get("answer_text") or "").strip()
    coverage = count / expected if expected else 0.0
    minimum_coverage = float(
        minimum_coverage
        if minimum_coverage is not None
        else os.environ.get("GEO_DOUBAO_PARTIAL_MIN_COVERAGE", "0.65")
    )
    minimum_sources = int(
        minimum_sources
        if minimum_sources is not None
        else os.environ.get("GEO_DOUBAO_PARTIAL_MIN_SOURCES", "3")
    )
    minimum_body_chars = int(
        minimum_body_chars
        if minimum_body_chars is not None
        else os.environ.get("GEO_DOUBAO_PARTIAL_MIN_BODY_CHARS", "200")
    )
    payload_url = str(payload.get("url") or "").rstrip("/")
    expected_url = str(latest_href or "").rstrip("/")
    if expected_url and payload_url and payload_url != expected_url:
        return None
    if expected <= 0 or count < minimum_sources or len(body) < minimum_body_chars:
        return None
    if coverage < max(0.0, min(1.0, minimum_coverage)):
        return None

    recovered = dict(payload)
    recovered.update({
        "count": count,
        "expectedCount": expected,
        "complete": False,
        "source_capture_complete": False,
        "partialAccepted": True,
        "missingCount": max(0, expected - count),
        "sourceCoverage": round(coverage, 4),
        "partialAcceptanceReason": "stable_after_full_retry_budget",
    })
    return recovered


def wrap_grabber(module: Any) -> Any:
    original: Callable[..., dict[str, Any]] = module.grab_with_retry

    def resilient_grab_with_retry(ws_url: str, latest_href: str = "") -> dict[str, Any]:
        try:
            return original(ws_url, latest_href)
        except RuntimeError as exc:
            recovered = recover_partial_payload(exc, latest_href)
            if recovered is None:
                raise
            print(
                "accept stable partial Doubao sources:",
                json.dumps({
                    "count": recovered["count"],
                    "expectedCount": recovered["expectedCount"],
                    "coverage": recovered["sourceCoverage"],
                    "url": recovered.get("url"),
                }, ensure_ascii=False),
                flush=True,
            )
            return recovered

    module.grab_with_retry = resilient_grab_with_retry
    return module


def patch_appium_status_probe(pipeline: Any) -> None:
    """Treat Appium's successful status message as informational, not an error.

    The legacy generic JSON helper rejects any ``value.message`` field. Appium
    2 returns a message even for a healthy ``/status`` response, so the old
    probe reports a live server as down and then tries to bind a second server
    to port 4723. Keep the stricter generic request handling for real commands,
    and replace only the health probe.
    """
    appium_client = pipeline.mumu.AppiumClient

    def production_server_ready(self: Any) -> bool:
        try:
            response = self.http.get(self._url("status"), timeout=5)
            if response.status_code >= 400:
                return False
            data = response.json()
            value = data.get("value") if isinstance(data, dict) else None
            return isinstance(value, dict) and value.get("ready") is not False
        except Exception:
            return False

    appium_client.server_ready = production_server_ready


def patch_new_chat_navigation(pipeline: Any) -> None:
    """Prefer the current app's direct new-chat button, with legacy fallback."""
    mumu = pipeline.mumu
    automation = mumu.DoubaoAutomation
    original = automation.create_new_chat
    current_ids = (
        "com.larus.nova:id/larus_chat_top_left_create_new_cvs",
        "com.larus.nova:id/right_img",
    )

    def production_create_new_chat(self: Any) -> None:
        source, root = self.source_root()
        if mumu.page_name(root) == "chat":
            for resource_id in current_ids:
                if not mumu.has_id(root, resource_id):
                    continue
                try:
                    self.logger.info("使用聊天页的新建对话入口：%s", resource_id)
                    self.appium.click_id(resource_id, timeout=5)
                    # The direct control replaces the current conversation in
                    # place, so there may be no intermediate list/menu page.
                    time.sleep(1)
                    self.wait_until(
                        lambda item: (
                            mumu.page_name(item) == "chat"
                            and mumu.has_id(item, mumu.INPUT_ID)
                        ),
                        timeout=12,
                        description="新对话聊天页",
                    )
                    return
                except mumu.AutomationError as exc:
                    self.logger.warning("新版新建对话入口失败：%s", exc)
                    break
        return original(self)

    automation.create_new_chat = production_create_new_chat


def main() -> int:
    if str(LEGACY_ROOT) not in sys.path:
        sys.path.insert(0, str(LEGACY_ROOT))
    from doubao_mumu_controller import doubao_mumu_web_pipeline as pipeline

    original_import_grabber = pipeline.import_grabber
    original_launch_account_browser = pipeline.launch_account_browser

    def import_resilient_grabber() -> Any:
        return wrap_grabber(original_import_grabber())

    def launch_production_extension_browser(*args: Any, **kwargs: Any) -> Any:
        # Keep the legacy controller itself read-only, but make new Chrome
        # sessions load the maintained production extension from this project.
        legacy_monitor_dir = pipeline.MONITOR_DIR
        pipeline.MONITOR_DIR = ROOT
        try:
            return original_launch_account_browser(*args, **kwargs)
        finally:
            pipeline.MONITOR_DIR = legacy_monitor_dir

    pipeline.import_grabber = import_resilient_grabber
    pipeline.launch_account_browser = launch_production_extension_browser
    patch_appium_status_probe(pipeline)
    patch_new_chat_navigation(pipeline)
    return int(pipeline.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
