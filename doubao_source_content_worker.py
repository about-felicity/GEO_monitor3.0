import argparse
import csv
from collections import defaultdict
import hashlib
import html as html_module
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from lxml import etree, html

import doubao_dashboard_server as dashboard
import doubao_brand_settings as brand_settings
from monitor_core import database as monitor_database
from monitor_core import analytics as monitor_analytics


BASE_DIR = Path(__file__).resolve().parent
SCRAPLING_DIR = BASE_DIR / "Scrapling"
REFS_CSV = BASE_DIR / "doubao_refs_result.csv"
PRODUCTS_CSV = BASE_DIR / "doubao_products_result.csv"
WEB_RESULTS = BASE_DIR / "runtime" / "web_results"
DEEPSEEK_RESULTS = WEB_RESULTS / "deepseek_results.jsonl"
YUANBAO_RESULTS = WEB_RESULTS / "yuanbao_results.jsonl"
WENXIN_RESULTS = WEB_RESULTS / "wenxin_results.jsonl"
QUARK_RESULTS = WEB_RESULTS / "quark_results.jsonl"
INDEX_PATH = BASE_DIR / "doubao_source_content_index.json"
DB_PATH = BASE_DIR / "doubao_source_content.db"
LOCK_PATH = BASE_DIR / "doubao_source_content_worker.lock"
LOG_PATH = BASE_DIR / "doubao_source_content_worker.log"
LOG_MAX_BYTES = max(1_000_000, int(os.environ.get("DOUBAO_CONTENT_LOG_MAX_BYTES", "20000000") or 20000000))
LOG_BACKUPS = max(1, min(5, int(os.environ.get("DOUBAO_CONTENT_LOG_BACKUPS", "2") or 2)))
INDEX_PUBLISH_INTERVAL = max(
    5.0, float(os.environ.get("DOUBAO_CONTENT_INDEX_PUBLISH_INTERVAL", "20") or 20)
)
CST = timezone(timedelta(hours=8))

MAX_BYTES = max(500_000, int(os.environ.get("DOUBAO_CONTENT_MAX_BYTES", "4000000") or 4000000))
MAX_TEXT_CHARS = max(20_000, int(os.environ.get("DOUBAO_CONTENT_MAX_TEXT", "300000") or 300000))
WORKERS = max(1, min(12, int(os.environ.get("DOUBAO_CONTENT_WORKERS", "3") or 3)))
BATCH_SIZE = max(1, int(os.environ.get("DOUBAO_CONTENT_BATCH", "24") or 24))
DYNAMIC_WORKERS = max(
    1, min(3, int(os.environ.get("DOUBAO_CONTENT_DYNAMIC_WORKERS", "1") or 1))
)
REQUEST_TIMEOUT = max(5, int(os.environ.get("DOUBAO_CONTENT_TIMEOUT", "18") or 18))
HOST_DELAY = max(0.0, float(os.environ.get("DOUBAO_CONTENT_HOST_DELAY", "0.35") or 0.35))
MIN_CONTENT_CHARS = 80
SOURCE_CONTENT_SCOPE_SCHEMA_VERSION = 1

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
}

HOST_LOCKS = {}
HOST_LOCKS_GUARD = threading.Lock()
DYNAMIC_FETCH_LOCK = threading.Semaphore(DYNAMIC_WORKERS)
_BRAND_CACHE = {"mtime": None, "value": None}
_URL_CACHE = {"mtime": None, "value": None}
_LOG_LOCK = threading.Lock()
_INDEX_PUBLISH_LAST = [0.0]


def now_str():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    try:
        with _LOG_LOCK:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size >= LOG_MAX_BYTES:
                oldest = Path(str(LOG_PATH) + f".{LOG_BACKUPS}")
                oldest.unlink(missing_ok=True)
                for index in range(LOG_BACKUPS - 1, 0, -1):
                    source = Path(str(LOG_PATH) + f".{index}")
                    if source.exists():
                        os.replace(source, Path(str(LOG_PATH) + f".{index + 1}"))
                os.replace(LOG_PATH, Path(str(LOG_PATH) + ".1"))
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(now_str() + " " + str(message) + "\n")
    except Exception:
        pass


