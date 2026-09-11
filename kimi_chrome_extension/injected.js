(function () {
  "use strict";
  if (window.__quarkMonitorNetworkHookInstalled) return;
  window.__quarkMonitorNetworkHookInstalled = true;

  const MAX_BODY = 5_000_000;
  const URL_PATTERN = /https?:\\?\/\\?\/[^\s"'<>\\\]\[\u3000]+/gi;
  const URL_KEYS = [
    "url", "href", "link", "sourceUrl", "source_url", "pageUrl", "page_url", "referUrl", "refer_url",
    "targetUrl", "target_url", "webUrl", "web_url", "jumpUrl", "jump_url", "originalUrl", "original_url",
    "landingPageUrl", "landing_page_url", "shareUrl", "share_url", "displayUrl", "display_url",
    "videoUrl", "video_url", "articleUrl", "article_url", "docUrl", "doc_url",
    "linkUrl", "link_url", "webUri", "web_uri", "sourceLink", "source_link"
  ];
  const TITLE_KEYS = [
    "title", "webTitle", "web_title", "pageTitle", "page_title", "docTitle", "doc_title",
    "articleTitle", "article_title", "videoTitle", "video_title", "displayTitle", "display_title",
    "sourceTitle", "source_title", "headline", "caption", "name", "sourceName", "source_name",
    "siteName", "site_name", "text", "abstract", "summary"
  ];

  function titleFrom(value, inherited) {
    if (!value || typeof value !== "object") return inherited || "";
    for (const key of TITLE_KEYS) {
      const candidate = value[key];
      if (typeof candidate !== "string") continue;
      const clean = candidate.replace(/\s+/g, " ").trim();
      if (clean.length >= 3 && clean.length <= 500 && !/^https?:\/\//i.test(clean)) return clean;
    }
    return inherited || "";
  }

  function emit(items, requestUrl) {
    if (!items.length) return;
    window.dispatchEvent(new CustomEvent("quark-monitor:sources", {
      detail: { items: items.slice(0, 500), requestUrl: String(requestUrl || "") }
    }));
  }

  function scanText(text, requestUrl) {
    if (!text || text.length > MAX_BODY) return;
    const output = [];
    const seen = new Set();
    const push = (url, title) => {
      const clean = String(url || "").replace(/\\\//g, "/").replace(/[),.;，。；]+$/, "");
      if (!/^https?:\/\//i.test(clean) || seen.has(clean)) return;
      seen.add(clean);
      output.push({ url: clean, title: String(title || "").slice(0, 500), via: "network" });
    };
    const visited = new Set();
    const walk = (value, depth, inheritedTitle) => {
      if (depth > 15 || output.length >= 500) return;
      if (typeof value === "string") {
        const trimmed = value.trim();
        if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && trimmed.length <= MAX_BODY) {
          try { walk(JSON.parse(trimmed), depth + 1, inheritedTitle); } catch (_) {}
        }
        return;
      }
      if (Array.isArray(value)) return value.forEach((item) => walk(item, depth + 1, inheritedTitle));
      if (!value || typeof value !== "object" || visited.has(value)) return;
      visited.add(value);
      const title = titleFrom(value, inheritedTitle);
      for (const key of URL_KEYS) {
        if (typeof value[key] === "string") push(value[key], title);
      }
      Object.values(value).forEach((item) => walk(item, depth + 1, title));
    };
    try { walk(JSON.parse(text), 0, ""); } catch (_) {}
    for (const line of text.split(/\r?\n/)) {
      const payload = line.replace(/^\s*(?:data|event)\s*:\s*/i, "").trim();
      if (!payload || payload === "[DONE]" || !/^[{[]/.test(payload)) continue;
      try { walk(JSON.parse(payload), 0, ""); } catch (_) {}
    }
    for (const match of text.match(URL_PATTERN) || []) push(match, "");
    emit(output, requestUrl);
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = async function (...args) {
      const response = await originalFetch.apply(this, args);
      try {
        const clone = response.clone();
        const type = String(clone.headers.get("content-type") || "");
        if (/json|text|event-stream|octet-stream/i.test(type)) {
          clone.text().then((body) => scanText(body, clone.url)).catch(() => {});
        }
      } catch (_) {}
      return response;
    };
  }

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__quarkMonitorUrl = url;
    return originalOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    this.addEventListener("load", function () {
      try {
        if (typeof this.responseText === "string") scanText(this.responseText, this.responseURL || this.__quarkMonitorUrl);
      } catch (_) {}
    }, { once: true });
    return originalSend.apply(this, args);
  };

  const OriginalWebSocket = window.WebSocket;
  if (typeof OriginalWebSocket === "function") {
    window.WebSocket = new Proxy(OriginalWebSocket, {
      construct(Target, args) {
        const socket = new Target(...args);
        socket.addEventListener("message", (event) => {
          try {
            if (typeof event.data === "string") scanText(event.data, String(args[0] || "websocket"));
          } catch (_) {}
        });
        return socket;
      }
    });
  }

  window.addEventListener("quark-monitor:reset", () => {
    window.dispatchEvent(new CustomEvent("quark-monitor:sources", { detail: { reset: true, items: [] } }));
  });
})();
