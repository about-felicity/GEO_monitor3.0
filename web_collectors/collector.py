from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from monitor_core.plugins import ROOT

from .browser import SitePage, launch_browser
from .config import SiteConfig, site_config


def _js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class BrowserCollector:
    def __init__(self, model: str, *, headless: bool = True):
        self.site: SiteConfig = site_config(model)
        launch_browser(self.site, headless=headless)
        self.page = SitePage(self.site)
        self.page.call("Page.enable")
        self.page.call("Runtime.enable")

    def close(self) -> None:
        if self.page.ws is not None:
            self.page.ws.close()
            self.page.ws = None

    def navigate_home(self) -> None:
        self.page.call("Page.navigate", {"url": self.site.home_url}, timeout=30)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.page.evaluate("document.readyState")
            if state in {"interactive", "complete"}:
                return
            time.sleep(0.5)
        raise TimeoutError(f"{self.site.name} 网页加载超时")

    def _input_state(self) -> dict[str, Any]:
        return self.page.evaluate(fr"""
(()=>{{
  const selectors={_js(self.site.input_selectors)};
  const visible=(el)=>{{const r=el.getBoundingClientRect();const s=getComputedStyle(el);return r.width>8&&r.height>8&&s.display!=='none'&&s.visibility!=='hidden';}};
  for(const selector of selectors){{
    for(const el of document.querySelectorAll(selector)){{
      if(!visible(el)||el.disabled||el.getAttribute('aria-disabled')==='true')continue;
      const r=el.getBoundingClientRect();el.scrollIntoView({{block:'center'}});el.focus();
      return {{ok:true,tag:el.tagName,url:location.href,x:r.left+r.width/2,y:r.top+r.height/2}};
    }}
  }}
  return {{ok:false,url:location.href,title:document.title,text:String(document.body?.innerText||'').slice(0,1000)}};
}})()
""") or {}

    def check_ready(self) -> dict[str, Any]:
        self.navigate_home()
        deadline = time.monotonic() + 20
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = self._input_state()
            if state.get("ok"):
                return {"ok": True, "status": "ready", "message": f"{self.site.name} 网页会话可用", "web": {"masked": "网页会话"}, "location": "local"}
            time.sleep(1)
        text = str(state.get("text") or "")
        marker = next((item for item in self.site.login_markers if item.casefold() in text.casefold()), "")
        return {"ok": False, "status": "login_required", "message": f"请在已打开的 {self.site.name} 网页完成登录" + (f"（检测到：{marker}）" if marker else ""), "web": {}, "location": "local"}

    def _snapshot(self) -> dict[str, Any]:
        return self.page.evaluate(fr"""
(()=>{{
  const selectors={_js(self.site.answer_selectors)};
  const internal={_js(self.site.internal_hosts)};
  const visible=(el)=>{{const r=el.getBoundingClientRect();const s=getComputedStyle(el);return r.width>8&&r.height>8&&s.display!=='none'&&s.visibility!=='hidden';}};
  let nodes=[];
  for(const selector of selectors)nodes.push(...document.querySelectorAll(selector));
  nodes=[...new Set(nodes)].filter(el=>visible(el)&&String(el.innerText||'').trim().length>=20);
  if(!nodes.length)nodes=[...document.querySelectorAll('main article, main [class*=answer], main [class*=markdown], main [class*=message]')].filter(el=>visible(el)&&String(el.innerText||'').trim().length>=20);
  const node=nodes[nodes.length-1]||null;
  const body=String(node?.innerText||'').replace(/\n{{3,}}/g,'\n\n').trim();
  const anchors=[...(node||document.querySelector('main')||document).querySelectorAll('a[href]')];
  const seen=new Set(),sources=[];
  for(const a of anchors){{
    try{{
      const u=new URL(a.href,location.href);const host=u.hostname.replace(/^www\./,'').toLowerCase();
      if(!/^https?:$/.test(u.protocol)||internal.some(x=>host===x||host.endsWith('.'+x)))continue;
      const title=String(a.innerText||a.title||a.getAttribute('aria-label')||host).replace(/\s+/g,' ').trim();
      if(!title||seen.has(u.href))continue;seen.add(u.href);sources.push({{title,url:u.href,href:u.href}});
    }}catch{{}}
  }}
  const text=String(document.body?.innerText||'');
  const busy=/正在生成|思考中|搜索中|停止生成|Stop generating/i.test(text)&&!!document.querySelector('button,[role=button]');
  return {{url:location.href,title:document.title,body,busy,readyState:document.readyState,sources,pageText:text.slice(-3000)}};
}})()
""") or {}

    def _submit(self, question: str) -> None:
        state = self._input_state()
        if not state.get("ok"):
            raise RuntimeError(f"{self.site.name} 网页没有找到可用输入框，请先登录或更新网页适配器")
        self.page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2})
        self.page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2})
        self.page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace"})
        self.page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace"})
        self.page.call("Input.insertText", {"text": question})
        time.sleep(0.4)
        self.page.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter"})
        self.page.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter"})

    def _doubao_result(self) -> dict[str, Any] | None:
        if self.site.id != "doubao":
            return None
        loaded = self.page.evaluate("Boolean(window.__webCollectorDoubaoLoaded)")
        if not loaded:
            script = (ROOT / "doubao_ref_extension" / "content.js").read_text(encoding="utf-8")
            self.page.evaluate(script + "\nwindow.__webCollectorDoubaoLoaded=true;", timeout=30)
        self.page.evaluate("""
(()=>{
  const result=document.getElementById('__doubao_ref_result');
  if(result)result.value='';
  localStorage.removeItem('__doubao_ref_result');
  localStorage.setItem('__doubao_ref_command',String(Date.now()));
})()
""")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            value = self.page.evaluate("document.getElementById('__doubao_ref_result')?.value||''")
            if value:
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("status") != "running":
                    return {
                        "body": str(parsed.get("answerText") or ""),
                        "sources": [dict(item, url=str(item.get("href") or item.get("url") or "")) for item in parsed.get("items") or [] if isinstance(item, dict)],
                        "expected_source_count": int(parsed.get("expectedCount") or 0),
                        "source_capture_complete": bool(parsed.get("complete")),
                        "url": str(parsed.get("url") or ""),
                    }
            time.sleep(0.5)
        return None

    def collect(self, question: str, *, timeout: int = 240, stable_seconds: int = 6) -> dict[str, Any]:
        self.navigate_home()
        ready = self.check_ready()
        if not ready.get("ok"):
            raise RuntimeError(str(ready.get("message") or "网页会话未登录"))
        baseline = self._snapshot()
        baseline_body = str(baseline.get("body") or "")
        self._submit(question)
        deadline = time.monotonic() + max(30, timeout)
        stable_since: float | None = None
        previous = ""
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            time.sleep(1.5)
            last = self._snapshot()
            body = str(last.get("body") or "").strip()
            changed = bool(body and body != baseline_body and len(body) >= 20)
            question_seen = question in str(last.get("pageText") or "") or question in str(self.page.evaluate("String(document.body?.innerText||'').slice(-12000)") or "")
            if changed and question_seen and not last.get("busy") and last.get("readyState") == "complete":
                current = time.monotonic()
                stable_since = (stable_since or current) if body == previous else current
                if body == previous and current - stable_since >= stable_seconds:
                    break
            else:
                stable_since = None
            previous = body
        else:
            raise TimeoutError(f"{self.site.name} 回答等待超时：正文 {len(str(last.get('body') or ''))} 字")
        special = self._doubao_result()
        if special and special.get("body"):
            last.update(special)
        sources = [item for item in last.get("sources") or [] if isinstance(item, dict) and str(item.get("url") or item.get("href") or "").startswith(("http://", "https://"))]
        expected = int(last.get("expected_source_count") or len(sources))
        return {
            "body": str(last.get("body") or "").strip(),
            "sources": sources,
            "url": str(last.get("url") or ""),
            "expected_source_count": expected,
            "source_capture_complete": bool(last.get("source_capture_complete", len(sources) >= expected)),
            "capture_mode": "headless_web",
        }


def create_collector(model: str, *, headless: bool = True):
    if str(model).strip().casefold() == "yuanbao":
        from .scrapling_yuanbao import YuanbaoScraplingCollector
        return YuanbaoScraplingCollector(headless=headless)
    return BrowserCollector(model, headless=headless)
