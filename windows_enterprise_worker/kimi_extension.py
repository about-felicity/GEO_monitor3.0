"""Control the dedicated Kimi Chrome extension through the local CDP port."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from monitor_core.cdp_chat import chrome_executable


ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = ROOT / "kimi_chrome_extension"
PROFILE_ROOT = ROOT / "runtime" / "web_profiles" / "kimi-extension"
KIMI_URL = "https://www.kimi.com/"


def _port() -> int:
    return int(os.environ.get("GEO_KIMI_EXTENSION_PORT", "9227"))


def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", _port()), timeout=0.5):
            return True
    except OSError:
        return False


class _CdpSocket:
    def __init__(self, url: str):
        import websocket

        self.socket = websocket.create_connection(
            url, timeout=15, origin=f"http://127.0.0.1:{_port()}"
        )
        self.next_id = 0
        self.events: list[dict[str, Any]] = []

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        message_id = self.next_id
        self.socket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.5, deadline - time.monotonic()))
            value = json.loads(self.socket.recv())
            if value.get("id") != message_id:
                self.events.append(value)
                continue
            if value.get("error"):
                raise RuntimeError(json.dumps(value["error"], ensure_ascii=False))
            result = value.get("result")
            return result if isinstance(result, dict) else {}
        raise TimeoutError(f"Chrome CDP 命令超时：{method}")

    def evaluate(self, expression: str, context_id: int | None = None) -> Any:
        params: dict[str, Any] = {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        }
        if context_id is not None:
            params["contextId"] = context_id
        result = self.command("Runtime.evaluate", params)
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        self.socket.close()


class KimiExtensionClient:
    def __init__(self) -> None:
        self.process: subprocess.Popen[Any] | None = None

    @staticmethod
    def _json(path: str) -> Any:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{_port()}{path}", timeout=5
        ) as response:
            return json.load(response)

    def _launch(self) -> None:
        if _port_open():
            return
        PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
        command = [
            str(chrome_executable()),
            f"--remote-debugging-port={_port()}",
            f"--user-data-dir={PROFILE_ROOT}",
            "--enable-unsafe-extension-debugging",
            "--remote-allow-origins=*",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=ChromeWhatsNewUI",
            KIMI_URL,
        ]
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt" else 0
            ),
            start_new_session=os.name != "nt",
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not _port_open():
            time.sleep(0.25)
        if not _port_open():
            raise RuntimeError("Kimi 插件专用 Chrome 启动超时")

    def _target(self) -> dict[str, Any]:
        targets = self._json("/json/list")
        pages = [
            item for item in targets
            if item.get("type") == "page" and "kimi.com" in str(item.get("url") or "")
        ]
        if not pages:
            raise RuntimeError("Kimi 插件 Chrome 中没有 Kimi 页面")
        return pages[-1]

    def _load_extension(self) -> None:
        if not EXTENSION_ROOT.joinpath("manifest.json").is_file():
            raise RuntimeError("Kimi Chrome 插件文件不完整")
        version = self._json("/json/version")
        browser = _CdpSocket(str(version.get("webSocketDebuggerUrl") or ""))
        try:
            browser.command("Extensions.loadUnpacked", {"path": str(EXTENSION_ROOT.resolve())})
        finally:
            browser.close()
        page = _CdpSocket(str(self._target().get("webSocketDebuggerUrl") or ""))
        try:
            page.command("Page.reload", {"ignoreCache": True})
        finally:
            page.close()
        time.sleep(2)

    def _raw_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        page = _CdpSocket(str(self._target().get("webSocketDebuggerUrl") or ""))
        try:
            page.command("Runtime.enable")
            contexts = [
                event.get("params", {}).get("context", {})
                for event in page.events
                if event.get("method") == "Runtime.executionContextCreated"
            ]
            candidates: list[tuple[tuple[int, ...], int]] = []
            for context in contexts:
                context_id = int(context.get("id") or 0)
                if not context_id:
                    continue
                try:
                    manifest = page.evaluate(
                        "(()=>{const m=globalThis.chrome?.runtime?.getManifest?.();"
                        "return m?{name:m.name||'',version:m.version||'0'}:null})()", context_id
                    )
                except RuntimeError:
                    continue
                if not isinstance(manifest, dict) or "Kimi对话监控" not in str(manifest.get("name") or ""):
                    continue
                version = tuple(int(part) if part.isdigit() else 0 for part in str(manifest.get("version") or "0").split("."))
                candidates.append((version, context_id))
            for _, context_id in sorted(candidates, reverse=True):
                encoded = json.dumps(payload, ensure_ascii=False)
                value = page.evaluate(
                    "(async()=>{try{return await chrome.runtime.sendMessage(" + encoded
                    + ");}catch(error){return {ok:false,error:String(error?.message||error)};}})()",
                    context_id,
                )
                if isinstance(value, dict):
                    return value
            if candidates:
                return {"ok": False, "error": "Kimi 插件返回格式无效"}
            raise RuntimeError("Kimi 页面尚未加载诊断插件")
        finally:
            page.close()

    def message(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._launch()
        try:
            return self._raw_message(payload)
        except Exception:
            self._load_extension()
            return self._raw_message(payload)

    def probe(self) -> dict[str, Any]:
        try:
            value: dict[str, Any] = {}
            for attempt in range(3):
                value = self.message({"type": "PROBE_READY"})
                if value.get("ok") and value.get("ready"):
                    break
                if attempt < 2:
                    time.sleep(2)
            ready = bool(value.get("ok") and value.get("ready"))
            return {
                "ready": ready,
                "message": str(value.get("message") or (
                    "Kimi Chrome 插件已登录并就绪" if ready
                    else "请在 Kimi 插件专用 Chrome 中完成登录"
                ))[:200],
            }
        except Exception as exc:
            return {"ready": False, "message": f"Kimi 插件未就绪：{type(exc).__name__}: {exc}"[:200]}

    def run_job(
        self, *, job_id: str, questions: list[str], rounds: int,
        progress: Callable[[str], None], timeout: int,
    ) -> list[dict[str, Any]]:
        settings = {
            "questions": questions, "rounds": rounds, "questionMode": "interleaved",
            "intervalMinSeconds": int(os.environ.get("GEO_KIMI_EXTENSION_MIN_GAP", "3")),
            "intervalMaxSeconds": int(os.environ.get("GEO_KIMI_EXTENSION_MAX_GAP", "5")),
            "timeoutSeconds": min(420, max(60, timeout)), "stableSeconds": 8,
            "minAnswerLength": 30, "maxRetries": 2, "retryDelaySeconds": 15,
            # The diagnosis worker reads the extension's durable local storage
            # directly, keeping it isolated from the standalone monitor's 8767
            # result history when both products are open on the same machine.
            "receiverUrl": "http://127.0.0.1:8797", "targetUrl": KIMI_URL,
        }
        started = self.message({"type": "START_JOB", "jobId": job_id, "settings": settings})
        if not started.get("ok"):
            raise RuntimeError(str(started.get("error") or "Kimi 插件任务启动失败"))
        deadline = time.monotonic() + timeout * max(1, len(questions) * rounds) + 60
        try:
            while time.monotonic() < deadline:
                context = self.message({"type": "GET_CONTEXT"})
                job = context.get("job") if isinstance(context.get("job"), dict) else {}
                if str(job.get("id") or "") != job_id:
                    raise RuntimeError("Kimi 插件当前任务被其他任务替换")
                cursor = int(job.get("cursor") or 0)
                state = str(job.get("state") or "")
                progress(f"Kimi 插件已采集 {cursor}/{len(questions) * rounds} 轮")
                if state == "completed":
                    result = self.message({"type": "GET_ALL_RESULTS"})
                    rows = [
                        item for item in result.get("results") or []
                        if isinstance(item, dict) and str(item.get("run_id") or "") == job_id
                    ]
                    rows.sort(key=lambda item: int(item.get("round") or 0))
                    if len(rows) != len(questions) * rounds:
                        raise RuntimeError(f"Kimi 插件仅返回 {len(rows)}/{len(questions) * rounds} 轮")
                    failed = next((item for item in rows if item.get("status") == "failed"), None)
                    if failed:
                        raise RuntimeError(str(failed.get("skip_reason") or "Kimi 插件采集失败"))
                    return rows
                if state in {"error", "stopped"}:
                    raise RuntimeError(str(job.get("lastError") or f"Kimi 插件任务 {state}"))
                time.sleep(2)
            raise TimeoutError("Kimi 插件采集任务超时")
        except BaseException:
            try:
                self.message({"type": "STOP_JOB"})
            except Exception:
                pass
            raise
