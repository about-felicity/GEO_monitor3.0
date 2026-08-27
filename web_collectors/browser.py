from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from monitor_core.cdp_chat import CDPPage, chrome_executable
from monitor_core.plugins import ROOT

from .config import SiteConfig


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def profile_path(model: str) -> Path:
    return ROOT / "runtime" / "web_profiles" / model


def launch_browser(site: SiteConfig, *, headless: bool) -> None:
    if port_open(site.port):
        return
    profile = profile_path(site.id)
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(chrome_executable()),
        f"--remote-debugging-port={site.port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-background-networking",
        "--window-size=1440,1100",
        f"--user-data-dir={profile}",
    ]
    if headless:
        command.extend(("--headless=new", "--disable-gpu"))
    command.extend(("--new-window", site.home_url))
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not port_open(site.port):
        time.sleep(0.25)
    if not port_open(site.port):
        raise RuntimeError(f"{site.name} Chrome 启动超时（端口 {site.port}）")


class SitePage(CDPPage):
    """CDP page that binds to the tab belonging to one configured site."""

    def __init__(self, site: SiteConfig):
        self.site = site
        super().__init__(site.port)

    def connect(self, target_id: str = "") -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=10) as response:
            tabs = json.load(response)
        pages = [item for item in tabs if item.get("type") == "page"]
        page = next((item for item in pages if target_id and item.get("id") == target_id), None)
        if page is None:
            page = next(
                (item for item in pages if any(host in str(item.get("url") or "") for host in self.site.internal_hosts)),
                pages[0] if pages else None,
            )
        if page is None:
            raise RuntimeError(f"{self.site.name} Chrome 中没有可用网页")
        import websocket
        self.target_id = str(page.get("id") or "")
        self.ws = websocket.create_connection(
            page["webSocketDebuggerUrl"], timeout=3, origin="http://127.0.0.1"
        )


def close_browser(site: SiteConfig) -> None:
    if not port_open(site.port):
        return
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{site.port}/json/version", timeout=5) as response:
            version: dict[str, Any] = json.load(response)
        url = str(version.get("webSocketDebuggerUrl") or "")
        if not url:
            return
        import websocket
        ws = websocket.create_connection(url, timeout=5, origin="http://127.0.0.1")
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        ws.close()
    except Exception:
        return
