from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from monitor_core.plugins import ROOT

from .browser import session_cookies
from .config import site_config


def _visible_input(page, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = page.locator(selector)
        for index in range(locator.count()):
            item = locator.nth(index)
            try:
                if item.is_visible() and item.is_enabled():
                    return item
            except Exception:
                continue
    return None


def _logged_out(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        return True
    return any(marker in text for marker in (
        "请登录后输入内容",
        "请使用微信扫描二维码登录",
        "未登录",
        "Not logged in",
        "Log In with WeChat",
        "Please scan the QR code",
    ))


class YuanbaoScraplingCollector:
    """Persistent Yuanbao collector backed by Scrapling's stealth browser."""

    def __init__(self, *, headless: bool = True, profile: Path | None = None):
        try:
            from scrapling.fetchers import StealthySession
        except ImportError as exc:
            raise RuntimeError("元宝隐身采集需要安装 scrapling[fetchers]") from exc
        self.site = site_config("yuanbao")
        session_root = ROOT / "runtime" / "web_sessions"
        session_root.mkdir(parents=True, exist_ok=True)
        self._temporary_profile = profile is None
        self.profile = profile or Path(tempfile.mkdtemp(prefix="yuanbao-", dir=session_root))
        self.profile.mkdir(parents=True, exist_ok=True)
        self.cookies = session_cookies(self.site)
        self._closed = False
        self.session = StealthySession(
            headless=headless,
            user_data_dir=str(self.profile),
            real_chrome=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            google_search=False,
            disable_resources=False,
            hide_canvas=True,
            block_webrtc=True,
            load_dom=True,
            network_idle=False,
            timeout=30000,
            retries=1,
            additional_args={"viewport": {"width": 1440, "height": 1100}},
        )
        self.session.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.session.close()
        finally:
            if self._temporary_profile:
                shutil.rmtree(self.profile, ignore_errors=True)
            try:
                (ROOT / "runtime" / "web_sessions").rmdir()
            except OSError:
                pass

    def _apply_cookies(self, page) -> None:
        if not self.cookies:
            return
        page.context.add_cookies(self.cookies)
        self.cookies = []

    def _snapshot(self, page) -> dict[str, Any]:
        return page.evaluate(
            r"""
([selectors,internal])=>{
  const visible=(el)=>{const r=el.getBoundingClientRect();const s=getComputedStyle(el);return r.width>8&&r.height>8&&s.display!=='none'&&s.visibility!=='hidden';};
  let nodes=[];
  for(const selector of selectors)nodes.push(...document.querySelectorAll(selector));
  nodes=[...new Set(nodes)].filter(el=>visible(el)&&String(el.innerText||'').trim().length>=20);
  if(!nodes.length)nodes=[...document.querySelectorAll('main article, main [class*=answer], main [class*=markdown], main [class*=message]')].filter(el=>visible(el)&&String(el.innerText||'').trim().length>=20);
  const node=nodes[nodes.length-1]||null;
  const body=String(node?.innerText||'').replace(/\n{3,}/g,'\n\n').trim();
  const anchors=[...(node||document.querySelector('main')||document).querySelectorAll('a[href]')];
  const seen=new Set(),sources=[];
  for(const a of anchors){
    try{
      const u=new URL(a.href,location.href);const host=u.hostname.replace(/^www\./,'').toLowerCase();
      if(!/^https?:$/.test(u.protocol)||internal.some(x=>host===x||host.endsWith('.'+x)))continue;
      const title=String(a.innerText||a.title||a.getAttribute('aria-label')||host).replace(/\s+/g,' ').trim();
      if(!title||seen.has(u.href))continue;seen.add(u.href);sources.push({title,url:u.href,href:u.href});
    }catch{}
  }
  const text=String(document.body?.innerText||'');
  const busy=/正在生成|思考中|搜索中|停止生成|Stop generating/i.test(text);
  return {url:location.href,title:document.title,body,busy,sources,pageText:text.slice(-12000)};
}
""",
            [list(self.site.answer_selectors), list(self.site.internal_hosts)],
        ) or {}

    def check_ready(self) -> dict[str, Any]:
        if not self.cookies:
            from .browser import cookie_env_name
            return {
                "ok": False,
                "status": "login_required",
                "message": f"隐身会话需要临时设置 {cookie_env_name(self.site.id)}",
                "web": {},
                "location": "local",
            }
        state: dict[str, Any] = {}
        error: list[Exception] = []

        def action(page) -> None:
            try:
                self._apply_cookies(page)
                page.goto(self.site.home_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                state.update({
                    "input": bool(_visible_input(page, self.site.input_selectors)),
                    "logged_out": _logged_out(page),
                    "url": page.url,
                })
            except Exception as exc:
                error.append(exc)

        self.session.fetch(self.site.home_url, page_action=action, wait=0, timeout=30000)
        if error:
            raise error[0]
        if state.get("input") and not state.get("logged_out"):
            return {"ok": True, "status": "ready", "message": "腾讯元宝 Scrapling 网页会话可用", "web": {"masked": "Scrapling 持久会话"}, "location": "local"}
        return {"ok": False, "status": "login_required", "message": "腾讯元宝尚未登录，请运行元宝 Scrapling 可见登录命令", "web": {}, "location": "local"}

    def wait_for_login(self, timeout: int = 600) -> dict[str, Any]:
        state: dict[str, Any] = {}

        def action(page) -> None:
            self._apply_cookies(page)
            page.goto(self.site.home_url, wait_until="domcontentloaded")
            deadline = time.monotonic() + max(30, timeout)
            while time.monotonic() < deadline:
                if _visible_input(page, self.site.input_selectors) and not _logged_out(page):
                    state.update({"ok": True, "url": page.url})
                    return
                page.wait_for_timeout(1000)
            state.update({"ok": False, "url": page.url})

        self.session.fetch(self.site.home_url, page_action=action, wait=0, timeout=max(30000, timeout * 1000))
        return state

    def collect(self, question: str, *, timeout: int = 240, stable_seconds: int = 6) -> dict[str, Any]:
        output: dict[str, Any] = {}
        error: list[Exception] = []

        def action(page) -> None:
            try:
                self._apply_cookies(page)
                page.goto(self.site.home_url, wait_until="domcontentloaded")
                input_box = _visible_input(page, self.site.input_selectors)
                if input_box is None or _logged_out(page):
                    raise RuntimeError("腾讯元宝 Scrapling 会话未登录或输入框选择器已失效")
                baseline = self._snapshot(page)
                baseline_body = str(baseline.get("body") or "")
                input_box.click()
                try:
                    input_box.fill(question)
                except Exception:
                    input_box.press("Control+A")
                    input_box.press("Backspace")
                    input_box.press_sequentially(question, delay=15)
                input_box.press("Enter")
                deadline = time.monotonic() + max(30, timeout)
                stable_since: float | None = None
                previous = ""
                last: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    page.wait_for_timeout(1200)
                    last = self._snapshot(page)
                    body = str(last.get("body") or "").strip()
                    changed = bool(body and body != baseline_body and len(body) >= 20)
                    question_seen = question in str(last.get("pageText") or "")
                    if changed and question_seen and not last.get("busy"):
                        current = time.monotonic()
                        stable_since = (stable_since or current) if body == previous else current
                        if body == previous and current - stable_since >= stable_seconds:
                            output.update(last)
                            return
                    else:
                        stable_since = None
                    previous = body
                raise TimeoutError(f"腾讯元宝回答等待超时：正文 {len(str(last.get('body') or ''))} 字")
            except Exception as exc:
                error.append(exc)

        self.session.fetch(
            self.site.home_url,
            page_action=action,
            wait=0,
            timeout=max(30000, timeout * 1000),
            network_idle=False,
            disable_resources=False,
        )
        if error:
            raise error[0]
        if not output:
            raise RuntimeError("腾讯元宝 Scrapling 页面未返回采集结果")
        sources = [item for item in output.get("sources") or [] if isinstance(item, dict)]
        return {
            "body": str(output.get("body") or "").strip(),
            "sources": sources,
            "url": str(output.get("url") or ""),
            "expected_source_count": len(sources),
            "source_capture_complete": True,
            "capture_mode": "scrapling_incognito_stealth_web",
        }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="腾讯元宝 Scrapling 隐身浏览器")
    parser.add_argument("--login", action="store_true", help="打开可见浏览器并等待登录")
    parser.add_argument("--check", action="store_true", help="无头检查登录状态")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    collector = YuanbaoScraplingCollector(headless=not args.login)
    try:
        value = collector.wait_for_login(args.timeout) if args.login else collector.check_ready()
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0 if value.get("ok") else 2
    finally:
        collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
