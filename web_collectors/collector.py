from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from monitor_core.plugins import ROOT

from .browser import SitePage, launch_browser, session_cookies, storage_env_name
from .config import SiteConfig, site_config


_PROVIDER_NAVIGATION_MARKER = re.compile(
    r"^(?:近期对话|最近对话|历史对话|全部对话|对话历史|我的对话|"
    r"历史会话|最近会话|我的会话|新对话|新建对话|新会话|新建会话|"
    r"recent\s+chats?|chat\s+history|new\s+chat)\s*$",
    re.I,
)


def _clean_answer_body(question: str, body: str) -> str:
    question_key = re.sub(r"\s+", "", str(question or "")).casefold()
    output: list[str] = []
    skip_prompt_after_role = False
    for raw_line in str(body or "").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if _PROVIDER_NAVIGATION_MARKER.fullmatch(line):
            break
        line_key = re.sub(r"\s+", "", line).casefold()
        if not output and line_key in {"用户", "user"}:
            skip_prompt_after_role = True
            continue
        if skip_prompt_after_role and question_key and line_key == question_key:
            skip_prompt_after_role = False
            continue
        if not output and line_key in {"助手", "assistant"}:
            continue
        if not output and question_key and line_key == question_key:
            continue
        output.append(line)
    return "\n".join(output).strip()


