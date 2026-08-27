from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import websocket


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def chrome_executable() -> Path:
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    )
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError("找不到 Google Chrome")


def ensure_chrome(port: int, profile: Path, home_url: str) -> None:
    if not port_open(port):
        subprocess.Popen(
            [str(chrome_executable()), f"--remote-debugging-port={port}",
             "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={profile}", "--new-window", home_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not port_open(port):
            time.sleep(0.25)
    if not port_open(port):
        raise RuntimeError(f"专用 Chrome 启动超时（端口 {port}）")


class CDPPage:
    def __init__(self, port: int):
        self.port = port
        self.ws: websocket.WebSocket | None = None
        self.target_id = ""
        self.sequence = 0
        self.connect()

    def connect(self, target_id: str = "") -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=10))
        pages = [item for item in tabs if item.get("type") == "page"]
        page = next(
            (item for item in pages if target_id and str(item.get("id") or "") == target_id),
            None,
        ) or next(
            (item for item in pages if str(item.get("url") or "").startswith(("http://", "https://"))),
            pages[0] if pages else None,
        )
        if not page:
            raise RuntimeError("Chrome 中没有可采集页面")
        self.target_id = str(page.get("id") or "")
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=3, origin="http://127.0.0.1"
        )

    def replace_tab(self, url: str, timeout: float = 20) -> dict[str, str]:
        """Create one replacement tab, bind to it, then close the exact old tab."""
        old_target = str(getattr(self, "target_id", "") or "")
        created = self.call("Target.createTarget", {"url": str(url or "about:blank")}, timeout=timeout)
        new_target = str(created.get("targetId") or "")
        if not new_target:
            raise RuntimeError("Chrome 未返回新标签页标识")
        deadline = time.monotonic() + max(3, timeout)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.connect(new_target)
                if self.target_id == new_target:
                    break
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)
        else:
            raise RuntimeError(f"无法连接新建的 Chrome 标签页：{last_error}") from last_error

        closed = not old_target or old_target == new_target
        if not closed:
            close_url = f"http://127.0.0.1:{self.port}/json/close/{old_target}"
            for _ in range(3):
                try:
                    with urllib.request.urlopen(close_url, timeout=5) as response:
                        response.read()
                    closed = True
                    break
                except Exception:
                    time.sleep(0.2)
            if not closed:
                try:
                    closed = bool(
                        self.call("Target.closeTarget", {"targetId": old_target}, timeout=5).get("success")
                    )
                except Exception:
                    closed = False
        if not closed:
            raise RuntimeError(f"新标签页已打开，但旧标签页 {old_target} 关闭失败")
        return {"old_target": old_target, "new_target": new_target, "url": str(url or "about:blank")}

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        if self.ws is None:
            self.connect()
        self.sequence += 1
        self.ws.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
        return self.sequence

    def recv(self, timeout: float = 3) -> dict[str, Any]:
        assert self.ws is not None
        self.ws.settimeout(timeout)
        return json.loads(self.ws.recv())

    def _call_once(self, method: str, params: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        request_id = self.send(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = self.recv(min(2, max(0.1, deadline - time.monotonic())))
            except Exception:
                continue
            if message.get("id") == request_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
        raise TimeoutError(f"Chrome 调试调用超时：{method}")

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
        try:
            return self._call_once(method, params, timeout)
        except (TimeoutError, OSError, websocket.WebSocketException):
            self.connect()
            return self._call_once(method, params, timeout)

    def evaluate(self, expression: str, timeout: float = 15) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout,
        )
        return (result.get("result") or {}).get("value")

    def click(self, x: int, y: int) -> None:
        for event in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", {
                "type": event, "x": x, "y": y, "button": "left", "clickCount": 1,
            })


def external_sources(value: Any, excluded_hosts: tuple[str, ...]) -> list[dict[str, str]]:
    """Extract structured URL/title pairs from arbitrary response JSON."""
    found: dict[str, dict[str, str]] = {}

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            candidates = []
            for key in ("url", "href", "link", "sourceUrl", "source_url", "pageUrl", "referUrl", "action"):
                if isinstance(item.get(key), str):
                    candidates.append(item[key].strip())
            for url in candidates:
                try:
                    parsed = urlparse(url)
                except ValueError:
                    continue
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue
                host = parsed.netloc.casefold().removeprefix("www.")
                if any(host == excluded or host.endswith("." + excluded) for excluded in excluded_hosts):
                    continue
                title = next((str(item.get(key) or "").strip() for key in
                              ("text", "title", "name", "source", "abstract")
                              if str(item.get(key) or "").strip()), host)
                old = found.get(url)
                if old is None or len(title) > len(old["title"]):
                    found[url] = {"url": url, "title": title}
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            # Some sites wrap structured answer/citation payloads in JSON strings.
            value = item.strip()
            if value.startswith(("{", "[")) and len(value) <= 5_000_000:
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return
                walk(decoded)

    walk(value)
    return list(found.values())


def capture_json_responses(page: CDPPage, action, seconds: float = 18) -> list[tuple[str, Any]]:
    page.call("Network.enable", {
        "maxTotalBufferSize": 100_000_000,
        "maxResourceBufferSize": 10_000_000,
    })
    action()
    responses: dict[str, str] = {}
    finished: set[str] = set()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            message = page.recv(1)
        except Exception:
            continue
        if message.get("method") == "Network.responseReceived":
            params = message.get("params") or {}
            response = params.get("response") or {}
            mime = str(response.get("mimeType") or "")
            if params.get("type") in {"XHR", "Fetch"} or "json" in mime:
                responses[str(params.get("requestId"))] = str(response.get("url") or "")
        elif message.get("method") == "Network.loadingFinished":
            finished.add(str((message.get("params") or {}).get("requestId")))
    output: list[tuple[str, Any]] = []
    for request_id, url in responses.items():
        if request_id not in finished:
            continue
        try:
            body = page.call("Network.getResponseBody", {"requestId": request_id}, timeout=5).get("body") or ""
            if body.lstrip().startswith(("{", "[")):
                output.append((url, json.loads(body)))
        except Exception:
            continue
    return output