def atomic_json_write(path, payload):
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".json", dir=BASE_DIR)
    os.close(fd)
    try:
        with open(temp_name, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def publish_index(index, force=False):
    """Publish labels without rewriting a multi-megabyte file per URL."""
    now = time.monotonic()
    if not force and now - _INDEX_PUBLISH_LAST[0] < INDEX_PUBLISH_INTERVAL:
        return False
    atomic_json_write(INDEX_PATH, index)
    _INDEX_PUBLISH_LAST[0] = now
    return True


def load_index():
    if not INDEX_PATH.exists():
        return {"version": 1, "updated_at": "", "vocab_hash": "", "entries": {}}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("index root is not an object")
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data
    except Exception as exc:
        log("index load failed: " + repr(exc))
        return {"version": 1, "updated_at": "", "vocab_hash": "", "entries": {}}


def init_db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_content (
            url TEXT PRIMARY KEY,
            final_url TEXT,
            status TEXT NOT NULL,
            fetched_at TEXT,
            content_type TEXT,
            http_status INTEGER,
            extraction_method TEXT,
            extraction_quality TEXT,
            title TEXT,
            content_text TEXT,
            text_length INTEGER DEFAULT 0,
            content_hash TEXT,
            error TEXT,
            attempts INTEGER DEFAULT 0
        )
        """
    )
    connection.commit()
    return connection


def save_db_row(connection, url, result):
    connection.execute(
        """
        INSERT INTO source_content (
            url, final_url, status, fetched_at, content_type, http_status,
            extraction_method, extraction_quality, title, content_text,
            text_length, content_hash, error, attempts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            final_url=excluded.final_url,
            status=excluded.status,
            fetched_at=excluded.fetched_at,
            content_type=excluded.content_type,
            http_status=excluded.http_status,
            extraction_method=excluded.extraction_method,
            extraction_quality=excluded.extraction_quality,
            title=excluded.title,
            content_text=excluded.content_text,
            text_length=excluded.text_length,
            content_hash=excluded.content_hash,
            error=excluded.error,
            attempts=excluded.attempts
        """,
        (
            url, result.get("final_url", ""), result.get("status", "error"),
            result.get("fetched_at", ""), result.get("content_type", ""),
            result.get("http_status"), result.get("extraction_method", ""),
            result.get("extraction_quality", ""), result.get("title", ""),
            result.get("content_text", ""), result.get("text_length", 0),
            result.get("content_hash", ""), result.get("error", ""),
            result.get("attempts", 0),
        ),
    )
    connection.commit()


def get_db_text(connection, url):
    row = connection.execute(
        "SELECT content_text FROM source_content WHERE url=? AND status='ok'", (url,)
    ).fetchone()
    return str(row[0] or "") if row else ""


def get_db_result(connection, url):
    row = connection.execute(
        """
        SELECT final_url, status, fetched_at, content_type, http_status,
               extraction_method, extraction_quality, title, content_text,
               text_length, content_hash, error, attempts
        FROM source_content WHERE url=? AND status='ok'
        """,
        (url,),
    ).fetchone()
    if not row:
        return None
    keys = (
        "final_url", "status", "fetched_at", "content_type", "http_status",
        "extraction_method", "extraction_quality", "title", "content_text",
        "text_length", "content_hash", "error", "attempts",
    )
    return dict(zip(keys, row))


def safe_public_url(url):
    try:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False, "unsupported URL scheme"
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False, "non-public destination"
        return True, ""
    except Exception as exc:
        return False, "DNS/URL validation failed: " + str(exc)[:180]


def normalize_text(value):
    text = html_module.unescape(str(value or ""))
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


SOURCE_BODY_END_PREFIXES = (
    "上一篇", "下一篇", "免责声明", "相关阅读", "相关推荐", "相关文章",
    "相关资讯", "相关内容", "延伸阅读", "猜你喜欢", "热门推荐", "热门文章",
    "本月阅读榜",
)


def primary_article_text(value):
    """Remove recommendation/sidebar text appended after the article body.

    Some publishers expose no semantic ``article`` node, so the fallback HTML
    extractor receives the whole page.  Brand names in ``上一篇``、推荐阅读 or a
    monthly ranking are navigation, not evidence that the current article
    mentions an owned product.  Keep the full body archive for diagnostics but
    classify only the primary article portion.
    """
    text = normalize_text(value)
    if not text:
        return ""
    kept = []
    kept_chars = 0
    for line in text.splitlines():
        stripped = line.strip()
        compact = re.sub(r"\s+", "", stripped)
        is_end_marker = (
            any(compact.startswith(prefix) for prefix in SOURCE_BODY_END_PREFIXES)
            or bool(re.fullmatch(r".{0,16}(?:推荐阅读|本月阅读榜|热门阅读榜)[:：]?", compact))
        )
        if kept_chars >= 200 and is_end_marker:
            break
        kept.append(line)
        kept_chars += len(stripped)
    primary = normalize_text("\n".join(kept))
    return primary or text


def decode_bytes(raw, content_type=""):
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type or "", re.I)
    encodings = [match.group(1)] if match else []
    encodings += ["utf-8", "gb18030", "gbk", "big5", "latin1"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def json_text_values(value, output):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {
                "articlebody", "description", "transcript", "caption", "text", "video_description"
            } and isinstance(item, str):
                cleaned = normalize_text(item)
                if len(cleaned) >= 20:
                    output.append(cleaned)
            else:
                json_text_values(item, output)
    elif isinstance(value, list):
        for item in value:
            json_text_values(item, output)


def embedded_descriptions(raw_html):
    values = []
    for key in ("desc", "description", "video_description", "caption"):
        pattern = re.compile(r'"' + re.escape(key) + r'"\s*:\s*("(?:\\.|[^"\\])*")', re.I)
        for match in pattern.finditer(raw_html):
            try:
                value = normalize_text(json.loads(match.group(1)))
            except Exception:
                continue
            if 20 <= len(value) <= 20000:
                values.append(value)
            if len(values) >= 12:
                return values
    return values


def extract_html_content(raw_html):
    title = ""
    metadata = []
    method = "body_fallback"
    quality = "low"
    try:
        root = html.fromstring(raw_html)
    except (etree.ParserError, ValueError):
        plain = normalize_text(re.sub(r"<[^>]+>", " ", raw_html))
        return "", plain[:MAX_TEXT_CHARS], "html_regex_fallback", "low"

    title_nodes = root.xpath("//title/text()")
    if title_nodes:
        title = normalize_text(title_nodes[0])
    for xpath in (
        "//meta[@name='description']/@content",
        "//meta[@property='og:description']/@content",
        "//meta[@name='twitter:description']/@content",
    ):
        for value in root.xpath(xpath):
            cleaned = normalize_text(value)
            if len(cleaned) >= 20:
                metadata.append(cleaned)

    for raw_json in root.xpath("//script[@type='application/ld+json']/text()"):
        try:
            json_text_values(json.loads(raw_json), metadata)
        except Exception:
            continue
    metadata.extend(embedded_descriptions(raw_html))

    for node in root.xpath("//script|//style|//noscript|//svg|//canvas|//form|//nav|//header|//footer|//aside"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)

    candidates = []
    selectors = (
        "//article", "//main", "//*[@id='js_content']", "//*[@id='article-content']",
        "//*[contains(@class,'article-content')]", "//*[contains(@class,'article_content')]",
        "//*[contains(@class,'post-content')]", "//*[contains(@class,'post_content')]",
        "//*[contains(@class,'rich_media_content')]", "//*[contains(@class,'content-detail')]",
        "//*[contains(@class,'detail-content')]",
    )
    seen_nodes = set()
    for selector in selectors:
        for node in root.xpath(selector)[:20]:
            marker = id(node)
            if marker in seen_nodes:
                continue
            seen_nodes.add(marker)
            text = normalize_text(node.text_content())
            if len(text) >= MIN_CONTENT_CHARS:
                candidates.append(text)

    if candidates:
        body = max(candidates, key=len)
        method = "article_node"
        quality = "high" if len(body) >= 300 else "medium"
    else:
        bodies = root.xpath("//body")
        body = normalize_text(bodies[0].text_content()) if bodies else normalize_text(root.text_content())
        quality = "medium" if len(body) >= 500 else "low"

    parts = []
    fingerprints = set()
    for value in metadata + [body]:
        cleaned = normalize_text(value)
        fingerprint = hashlib.sha1(cleaned[:2000].encode("utf-8", errors="ignore")).hexdigest() if cleaned else ""
        if len(cleaned) < 20 or fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        parts.append(cleaned)
    return title, normalize_text("\n".join(parts))[:MAX_TEXT_CHARS], method, quality


def fetch_with_cdp(url, timeout=30):
    """Fetch a URL via an existing Chrome DevTools Protocol instance.

    Sites such as smzdm.com return a probe/captcha shell to plain HTTP
    clients. When the optional source-debug Chrome is running on port 9222
    (see scripts/operations/open_source_debug_browser.bat), this renders the page in a real
    browser and returns the visible text.
    """
    try:
        import websocket
    except Exception:
        return None

    cdp_host = os.environ.get("DOUBAO_CDP_HOST", "127.0.0.1")
    cdp_port = int(os.environ.get("DOUBAO_CDP_PORT", "9222"))
    list_url = f"http://{cdp_host}:{cdp_port}/json/list"

    ws_url = None
    try:
        req = urllib.request.Request(list_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
        for tab in tabs:
            if tab.get("type") == "page" and not tab.get("url", "").startswith(f"http://{cdp_host}:{cdp_port}"):
                ws_url = tab.get("webSocketDebuggerUrl")
                if ws_url:
                    break
        if not ws_url:
            new_url = f"http://{cdp_host}:{cdp_port}/json/new?about:blank"
            req = urllib.request.Request(new_url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                tab = json.loads(resp.read().decode("utf-8"))
            ws_url = tab.get("webSocketDebuggerUrl")
    except Exception as exc:
        log("CDP list failed: " + repr(exc))
        return None

    if not ws_url:
        return None

    ws = None
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
        next_id = [0]

        def send(method, params=None):
            next_id[0] += 1
            payload = {"id": next_id[0], "method": method}
            if params:
                payload["params"] = params
            ws.send(json.dumps(payload))
            return next_id[0]

        def recv_until(deadline, predicate):
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                ws.settimeout(min(1.5, remaining))
                try:
                    msg = json.loads(ws.recv())
                except Exception:
                    continue
                if predicate(msg):
                    return msg
            return None

        send("Page.enable")
        send("Page.navigate", {"url": url})

        nav_deadline = time.time() + timeout
        loaded = recv_until(nav_deadline, lambda m: m.get("method") == "Page.loadEventFired")
        if not loaded:
            log("CDP navigation timed out for " + url)
            return None

        # Allow lazy JS rendering before reading text.
        time.sleep(2.5)

        send("Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True})
        body_msg = recv_until(
            time.time() + 5,
            lambda m: "result" in m and isinstance(m["result"].get("result", {}).get("value"), str),
        )
        body_text = ""
        if body_msg:
            body_text = body_msg["result"]["result"]["value"]

        if len(body_text) < MIN_CONTENT_CHARS:
            return None

        send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
        title_msg = recv_until(
            time.time() + 3,
            lambda m: "result" in m and isinstance(m["result"].get("result", {}).get("value"), str),
        )
        title = ""
        if title_msg:
            title = title_msg["result"]["result"]["value"]

        content_text = normalize_text(body_text)[:MAX_TEXT_CHARS]
        return {
            "status": "ok",
            "fetched_at": now_str(),
            "attempts": 1,
            "http_status": 200,
            "content_type": "text/html",
            "final_url": url,
            "title": normalize_text(title),
            "extraction_method": "cdp_chrome",
            "extraction_quality": "high",
            "content_text": content_text,
            "text_length": len(content_text),
            "content_hash": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
            "error": "",
            "next_retry_at": "",
        }
    except Exception as exc:
        log("CDP fetch failed: " + repr(exc))
        return None
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


def brand_vocabulary():
    database_enabled = monitor_database.enabled()
    mtime = (
        # The live PostgreSQL pipeline updates the legacy product CSV
        # continuously. Treating that file as vocabulary configuration forced
        # every archived body to be re-labelled after nearly every callback.
        0 if database_enabled else (
            PRODUCTS_CSV.stat().st_mtime_ns if PRODUCTS_CSV.exists() else 0
        ),
        brand_settings.SETTINGS_PATH.stat().st_mtime_ns
        if brand_settings.SETTINGS_PATH.exists() else 0,
    )
    if _BRAND_CACHE["mtime"] == mtime and _BRAND_CACHE["value"] is not None:
        return _BRAND_CACHE["value"]
    brands = set(dashboard.KNOWN_BRANDS)
    configured = brand_settings.load_settings()
    brands.update(item["name"] for item in brand_settings.vocabulary(configured))
    if not database_enabled and PRODUCTS_CSV.exists():
        with PRODUCTS_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                value = str(row.get("brand_name") or "").strip()
                if value and not dashboard.is_invalid_brand_candidate(value):
                    brands.add(dashboard.canonical_brand_name(value))
    brands.discard("")
    ordered = sorted(brands, key=lambda value: value.casefold())
    own_rule_fingerprint = json.dumps(
        dashboard.OWN_PRODUCT_RULES, ensure_ascii=False, sort_keys=True
    )
    brand_settings_fingerprint = json.dumps(
        configured, ensure_ascii=False, sort_keys=True
    )
    digest = hashlib.sha256(
        (
            "\0".join(ordered) + "\0" + own_rule_fingerprint
            + "\0" + brand_settings_fingerprint
            + "\0own-schema:" + str(dashboard.OWN_PRODUCT_SCHEMA_VERSION)
            + "\0brand-match-schema:" + str(dashboard.BRAND_MATCH_SCHEMA_VERSION)
            + "\0source-scope-schema:" + str(SOURCE_CONTENT_SCOPE_SCHEMA_VERSION)
        ).encode("utf-8")
    ).hexdigest()[:16]
    value = (ordered, digest)
    _BRAND_CACHE.update({"mtime": mtime, "value": value})
    return value


def detect_brands(content_text, brands):
    if not content_text:
        return []
    raw = str(content_text).casefold()
    compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", raw)

    def alias_occurs(alias):
        alias_folded = str(alias or "").casefold()
        token = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", alias_folded)
        if not token:
            return False
        if not re.search(r"[\u3400-\u9fff]", token):
            return bool(re.search(
                r"(?<![a-z0-9])" + re.escape(alias_folded) + r"(?![a-z0-9])",
                raw,
            ))
        contexts = monitor_analytics.AMBIGUOUS_BRAND_CONTEXTS.get(token)
        if not contexts:
            return token in compact
        if re.search(
            r"(?<![\u3400-\u9fff])" + re.escape(alias_folded) + r"(?![\u3400-\u9fff])",
            raw,
        ):
            return True
        start = 0
        while True:
            position = compact.find(token, start)
            if position < 0:
                return False
            left = compact[max(0, position - 6):position]
            right = compact[position + len(token):position + len(token) + 8]
            if any(left.endswith(cue) for cue in contexts["left"]):
                return True
            if any(right.startswith(cue) for cue in contexts["right"]):
                return True
            start = position + 1

    def brand_occurs(brand):
        return any(alias_occurs(alias) for alias in dashboard.aliases_for_brand(brand))

    return sorted(
        (brand for brand in brands if brand_occurs(brand)),
        key=lambda value: value.casefold(),
    )


def retry_delay_seconds(attempts, blocked=False):
    schedule = [60, 300, 1800, 7200, 21600, 43200, 86400, 172800]
    delay = schedule[min(max(0, attempts - 1), len(schedule) - 1)]
    return max(delay, 21600) if blocked else delay


def is_skipped_source_url(url):
    # The old requests-only worker skipped smzdm entirely. Scrapling can fetch
    # it with a browser TLS fingerprint, so article sources are no longer
    # silently omitted from owned-brand analysis.
    return False


def fetch_http_with_scrapling(url):
    """Return a lightweight HTTP response tuple using the bundled Scrapling."""
    if SCRAPLING_DIR.exists() and str(SCRAPLING_DIR) not in sys.path:
        sys.path.insert(0, str(SCRAPLING_DIR))
    from scrapling.fetchers import Fetcher

    page = Fetcher.get(
        url,
        impersonate="chrome",
        stealthy_headers=True,
        follow_redirects="safe",
        timeout=REQUEST_TIMEOUT,
        retries=2,
    )
    raw = page.body
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="ignore")
    raw = bytes(raw or b"")[:MAX_BYTES]
    headers = page.headers
    content_type = str(
        headers.get("Content-Type")
        or headers.get("content-type")
        or ""
    )
    return {
        "status_code": int(page.status),
        "final_url": str(page.url or url),
        "content_type": content_type,
        "raw": raw,
        "transport": "scrapling",
    }


def fetch_http_with_stealthy_scrapling(url):
    """Render a script-heavy article in a hidden real browser as last fallback."""
    if SCRAPLING_DIR.exists() and str(SCRAPLING_DIR) not in sys.path:
        sys.path.insert(0, str(SCRAPLING_DIR))
    from scrapling.fetchers import StealthyFetcher

    with DYNAMIC_FETCH_LOCK:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            real_chrome=True,
            disable_resources=True,
            network_idle=False,
            load_dom=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            timeout=REQUEST_TIMEOUT * 1000,
            wait=800,
        )
    raw = page.body
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="ignore")
    raw = bytes(raw or b"")[:MAX_BYTES]
    headers = page.headers
    content_type = str(
        headers.get("Content-Type")
        or headers.get("content-type")
        or "text/html"
    )
    return {
        "status_code": int(page.status),
        "final_url": str(page.url or url),
        "content_type": content_type,
        "raw": raw,
        "transport": "scrapling_stealth",
    }


def fetch_http_with_requests(url):
    response = requests.get(
        url, headers=HEADERS, timeout=(6, REQUEST_TIMEOUT), stream=True,
        allow_redirects=True,
    )
    raw_parts = []
    total = 0
    for chunk in response.iter_content(65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_BYTES:
            raw_parts.append(chunk[: max(0, MAX_BYTES - (total - len(chunk)))])
            break
        raw_parts.append(chunk)
    return {
        "status_code": int(response.status_code),
        "final_url": str(response.url),
        "content_type": str(response.headers.get("Content-Type") or ""),
        "raw": b"".join(raw_parts),
        "transport": "requests",
    }


def iso_after(seconds):
    return (datetime.now(CST) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def fetch_one(url, previous_attempts=0, source_title=""):
    attempts = previous_attempts + 1
    allow_dynamic_browser = attempts <= 2
    fetched_at = now_str()
    safe, reason = safe_public_url(url)
    if not safe:
        return {
            "status": "error", "fetched_at": fetched_at, "attempts": attempts,
            "error": reason, "next_retry_at": iso_after(retry_delay_seconds(attempts, True)),
            "content_text": "", "text_length": 0,
        }

    host = urlparse(url).hostname or ""
    if is_skipped_source_url(url):
        return {
            "status": "skipped",
            "fetched_at": fetched_at,
            "attempts": attempts,
            "final_url": url,
            "error": "按配置跳过什么值得买正文",
            "next_retry_at": "",
            "content_text": "",
            "text_length": 0,
        }
    with HOST_LOCKS_GUARD:
        host_lock = HOST_LOCKS.setdefault(host, threading.Lock())
    try:
        with host_lock:
            try:
                response = fetch_http_with_requests(url)
            except Exception as request_exc:
                log("Requests failed; Scrapling HTTP fallback for %s: %s" % (
                    url, repr(request_exc)[:240],
                ))
                response = fetch_http_with_scrapling(url)
            if (
                int(response.get("status_code") or 0) >= 400
                and allow_dynamic_browser
                and not dashboard.own_product_mentions(source_title)
            ):
                try:
                    rendered = fetch_http_with_stealthy_scrapling(url)
                    if int(rendered.get("status_code") or 0) < int(response.get("status_code") or 999):
                        response = rendered
                except Exception as render_exc:
                    log("Scrapling browser status fallback failed for %s: %s" % (
                        url, repr(render_exc)[:240],
                    ))
            final_url = response["final_url"]
            final_safe, final_reason = safe_public_url(final_url)
            if not final_safe:
                raise ValueError("unsafe redirect: " + final_reason)
            raw = response["raw"]
            if HOST_DELAY:
                time.sleep(HOST_DELAY)
        content_type = response["content_type"]
        status_code = response["status_code"]
        transport = response["transport"]
        if status_code >= 400:
            blocked = status_code in (401, 403, 407, 409, 429) or status_code >= 500
            return {
                "status": "blocked" if blocked else "error", "fetched_at": fetched_at,
                "attempts": attempts, "http_status": status_code, "content_type": content_type,
                "final_url": final_url, "error": "HTTP %s" % status_code,
                "next_retry_at": iso_after(retry_delay_seconds(attempts, blocked)),
                "content_text": "", "text_length": 0,
            }
        if "pdf" in content_type.casefold() or final_url.casefold().endswith(".pdf"):
            return {
                "status": "unsupported", "fetched_at": fetched_at, "attempts": attempts,
                "http_status": status_code, "content_type": content_type, "final_url": final_url,
                "error": "PDF正文解析器暂不可用", "next_retry_at": iso_after(7 * 86400),
                "content_text": "", "text_length": 0,
            }
        decoded = decode_bytes(raw, content_type)
        title, content_text, method, quality = extract_html_content(decoded)
        if (
            len(content_text) < MIN_CONTENT_CHARS
            and transport != "scrapling_stealth"
            and allow_dynamic_browser
            and not dashboard.own_product_mentions(source_title)
        ):
            try:
                with host_lock:
                    rendered = fetch_http_with_stealthy_scrapling(url)
                    if HOST_DELAY:
                        time.sleep(HOST_DELAY)
                rendered_url = rendered["final_url"]
                rendered_safe, rendered_reason = safe_public_url(rendered_url)
                if not rendered_safe:
                    raise ValueError("unsafe rendered redirect: " + rendered_reason)
                if int(rendered.get("status_code") or 0) < 400:
                    rendered_decoded = decode_bytes(
                        rendered["raw"], rendered["content_type"]
                    )
                    rendered_title, rendered_text, rendered_method, rendered_quality = extract_html_content(rendered_decoded)
                    if len(rendered_text) > len(content_text):
                        final_url = rendered_url
                        content_type = rendered["content_type"]
                        status_code = int(rendered["status_code"])
                        transport = rendered["transport"]
                        title = rendered_title or title
                        content_text = rendered_text
                        method = rendered_method
                        quality = rendered_quality
            except Exception as render_exc:
                log("Scrapling browser content fallback failed for %s: %s" % (
                    url, repr(render_exc)[:240],
                ))
        if len(content_text) < MIN_CONTENT_CHARS:
            return {
                "status": "empty", "fetched_at": fetched_at, "attempts": attempts,
                "http_status": status_code, "content_type": content_type, "final_url": final_url,
                "title": title, "extraction_method": transport + "_" + method,
                "extraction_quality": quality,
                "error": "正文过短或页面仅有脚本壳", "next_retry_at": iso_after(retry_delay_seconds(attempts)),
                "content_text": content_text, "text_length": len(content_text),
            }
        return {
            "status": "ok", "fetched_at": fetched_at, "attempts": attempts,
            "http_status": status_code, "content_type": content_type, "final_url": final_url,
            "title": title, "extraction_method": transport + "_" + method,
            "extraction_quality": quality,
            "content_text": content_text, "text_length": len(content_text),
            "content_hash": hashlib.sha256(content_text.encode("utf-8")).hexdigest(),
            "error": "", "next_retry_at": "",
        }
    except Exception as exc:
        return {
            "status": "error", "fetched_at": fetched_at, "attempts": attempts,
            "error": (type(exc).__name__ + ": " + str(exc))[:500],
            "next_retry_at": iso_after(retry_delay_seconds(attempts)),
            "content_text": "", "text_length": 0,
        }


def _load_jsonl_sources(path):
    """从模型采集 jsonl 读取信源 URL,返回 {url: (run_no, title)} 字典。

    覆盖 DeepSeek 和元宝的信源,使正文 worker 不再只处理豆包 CSV。
    """
    result = {}
    if not path.exists():
        return result
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if record.get("status") != "success":
                continue
            run_no = int(record.get("round") or 0)
            day = str(record.get("day") or dashboard.analytics_beijing_day(record.get("finished_at") or record.get("captured_at") or ""))
            for source in record.get("sources") or []:
                url = str(source.get("url") or source.get("href") or "").strip()
                if not url:
                    continue
                title = str(source.get("title") or "")
                current = result.get(url)
                if current is None or run_no > current[0]:
                    result[url] = (run_no, title, day)
    except Exception as exc:
        log("jsonl sources load failed for %s: %s" % (path, repr(exc)))
    return result


def _load_database_sources(days=7):
    """Read recent PostgreSQL callbacks for body analysis.

    Remote collectors now commit directly to PostgreSQL and deliberately no
    longer append their main-machine JSONL files.  Without this feed, newly
    collected Quark/Wenxin/Yuanbao/DeepSeek articles remain ``content pending`` and
    cannot receive owned-brand labels even when their body names the brand.
    """
    buckets = {"doubao": {}, "deepseek": {}, "yuanbao": {}, "wenxin": {}, "quark": {}}
    if not monitor_database.enabled():
        return buckets
    try:
        monitor_database.ensure_schema()
        with monitor_database.connection() as connection:
            rows = connection.execute(
                "WITH recent AS ("
                " SELECT r.model_id,r.sequence_no,r.day,s.source_index,s.payload,"
                " COALESCE(NULLIF(btrim(s.payload->>'url'),''),"
                "          NULLIF(btrim(s.payload->>'href'),'')) AS source_url"
                " FROM monitor_sources s JOIN monitor_runs r"
                " ON r.model_id=s.model_id AND r.run_id=s.run_id"
                " WHERE r.day >= (now() AT TIME ZONE 'Asia/Shanghai')::date - %s"
                "), ranked AS ("
                " SELECT model_id,sequence_no,day,payload,source_url,"
                " COUNT(*) OVER (PARTITION BY model_id,source_url) AS frequency,"
                " ROW_NUMBER() OVER (PARTITION BY model_id,source_url"
                " ORDER BY day DESC,sequence_no DESC,source_index DESC) AS row_no"
                " FROM recent WHERE source_url IS NOT NULL"
                ") SELECT model_id,sequence_no,day,payload,frequency"
                " FROM ranked WHERE row_no=1",
                (max(0, int(days) - 1),),
            ).fetchall()
        for row in rows:
            model_id = str(row.get("model_id") or "")
            if model_id not in buckets:
                continue
            source = dict(row.get("payload") or {})
            url = str(source.get("url") or source.get("href") or "").strip()
            if not url:
                continue
            run_no = int(row.get("sequence_no") or 0)
            buckets[model_id][url] = (
                run_no,
                str(source.get("title") or ""),
                str(row.get("day") or ""),
                int(row.get("frequency") or 1),
            )
    except Exception as exc:
        log("database sources load failed: %s" % repr(exc))
    return buckets


def collect_urls():
    database_enabled = monitor_database.enabled()
    # PostgreSQL is the live source of truth.  The legacy CSV/JSONL exports are
    # large historical snapshots (currently more than 130 MB) and rereading
    # them on every callback makes the worker look alive while doing no fetches.
    # Keep them only as an offline fallback when PostgreSQL is unavailable.
    source_files = () if database_enabled else (
        REFS_CSV, DEEPSEEK_RESULTS, YUANBAO_RESULTS, WENXIN_RESULTS, QUARK_RESULTS
    )
    file_mtime = sum(f.stat().st_mtime_ns if f.exists() else 0 for f in source_files)
    try:
        database_version = monitor_database.global_version()
    except Exception:
        database_version = 0
    cache_token = (file_mtime, database_version)
    if _URL_CACHE["mtime"] == cache_token and _URL_CACHE["value"] is not None:
        return _URL_CACHE["value"]
    buckets = {"doubao": {}, "deepseek": {}, "yuanbao": {}, "wenxin": {}, "quark": {}}
    if not database_enabled and REFS_CSV.exists():
        with REFS_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                url = str(row.get("href") or "").strip()
                if not url:
                    continue
                try:
                    run_no = int(str(row.get("run_no") or "0") or 0)
                except (TypeError, ValueError):
                    run_no = 0
                current = buckets["doubao"].get(url)
                if current is None or run_no > current[0]:
                    buckets["doubao"][url] = (run_no, str(row.get("title") or ""), str(row.get("day") or row.get("captured_day") or ""))
    if not database_enabled:
        for model_id, jsonl_path in (("deepseek", DEEPSEEK_RESULTS), ("yuanbao", YUANBAO_RESULTS),
                                     ("wenxin", WENXIN_RESULTS), ("quark", QUARK_RESULTS)):
            for url, (run_no, title, day) in _load_jsonl_sources(jsonl_path).items():
                current = buckets[model_id].get(url)
                if current is None or run_no > current[0]:
                    buckets[model_id][url] = (run_no, title, day)
    database_buckets = _load_database_sources() if database_enabled else {}
    for model_id, values in database_buckets.items():
        for url, meta in values.items():
            run_no, title, day = meta[:3]
            frequency = int(meta[3] if len(meta) > 3 else 1)
            current = buckets[model_id].get(url)
            if current is None or run_no > current[0]:
                buckets[model_id][url] = (run_no, title, day, frequency)
    ordered = {
        model_id: sorted(
            ({"url": url, "run_no": meta[0], "title": meta[1], "day": meta[2],
              "frequency": int(meta[3] if len(meta) > 3 else 1), "model": model_id}
             for url, meta in values.items()), key=lambda item: (-item["run_no"], item["url"])
        )
        for model_id, values in buckets.items()
    }
    # Fair round-robin prevents a large Doubao backlog from starving a newly
    # added model's article bodies and therefore its owned-brand labels.
    value, seen = [], set()
    max_len = max((len(rows) for rows in ordered.values()), default=0)
    for index in range(max_len):
        for model_id in ("deepseek", "yuanbao", "wenxin", "quark", "doubao"):
            rows = ordered[model_id]
            if index >= len(rows) or rows[index]["url"] in seen:
                continue
            seen.add(rows[index]["url"])
            value.append(rows[index])
    _URL_CACHE.update({"mtime": cache_token, "value": value})
    return value


def article_urls(urls):
    selected = []
    for item in urls:
        row = {"href": item["url"], "title": item.get("title", "")}
        source_type, _media, _host, _note = dashboard.source_for(row, {}, {})
        if "文章" in source_type:
            selected.append(item)
    return selected


def due(entry, vocab_hash):
    if not entry:
        return True
    if entry.get("status") == "ok":
        # A vocabulary change only requires re-labelling archived successful
        # bodies. It must not bypass the retry backoff of thousands of blocked
        # or script-only historical pages.
        return entry.get("vocab_hash") != vocab_hash
    if entry.get("status") == "skipped":
        return True
    next_retry = str(entry.get("next_retry_at") or "")
    if not next_retry:
        return True
    try:
        return datetime.fromisoformat(next_retry) <= datetime.now(CST)
    except Exception:
        return True


def prioritize_pending(items, entries):
    """Put never-seen sources ahead of retries and vocabulary refreshes."""
    today = datetime.now(CST).date().isoformat()
    return sorted(items, key=lambda item: (
        0 if item.get("day") == today and not entries.get(item["url"]) else
        1 if item.get("day") == today and (entries.get(item["url"]) or {}).get("status") != "ok" else
        2 if not entries.get(item["url"]) else
        3 if (entries.get(item["url"]) or {}).get("status") != "ok" else
        4,
        -int(item.get("frequency") or 1),
        -int(item.get("run_no") or 0),
    ))


def fair_pending_selection(items, limit):
    """Reserve each batch for every model while preserving local priority.

    Yuanbao currently emits many more article links than the other collectors.
    Taking the first global N rows lets that backlog starve Wenxin and Doubao,
    which leaves their owned-brand labels pending indefinitely.
    """
    groups = defaultdict(list)
    for item in items:
        groups[str(item.get("model") or "other")].append(item)
    order = [name for name in ("wenxin", "yuanbao", "doubao", "deepseek", "quark", "other") if groups[name]]
    selected = []
    index = 0
    while order and len(selected) < limit:
        name = order[index % len(order)]
        rows = groups[name]
        if rows:
            selected.append(rows.pop(0))
        if not rows:
            order.remove(name)
            index = 0
        else:
            index += 1
    return selected


def public_entry(result, brands, vocab_hash):
    content_text = result.get("content_text", "")
    analysis_text = primary_article_text(content_text)
    hits = detect_brands(analysis_text, brands) if result.get("status") == "ok" else []
    configured = brand_settings.load_settings()
    group_lookup = {
        dashboard.canonical_brand_name(item["name"]): item["group"]
        for item in brand_settings.vocabulary(configured)
    }
    return {
        "status": result.get("status", "error"),
        "fetched_at": result.get("fetched_at", ""),
        "next_retry_at": result.get("next_retry_at", ""),
        "attempts": result.get("attempts", 0),
        "http_status": result.get("http_status"),
        "content_type": result.get("content_type", ""),
        "final_url": result.get("final_url", ""),
        "title": result.get("title", ""),
        "extraction_method": result.get("extraction_method", ""),
        "extraction_quality": result.get("extraction_quality", ""),
        "text_length": result.get("text_length", 0),
        "content_hash": result.get("content_hash", ""),
        "brand_mentions": hits,
        "owned_brand_mentions": [
            brand for brand in hits if group_lookup.get(brand) == "owned"
        ],
        "competitor_brand_mentions": [
            brand for brand in hits if group_lookup.get(brand) == "competitor"
        ],
        "own_product_mentions": dashboard.own_product_mentions(analysis_text),
        "own_product_schema_version": dashboard.OWN_PRODUCT_SCHEMA_VERSION,
        "source_scope_schema_version": SOURCE_CONTENT_SCOPE_SCHEMA_VERSION,
        "excerpt": analysis_text[:1200],
        "error": result.get("error", ""),
        "vocab_hash": vocab_hash,
    }


def refresh_vocab_only(connection, url, entry, brands, vocab_hash):
    content_text = get_db_text(connection, url)
    if not content_text:
        # Some legacy successful rows predate the SQLite body archive. There
        # is nothing new to analyse for those URLs; repeatedly retrying all of
        # them on every pass only burns CPU. Preserve their existing evidence
        # and acknowledge the current vocabulary version.
        updated = dict(entry)
        updated["vocab_hash"] = vocab_hash
        updated["own_product_schema_version"] = dashboard.OWN_PRODUCT_SCHEMA_VERSION
        return updated
    updated = dict(entry)
    analysis_text = primary_article_text(content_text)
    hits = detect_brands(analysis_text, brands)
    configured = brand_settings.load_settings()
    group_lookup = {
        dashboard.canonical_brand_name(item["name"]): item["group"]
        for item in brand_settings.vocabulary(configured)
    }
    updated["brand_mentions"] = hits
    updated["owned_brand_mentions"] = [
        brand for brand in hits if group_lookup.get(brand) == "owned"
    ]
    updated["competitor_brand_mentions"] = [
        brand for brand in hits if group_lookup.get(brand) == "competitor"
    ]
    updated["own_product_mentions"] = dashboard.own_product_mentions(analysis_text)
    updated["own_product_schema_version"] = dashboard.OWN_PRODUCT_SCHEMA_VERSION
    updated["source_scope_schema_version"] = SOURCE_CONTENT_SCOPE_SCHEMA_VERSION
    updated["excerpt"] = analysis_text[:1200]
    updated["vocab_hash"] = vocab_hash
    return updated


def pid_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
            )
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            # PROCESS_QUERY_LIMITED_INFORMATION. The previous value was
            # SYNCHRONIZE, which opens successfully but makes
            # GetExitCodeProcess fail with access denied on Windows.
            handle = kernel32.OpenProcess(0x00001000, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    if LOCK_PATH.exists():
        try:
            owner_pid = LOCK_PATH.read_text(
                encoding="ascii", errors="ignore"
            ).strip()
            if pid_is_running(owner_pid):
                return False
        except Exception:
            pass
        try:
            LOCK_PATH.unlink()
        except Exception:
            return False
    descriptor = None
    try:
        descriptor = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        return True
    except FileExistsError:
        return False
    except Exception:
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def heartbeat():
    try:
        LOCK_PATH.touch()
    except Exception:
        pass


def owns_lock():
    """Return false as soon as a replacement worker owns the singleton lock."""
    try:
        return LOCK_PATH.read_text(
            encoding="ascii", errors="ignore"
        ).strip() == str(os.getpid())
    except Exception:
        return False


def release_lock():
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="ascii", errors="ignore").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except Exception:
        pass


def dashboard_running():
    try:
        with socket.create_connection(("127.0.0.1", dashboard.PORT), timeout=1):
            return True
    except OSError:
        return False


def process_batch(index, connection, urls, brands, vocab_hash, limit):
    entries = index.setdefault("entries", {})
    candidates = []
    skipped_now = 0
    for item in urls:
        url = item["url"]
        if not is_skipped_source_url(url):
            candidates.append(item)
            continue
        if (entries.get(url) or {}).get("status") == "skipped":
            continue
        result = {
            "status": "skipped",
            "fetched_at": now_str(),
            "attempts": int((entries.get(url) or {}).get("attempts") or 0),
            "final_url": url,
            "error": "按配置跳过什么值得买正文",
            "next_retry_at": "",
            "content_text": "",
            "text_length": 0,
        }
        save_db_row(connection, url, result)
        entries[url] = public_entry(result, brands, vocab_hash)
        skipped_now += 1
    pending = [
        item for item in candidates
        if due(entries.get(item["url"]), vocab_hash)
    ]
    # Never let a large historical retry/vocabulary-refresh backlog hide newly
    # received sources.  ``urls`` is already ordered newest-first with fair
    # model round-robin; Python's stable sort preserves that ordering inside
    # each priority class.
    pending = prioritize_pending(pending, entries)
    if not pending:
        if skipped_now:
            index["vocab_hash"] = vocab_hash
            index["updated_at"] = now_str()
            publish_index(index, force=True)
        return skipped_now, 0
    network_pending = []
    for item in pending:
        entry = entries.get(item["url"]) or {}
        if entry.get("status") == "ok" and entry.get("vocab_hash") != vocab_hash:
            updated = refresh_vocab_only(connection, item["url"], entry, brands, vocab_hash)
            if updated:
                entries[item["url"]] = updated
                continue
        network_pending.append(item)
    selected = fair_pending_selection(network_pending, limit)
    if not selected:
        index["vocab_hash"] = vocab_hash
        index["updated_at"] = now_str()
        publish_index(index, force=True)
        return 0, len(pending)

    ok = 0
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="source-content") as pool:
        futures = {
            pool.submit(
                fetch_one,
                item["url"],
                int((entries.get(item["url"]) or {}).get("attempts") or 0),
                item.get("title", ""),
            ): item
            for item in selected
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "status": "error", "fetched_at": now_str(), "attempts": 1,
                    "error": repr(exc)[:500], "next_retry_at": iso_after(300),
                    "content_text": "", "text_length": 0,
                }
            url = item["url"]
            if result.get("status") != "ok":
                archived = get_db_result(connection, url)
                if archived:
                    # A temporary 403/502 or script-shell response must not erase
                    # a previously successful article archive or product evidence.
                    entries[url] = public_entry(archived, brands, vocab_hash)
                    log(
                        "retain archived status=ok after refresh=%s url=%s error=%s"
                        % (result.get("status"), url, str(result.get("error") or "")[:160])
                    )
                    heartbeat()
                    continue
            save_db_row(connection, url, result)
            entries[url] = public_entry(result, brands, vocab_hash)
            if result.get("status") == "ok":
                ok += 1
            # Publish near-real-time without serializing the full index after
            # every single URL.
            index["vocab_hash"] = vocab_hash
            index["updated_at"] = now_str()
            publish_index(index)
            log(
                "%s model=%s status=%s chars=%s brands=%s url=%s error=%s"
                % (
                    "done" if result.get("status") == "ok" else "retry",
                    item.get("model") or "unknown",
                    result.get("status"), result.get("text_length", 0),
                    len(entries[url].get("brand_mentions") or []), url,
                    str(result.get("error") or "")[:160],
                )
            )
            heartbeat()
    index["vocab_hash"] = vocab_hash
    index["updated_at"] = now_str()
    publish_index(index, force=True)
    return skipped_now + len(selected), max(0, len(network_pending) - len(selected))


def refresh_all_vocab(connection, index, urls, brands, vocab_hash):
    entries = index.setdefault("entries", {})
    updated_count = 0
    for item in urls:
        url = item["url"]
        entry = entries.get(url) or {}
        if entry.get("status") != "ok":
            archived = get_db_result(connection, url)
            if archived:
                entries[url] = public_entry(archived, brands, vocab_hash)
                updated_count += 1
            continue
        updated = refresh_vocab_only(
            connection, url, entry, brands, vocab_hash
        )
        if updated:
            entries[url] = updated
            updated_count += 1
    index["vocab_hash"] = vocab_hash
    index["updated_at"] = now_str()
    publish_index(index, force=True)
    return updated_count


def include_archived_urls(urls, index):
    """Include archived bodies no longer present in today's collection catalog."""
    result = list(urls)
    seen = {str(item.get("url") or "") for item in result}
    for url, entry in (index.get("entries") or {}).items():
        if not url or url in seen or entry.get("status") != "ok":
            continue
        result.append({"url": url, "title": entry.get("title", ""), "model": "archived"})
        seen.add(url)
    return result


def main():
    parser = argparse.ArgumentParser(description="Incrementally archive public source-page content.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--vocab-only",
        action="store_true",
        help="Re-evaluate archived article bodies without making network requests.",
    )
    parser.add_argument("--limit", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="Exit when the dashboard process that launched this worker exits.",
    )
    parser.add_argument("--model", choices=("doubao", "deepseek", "yuanbao", "wenxin", "quark"))
    parser.add_argument("--day", help="Only process sources last seen on this Beijing date.")
    parser.add_argument(
        "--articles-only",
        action="store_true",
        help="Prioritize public article pages for owned-brand body analysis.",
    )
    args = parser.parse_args()
    if not acquire_lock():
        log("skip: worker already running")
        return
    connection = None
    try:
        connection = init_db()
        index = load_index()
        if args.vocab_only:
            brands, vocab_hash = brand_vocabulary()
            updated = refresh_all_vocab(
                connection,
                index,
                # Re-evaluate every archived body. Some publishers use URL
                # shapes that the coarse source classifier does not recognize
                # as articles, but their saved article body is still evidence.
                include_archived_urls(collect_urls(), index),
                brands,
                vocab_hash,
            )
            log("vocab refresh updated=%s" % updated)
            return
        log(
            "watch start workers=%s dynamic_workers=%s batch=%s parent_pid=%s"
            % (WORKERS, DYNAMIC_WORKERS, args.limit, args.parent_pid or "none")
        )
        while (
            args.once
            or (
                dashboard_running()
                and owns_lock()
                and (not args.parent_pid or pid_is_running(args.parent_pid))
            )
        ):
            heartbeat()
            brands, vocab_hash = brand_vocabulary()
            # Video ownership is title-only. Browser rendering is reserved for
            # article pages where body text changes the ownership verdict.
            urls = article_urls(collect_urls())
            if args.model:
                urls = [item for item in urls if item.get("model") == args.model]
            if args.day:
                urls = [item for item in urls if item.get("day") == args.day]
            processed, remaining = process_batch(
                index, connection, urls, brands, vocab_hash, max(1, args.limit)
            )
            counts = {}
            for entry in index.get("entries", {}).values():
                status = str(entry.get("status") or "missing")
                counts[status] = counts.get(status, 0) + 1
            log("pass processed=%s remaining=%s total=%s states=%s" % (processed, remaining, len(urls), counts))
            if args.once:
                break
            # Publish in larger, less frequent batches so the dashboard can
            # finish one statistics refresh before the content index changes.
            time.sleep(10 if processed and remaining else 30 if processed else 45)
        log("watch stop")
    finally:
        if connection is not None:
            connection.close()
        release_lock()


if __name__ == "__main__":
    main()
