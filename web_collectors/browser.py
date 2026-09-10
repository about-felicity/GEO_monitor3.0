from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from monitor_core.cdp_chat import CDPPage, chrome_executable
from monitor_core.plugins import ROOT

from .config import SiteConfig


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _session_root() -> Path:
    path = ROOT / "runtime" / "web_sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cookie_env_name(model: str) -> str:
    return f"MONITOR_{str(model).strip().upper()}_COOKIES_JSON"


def storage_env_name(model: str) -> str:
    return f"MONITOR_{str(model).strip().upper()}_STORAGE_JSON"


def session_cookies(site: SiteConfig) -> list[dict[str, Any]]:
    """Load browser cookies from process memory without writing them to disk."""
    raw = str(os.environ.get(cookie_env_name(site.id)) or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{cookie_env_name(site.id)} 不是有效的 JSON") from exc
    if isinstance(value, dict):
        value = value.get("cookies")
    if not isinstance(value, list):
        raise RuntimeError(f"{cookie_env_name(site.id)} 必须是 Cookie 数组或包含 cookies 数组的对象")
    default_host = urlparse(site.home_url).hostname or ""
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not str(item.get("name") or ""):
            continue
        cookie = {
            key: item[key]
            for key in ("name", "value", "domain", "path", "secure", "httpOnly", "expires", "sameSite")
            if key in item
        }
        if "expires" not in cookie and item.get("expirationDate") is not None:
            cookie["expires"] = item["expirationDate"]
        cookie["name"] = str(cookie["name"])
        cookie["value"] = str(cookie.get("value") or "")
        if not cookie.get("domain"):
            cookie["domain"] = default_host
        cookie.setdefault("path", "/")
        same_site = str(cookie.get("sameSite") or "").strip().casefold()
        if same_site:
            cookie["sameSite"] = {
                "strict": "Strict", "lax": "Lax", "none": "None",
                "no_restriction": "None", "unspecified": "Lax",
            }.get(same_site, "Lax")
        output.append(cookie)
    return output


@dataclass
class BrowserHandle:
    site: SiteConfig
    process: subprocess.Popen[Any] | None
    profile: Path | None
    owned: bool

    def close(self) -> None:
        if not self.owned:
            return
        self.owned = False
        close_browser(self.site)
        if self.process is not None:
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.profile is not None:
            shutil.rmtree(self.profile, ignore_errors=True)
        root = ROOT / "runtime" / "web_sessions"
        try:
            root.rmdir()
        except OSError:
            pass


def launch_browser(site: SiteConfig, *, headless: bool) -> BrowserHandle:
    if port_open(site.port):
        raise RuntimeError(f"{site.name} 调试端口 {site.port} 已被占用；请先停止旧采集进程")
    persistent = site.id in {"deepseek", "kimi"}
    if persistent:
        profile = ROOT / "runtime" / "web_profiles" / site.id
        profile.mkdir(parents=True, exist_ok=True)
    else:
        session_root = _session_root()
        for stale in session_root.glob(f"{site.id}-*"):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
        profile = Path(tempfile.mkdtemp(prefix=f"{site.id}-", dir=session_root))
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
    if not persistent:
        command.append("--incognito")
    if headless:
        command.extend(("--headless=new", "--disable-gpu"))
    command.extend(("--new-window", site.home_url))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except Exception:
        shutil.rmtree(profile, ignore_errors=True)
        raise
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not port_open(site.port):
        time.sleep(0.25)
    if not port_open(site.port):
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
        raise RuntimeError(f"{site.name} Chrome 启动超时（端口 {site.port}）")
    return BrowserHandle(
        site=site,
        process=process,
        profile=None if persistent else profile,
        owned=True,
    )


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