def _js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class BrowserCollector:
    def __init__(self, model: str, *, headless: bool = True):
        self.site: SiteConfig = site_config(model)
        self.browser = launch_browser(self.site, headless=headless)
        try:
            self.page = SitePage(self.site)
            self.page.call("Page.enable")
            self.page.call("Runtime.enable")
            raw_storage = str(os.environ.get(storage_env_name(self.site.id)) or "").strip()
            if raw_storage:
                try:
                    storage = json.loads(raw_storage)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"{storage_env_name(self.site.id)} 不是有效的 JSON") from exc
                if isinstance(storage, dict):
                    self.page.evaluate(fr"""
(()=>{{
  const state={_js(storage)};
  for(const [key,value] of Object.entries(state.local_storage||{{}}))localStorage.setItem(key,String(value));
  for(const [key,value] of Object.entries(state.session_storage||{{}}))sessionStorage.setItem(key,String(value));
  return true;
}})()
""")
            cookies = session_cookies(self.site)
            # DeepSeek and Kimi use dedicated persistent local profiles so a
            # successful manual login survives worker/browser restarts.  Their
            # readiness is proved from the live page below; an exported cookie
            # environment variable is only required for incognito collectors.
            self.has_session_cookies = bool(cookies) or self.site.id in {"deepseek", "kimi"}
            if self.has_session_cookies:
                self.page.call("Network.enable")
                self.page.call("Network.setCookies", {"cookies": cookies})
        except Exception:
            self.browser.close()
            raise

    def close(self) -> None:
        try:
            if self.page.ws is not None:
                self.page.ws.close()
                self.page.ws = None
        finally:
            self.browser.close()

    def navigate_home(self) -> None:
        self.page.call("Page.navigate", {"url": self.site.home_url}, timeout=30)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.page.evaluate("document.readyState")
            if state in {"interactive", "complete"}:
                return
            time.sleep(0.5)
        raise TimeoutError(f"{self.site.name} 网页加载超时")

    def _input_state(self, *, focus: bool = True) -> dict[str, Any]:
        return self.page.evaluate(fr"""
(()=>{{
  const selectors={_js(self.site.input_selectors)};
  const shouldFocus={_js(focus)};
  const visible=(el)=>{{const r=el.getBoundingClientRect();const s=getComputedStyle(el);return r.width>8&&r.height>8&&s.display!=='none'&&s.visibility!=='hidden';}};
  for(const selector of selectors){{
    for(const el of document.querySelectorAll(selector)){{
      if(!visible(el)||el.disabled||el.getAttribute('aria-disabled')==='true')continue;
      const r=el.getBoundingClientRect();
      if(shouldFocus){{el.scrollIntoView({{block:'center'}});el.focus();}}
      return {{ok:true,tag:el.tagName,url:location.href,x:r.left+r.width/2,y:r.top+r.height/2}};
    }}
  }}
  return {{ok:false,url:location.href,title:document.title,text:String(document.body?.innerText||'').slice(0,1000)}};
}})()
""") or {}

    def check_ready(self) -> dict[str, Any]:
        if not self.has_session_cookies:
            from .browser import cookie_env_name
            return {
                "ok": False,
                "status": "login_required",
                "message": f"隐身会话需要临时设置 {cookie_env_name(self.site.id)}",
                "web": {},
                "location": "local",
            }
        self.navigate_home()
        deadline = time.monotonic() + 20
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            # Login dialogs contain phone/code fields. This check must remain
            # passive or it steals focus back to the chat composer every second.
            state = self._input_state(focus=False)
            body_text = str(self.page.evaluate(
                "String(document.body?.innerText||'').slice(0,12000)"
            ) or "")
            logged_out = any(
                marker.casefold() in body_text.casefold()
                for marker in self.site.login_markers
            )
            if state.get("ok") and not logged_out:
                return {"ok": True, "status": "ready", "message": f"{self.site.name} 网页会话可用", "web": {"masked": "网页会话"}, "location": "local"}
            time.sleep(1)
        text = str(state.get("text") or "")
        marker = next((item for item in self.site.login_markers if item.casefold() in text.casefold()), "")
        return {"ok": False, "status": "login_required", "message": f"请在已打开的 {self.site.name} 网页完成登录" + (f"（检测到：{marker}）" if marker else ""), "web": {}, "location": "local"}

    def wait_for_login_cookies(
        self, *, timeout: int = 600, progress: Callable[[str], None] | None = None
    ) -> list[dict[str, Any]]:
        """Wait for a manual login and return cookies without persisting them."""
        self.page.call("Network.enable")
        self.navigate_home()
        time.sleep(1.5)

        def cookies() -> list[dict[str, Any]]:
            payload = self.page.call("Network.getAllCookies") or {}
            values = payload.get("cookies") if isinstance(payload, dict) else []
            output = []
            for item in values or []:
                if not isinstance(item, dict) or not str(item.get("name") or ""):
                    continue
                output.append({
                    key: item[key]
                    for key in (
                        "name", "value", "domain", "path", "secure", "httpOnly",
                        "expires", "sameSite",
                    )
                    if key in item
                })
            return output

        deadline = time.monotonic() + max(30, timeout)
        next_progress = 0.0
        consecutive_ready = 0
        while time.monotonic() < deadline:
            # Never steal focus from phone/code fields in the active login dialog.
            state = self._input_state(focus=False)
            body_text = str(self.page.evaluate(
                "String(document.body?.innerText||'').slice(0,12000)"
            ) or "")
            logged_out = any(marker in body_text for marker in self.site.login_markers)
            current = cookies()
            consecutive_ready = (
                consecutive_ready + 1
                if state.get("ok") and not logged_out and current else 0
            )
            if consecutive_ready >= 2:
                return current
            moment = time.monotonic()
            if progress and moment >= next_progress:
                remaining = max(0, int(deadline - moment))
                progress(f"等待你在本机 {self.site.name} 隐身窗口完成登录（剩余 {remaining} 秒）")
                next_progress = moment + 3
            time.sleep(1)
        raise TimeoutError(f"{self.site.name} 登录等待超时，请重新发起登录")

    def storage_state(self) -> dict[str, dict[str, str]]:
        value = self.page.evaluate("""
(()=>({
  local_storage:Object.fromEntries(Object.entries(localStorage)),
  session_storage:Object.fromEntries(Object.entries(sessionStorage)),
}))()
""") or {}
        return {
            "local_storage": {
                str(key): str(item) for key, item in (value.get("local_storage") or {}).items()
            },
            "session_storage": {
                str(key): str(item) for key, item in (value.get("session_storage") or {}).items()
            },
        }

    def _snapshot(self) -> dict[str, Any]:
        return self.page.evaluate(fr"""
(()=>{{
  const siteId={_js(self.site.id)};
  const selectors={_js(self.site.answer_selectors)};
  const internal={_js(self.site.internal_hosts)};
  const visible=(el)=>{{const r=el.getBoundingClientRect();const s=getComputedStyle(el);return r.width>8&&r.height>8&&s.display!=='none'&&s.visibility!=='hidden';}};
  let nodes=[];
  if(siteId==='kimi'){{
    // Kimi's tool-search reasoning uses the same `.markdown` class as the
    // final answer, but the current UI no longer always wraps the answer in
    // `.chat-content-item-assistant`.  Exclude tool-call markdown directly and
    // take the last remaining rendered answer.
    nodes=[...document.querySelectorAll('main .markdown, .markdown')]
      .filter(el=>!el.closest('.toolcall-content'));
  }}
  if(!nodes.length)for(const selector of selectors)nodes.push(...document.querySelectorAll(selector));
  nodes=[...new Set(nodes)].filter(el=>visible(el)&&String(el.innerText||'').trim().length>=20);
  if(!nodes.length)nodes=[...document.querySelectorAll('main article, main [class*=answer], main [class*=markdown], main [class*=message]')].filter(el=>visible(el)&&String(el.innerText||'').trim().length>=20);
  const node=nodes[nodes.length-1]||null;
  let body=String(node?.innerText||'').replace(/\n{{3,}}/g,'\n\n').trim();
  if(siteId==='deepseek'){{
    // DeepSeek can render one answer as several sibling `.ds-markdown`
    // fragments. Taking only the last fragment silently saved a short closing
    // tip while discarding the main answer. A diagnosis opens a fresh chat, so
    // all visible final-answer fragments belong to the current response.
    const fragments=[...document.querySelectorAll('.ds-markdown,[class*=ds-markdown]')]
      .filter(el=>visible(el)&&String(el.innerText||'').trim().length>=12);
    const unique=[];
    for(const fragment of fragments){{
      const text=String(fragment.innerText||'').replace(/\n{{3,}}/g,'\n\n').trim();
      if(text&&!unique.some(value=>value===text||value.includes(text)))unique.push(text);
    }}
    if(unique.length)body=unique.join('\n\n');
  }}
  const scope=node?.closest('[class*=message],[class*=answer],[class*=assistant],article')||node||document.querySelector('main')||document;
  const anchors=[...scope.querySelectorAll('a[href],a[data-href],a[data-url]')];
  for(const card of scope.querySelectorAll('[class*=source],[class*=citation],[class*=reference],[data-testid*=source],[data-testid*=citation]')){{
    anchors.push(...card.querySelectorAll('a[href],a[data-href],a[data-url]'));
  }}
  const seen=new Set(),sources=[];
  for(const a of anchors){{
    try{{
      let raw=a.getAttribute('data-url')||a.getAttribute('data-href')||a.getAttribute('href')||a.href;
      let u=new URL(raw,location.href);
      for(let depth=0;depth<3;depth++){{
        const host=u.hostname.replace(/^www\./,'').toLowerCase();
        if(!internal.some(x=>host===x||host.endsWith('.'+x)))break;
        const target=['url','target','redirect','redirect_url','dest','destination'].map(k=>u.searchParams.get(k)).find(Boolean);
        if(!target)break;
        try{{u=new URL(decodeURIComponent(target),location.href);}}catch{{break;}}
      }}
      const host=u.hostname.replace(/^www\./,'').toLowerCase();
      if(!/^https?:$/.test(u.protocol)||internal.some(x=>host===x||host.endsWith('.'+x)))continue;
      u.hash='';
      let title=String(a.innerText||a.title||a.getAttribute('aria-label')||host).replace(/\s+/g,' ').trim();
      if(!title||/^[-–—\s]*\d+$/.test(title))title=host;
      if(!title||seen.has(u.href))continue;seen.add(u.href);sources.push({{title,url:u.href,href:u.href}});
    }}catch{{}}
  }}
  const text=String(document.body?.innerText||'');
  const busy=/正在生成|思考中|搜索中|停止生成|Stop generating/i.test(text)&&!!document.querySelector('button,[role=button]');
  const providerError=siteId==='kimi'&&/和Kimi聊(?:天)?的人太多了|Kimi有点累了|晚点再问我一遍|订阅会员可进入优先队列/.test(text)
    ? 'Kimi 当前请求较多，进入安全冷却后重试' : '';
  return {{url:location.href,title:document.title,body,busy,providerError,readyState:document.readyState,sources,pageText:text.slice(-3000)}};
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
        if self.site.id == "kimi":
            # Kimi's current Lexical editor treats Enter as text input in some
            # releases.  If the draft remains intact, click the actual send
            # control and verify later via the new conversation URL.
            time.sleep(0.5)
            draft = str(self.page.evaluate(
                "String(document.querySelector('.chat-input-editor')?.innerText||'').trim()"
            ) or "")
            if draft:
                if not self._click_center(".send-button-container"):
                    raise RuntimeError("Kimi 没有找到发送按钮")
        if self.site.id == "doubao":
            time.sleep(0.6)
            self.page.evaluate(r"""
(()=>{
  const input=document.querySelector('[contenteditable=true][role=textbox]');
  if(!input||!String(input.innerText||'').trim())return false;
  const ir=input.getBoundingClientRect();
  const candidates=[...document.querySelectorAll('button')].filter(button=>{
    const r=button.getBoundingClientRect();
    return !button.disabled&&r.width>=28&&r.width<=52&&r.height>=28&&r.height<=52&&
      r.x>=ir.right-80&&r.y>=ir.bottom&&r.y<=ir.bottom+80;
  });
  const send=candidates[candidates.length-1];
  if(!send)return false;
  send.click();
  return true;
})()
""")

    def _click_center(self, selector: str) -> bool:
        point = self.page.evaluate(fr"""
(()=>{{
  const element=document.querySelector({_js(selector)});
  if(!element)return null;
  const rect=element.getBoundingClientRect();
  if(rect.width<4||rect.height<4)return null;
  return {{x:rect.left+rect.width/2,y:rect.top+rect.height/2}};
}})()
""")
        if not isinstance(point, dict):
            return False
        coordinates = {"x": float(point["x"]), "y": float(point["y"])}
        self.page.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", **coordinates, "button": "left", "clickCount": 1,
        })
        self.page.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", **coordinates, "button": "left", "clickCount": 1,
        })
        return True

    def _ensure_kimi_standard(self) -> None:
        """Keep Kimi on the quick model with Standard thinking effort."""
        if self.site.id != "kimi":
            return
        current = str(self.page.evaluate(
            "String(document.querySelector('.current-effort')?.innerText||'').trim()"
        ) or "")
        if current in {"标准", "Standard"}:
            return
        if not self._click_center(".current-model"):
            raise RuntimeError("Kimi 没有找到模型选择入口")
        time.sleep(0.4)
        if not self._click_center(".effort-item"):
            raise RuntimeError("Kimi 没有找到思考强度入口")
        time.sleep(0.4)
        option = self.page.evaluate(r"""
(()=>{
  const options=[...document.querySelectorAll('.effort-option,[role=menuitemradio]')];
  const element=options.find(item=>/^(标准|Standard)$/i.test(String(item.innerText||'').trim()));
  if(!element)return null;
  const rect=element.getBoundingClientRect();
  return rect.width>4&&rect.height>4
    ? {x:rect.left+rect.width/2,y:rect.top+rect.height/2}:null;
})()
""")
        if not isinstance(option, dict):
            raise RuntimeError("Kimi 当前页面没有 Standard（标准）思考强度")
        coordinates = {"x": float(option["x"]), "y": float(option["y"])}
        self.page.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", **coordinates, "button": "left", "clickCount": 1,
        })
        self.page.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", **coordinates, "button": "left", "clickCount": 1,
        })
        time.sleep(0.6)
        selected = str(self.page.evaluate(
            "String(document.querySelector('.current-effort')?.innerText||'').trim()"
        ) or "")
        if selected not in {"标准", "Standard"}:
            raise RuntimeError(f"Kimi Standard 模式切换未生效：{selected or '未知'}")

    def _conversation_links(self) -> set[str]:
        values = self.page.evaluate(r"""
(()=>[...document.querySelectorAll('a[href]')]
  .map(a=>a.href).filter(href=>/kimi\.com\/(?:chat|c)\//i.test(href)))()
""") or []
        return {str(item) for item in values if str(item).startswith("http")}

    def _wait_submitted_conversation(
        self, question: str, previous_url: str, previous_links: set[str], timeout: int = 20
    ) -> str:
        deadline = time.monotonic() + max(8, timeout)
        while time.monotonic() < deadline:
            current = str(self.page.evaluate("location.href") or "")
            main_text = str(self.page.evaluate(
                "String((document.querySelector('.layout-content-main')||document.querySelector('main')||document.body)?.innerText||'')"
            ) or "")
            if any(marker in main_text for marker in (
                "和Kimi聊的人太多了", "和Kimi聊天的人太多了",
                "Kimi有点累了", "订阅会员可进入优先队列",
                "请求过于频繁",
            )):
                raise RuntimeError("Kimi 当前请求较多，进入安全冷却后重试")
            if question in main_text and current and current != previous_url and "/chat/" in current:
                return current
            links = self._conversation_links() - previous_links
            if question in main_text and links:
                return sorted(links)[-1]
            time.sleep(0.5)
        raise RuntimeError(f"{self.site.name} 问题已发送但没有取得独立会话地址")

    def _wait_current_answer(
        self, question: str, *, timeout: int, stable_seconds: int,
        baseline_body: str = "",
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(30, timeout)
        stable_since: float | None = None
        previous = ""
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            time.sleep(1.5)
            last = self._snapshot()
            if last.get("providerError"):
                raise RuntimeError(str(last["providerError"]))
            body = str(last.get("body") or "").strip()
            changed = bool(body and body != baseline_body and len(body) >= 20)
            current_text = str(self.page.evaluate(
                "String((document.querySelector('.layout-content-main')||document.querySelector('main')||document.body)?.innerText||'').slice(-20000)"
            ) or "")
            question_seen = question in current_text
            if changed and question_seen and not last.get("busy") and last.get("readyState") == "complete":
                current = time.monotonic()
                stable_since = (stable_since or current) if body == previous else current
                if body == previous and current - stable_since >= stable_seconds:
                    return last
            else:
                stable_since = None
            previous = body
        raise TimeoutError(
            f"{self.site.name} 回答等待超时：正文 {len(str(last.get('body') or ''))} 字"
        )

    def _capture_result(self, last: dict[str, Any], question: str) -> dict[str, Any]:
        special = self._doubao_result()
        if special and special.get("body"):
            last.update(special)
        body = _clean_answer_body(question, str(last.get("body") or ""))
        if len(re.sub(r"\s+", "", body)) < 6:
            raise RuntimeError(f"{self.site.name} 未抓到完整回答正文，本轮将自动重试")
        if self.site.id == "kimi" and any(marker in body for marker in (
            "和Kimi聊的人太多了", "和Kimi聊天的人太多了",
            "Kimi有点累了", "晚点再问我一遍", "订阅会员可进入优先队列",
            "服务繁忙", "请求过于频繁",
        )):
            raise RuntimeError("Kimi 暂时繁忙，本轮将自动退避后重试")
        sources = [
            item for item in last.get("sources") or []
            if isinstance(item, dict)
            and str(item.get("url") or item.get("href") or "").startswith(("http://", "https://"))
        ]
        expected = int(last.get("expected_source_count") or len(sources))
        return {
            "body": body,
            "sources": sources,
            "url": str(last.get("url") or ""),
            "expected_source_count": expected,
            "source_capture_complete": bool(last.get("source_capture_complete", len(sources) >= expected)),
            "capture_mode": "incognito_headless_web",
        }

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
        self._ensure_kimi_standard()
        baseline = self._snapshot()
        baseline_body = str(baseline.get("body") or "")
        self._submit(question)
        last = self._wait_current_answer(
            question, timeout=timeout, stable_seconds=stable_seconds,
            baseline_body=baseline_body,
        )
        return self._capture_result(last, question)

    def collect_batch(
        self, questions: list[str], *, timeout: int = 240,
        settle_seconds: int = 15, stable_seconds: int = 4,
    ) -> list[dict[str, Any]]:
        """Submit Kimi conversations first, then revisit each URL to harvest."""
        clean = [str(item or "").strip() for item in questions]
        if self.site.id != "kimi" or len(clean) < 2 or any(not item for item in clean):
            return [self.collect(item, timeout=timeout, stable_seconds=stable_seconds) for item in clean]
        self.navigate_home()
        ready = self.check_ready()
        if not ready.get("ok"):
            raise RuntimeError(str(ready.get("message") or "网页会话未登录"))
        conversations: list[tuple[str, str]] = []
        for question in clean:
            self.navigate_home()
            self._ensure_kimi_standard()
            previous_url = str(self.page.evaluate("location.href") or "")
            previous_links = self._conversation_links()
            self._submit(question)
            url = self._wait_submitted_conversation(
                question, previous_url, previous_links,
                timeout=int(os.environ.get("GEO_KIMI_SUBMIT_CONFIRM_SECONDS", "20")),
            )
            conversations.append((question, url))
            time.sleep(float(os.environ.get("GEO_KIMI_BURST_GAP_SECONDS", "1.5")))
        time.sleep(max(10, int(settle_seconds)))
        output: list[dict[str, Any]] = []
        for question, url in conversations:
            self.page.call("Page.navigate", {"url": url}, timeout=30)
            last = self._wait_current_answer(
                question, timeout=timeout, stable_seconds=stable_seconds,
            )
            output.append(self._capture_result(last, question))
        return output


def create_collector(model: str, *, headless: bool = True):
    if str(model).strip().casefold() == "yuanbao":
        from .scrapling_yuanbao import YuanbaoScraplingCollector
        return YuanbaoScraplingCollector(headless=headless)
    return BrowserCollector(model, headless=headless)
