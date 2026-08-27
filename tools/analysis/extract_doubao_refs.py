import csv
import json
import sys
import time
import urllib.error
import urllib.request


CDP_HOST = "http://127.0.0.1:9222"
OUT_JSON = "doubao_reference_links.json"
OUT_CSV = "doubao_reference_links.csv"


EXTRACT_JS = r"""
(() => {
  function uniq(items) {
    const seen = new Set();
    const result = [];
    for (const item of items) {
      if (!item.href || seen.has(item.href)) continue;
      seen.add(item.href);
      result.push(item);
    }
    return result;
  }

  function cleanText(text) {
    return String(text || '')
      .replace(/\s+/g, ' ')
      .replace(/^\d+\.\s*/, '')
      .trim();
  }

  const all = Array.from(document.querySelectorAll('div, section, article, main'));
  const refBlocks = all
    .filter(el => /搜索\s*\d+\s*个关键词[\s\S]*参考\s*\d+\s*篇资料/.test(el.innerText || ''))
    .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);

  const block = refBlocks[0] || document.body;

  const links = Array.from(block.querySelectorAll('a[href]'))
    .map((a, idx) => {
      const href = new URL(a.getAttribute('href'), location.href).href;
      const title = cleanText(a.innerText || a.textContent || a.getAttribute('aria-label') || '');
      return {
        index: idx + 1,
        title,
        href
      };
    })
    .filter(item => item.href.startsWith('http'))
    .filter(item => item.title || /douyin|iesdouyin|toutiao|baidu|bilibili|xiaohongshu|jd|taobao|tmall/i.test(item.href));

  return uniq(links).map((item, index) => ({
    index: index + 1,
    title: item.title,
    href: item.href
  }));
})()
"""


def request_json(url, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def find_target_page():
    pages = request_json(f"{CDP_HOST}/json")
    page_candidates = [p for p in pages if p.get("type") == "page"]

    keywords = ("doubao", "豆包", "larus", "coze")
    for page in page_candidates:
        text = f"{page.get('title', '')} {page.get('url', '')}".lower()
        if any(k.lower() in text for k in keywords):
            return page

    if page_candidates:
        return page_candidates[0]

    raise RuntimeError("没有找到可连接的 Chrome 页面")


def cdp_call(websocket_debugger_url, method, params=None):
    # Use the HTTP devtools endpoint mapped from the websocket URL.
    # ws://127.0.0.1:9222/devtools/page/ABC -> http://127.0.0.1:9222/json/runtime/evaluate is not available,
    # so we call Runtime.evaluate through the page-specific websocket with websocket-client if present.
    try:
        import websocket
    except Exception as exc:
        raise RuntimeError(
            "缺少 websocket-client。请先运行：python -m pip install websocket-client"
        ) from exc

    ws = websocket.create_connection(websocket_debugger_url, timeout=10)
    try:
        message_id = 1
        ws.send(json.dumps({
            "id": message_id,
            "method": method,
            "params": params or {},
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == message_id:
                if "error" in msg:
                    raise RuntimeError(json.dumps(msg["error"], ensure_ascii=False))
                return msg.get("result", {})
    finally:
        ws.close()


def extract_links(wait_seconds=10):
    if wait_seconds > 0:
        time.sleep(wait_seconds)

    page = find_target_page()
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("目标页面没有 webSocketDebuggerUrl，请确认 Chrome 使用调试端口启动")

    result = cdp_call(ws_url, "Runtime.evaluate", {
        "expression": EXTRACT_JS,
        "awaitPromise": True,
        "returnByValue": True,
    })

    value = result.get("result", {}).get("value")
    if not isinstance(value, list):
        raise RuntimeError("没有获取到链接数组：" + json.dumps(result, ensure_ascii=False))

    return value


def save_files(rows):
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["index", "title", "href"])
        writer.writeheader()
        writer.writerows(rows)


def main():
    wait_seconds = 10
    if len(sys.argv) >= 2:
        wait_seconds = int(sys.argv[1])

    try:
        rows = extract_links(wait_seconds)
        save_files(rows)
        print(json.dumps({
            "ok": True,
            "count": len(rows),
            "json": OUT_JSON,
            "csv": OUT_CSV,
            "items": rows,
        }, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
        }, ensure_ascii=False))
        raise


if __name__ == "__main__":
    main()
