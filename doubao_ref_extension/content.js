(function () {
  const RESULT_ID = "__doubao_ref_result";
  const STATUS_ID = "__doubao_ref_status";
  const COUNT_ID = "__doubao_ref_count";
  const COMPLETE_ID = "__doubao_ref_complete";
  const CHAT_TITLE_ID = "__doubao_ref_chat_title";
  const PANEL_ID = "__doubao_ref_panel";
  const PANEL_TEXTAREA_ID = "__doubao_ref_panel_textarea";
  const LATEST_GRAB_BUTTON_ID = "__doubao_ref_latest_grab";
  const STORAGE_KEY = "__doubao_ref_result";
  const COMMAND_KEY = "__doubao_ref_command";
  const HEADER_RE = /\u641c\u7d22\s*\d+\s*\u4e2a\u5173\u952e\u8bcd[\s\S]{0,120}?\u53c2\u8003\s*(\d+)\s*\u7bc7\u8d44\u6599/;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function ensureHiddenNode(id, tagName) {
    let node = document.getElementById(id);
    if (node) return node;

    node = document.createElement(tagName);
    node.id = id;
    node.style.position = "fixed";
    node.style.left = "-99999px";
    node.style.top = "-99999px";
    node.style.width = "1px";
    node.style.height = "1px";
    node.style.opacity = "0";
    node.setAttribute("aria-hidden", "true");
    document.documentElement.appendChild(node);
    return node;
  }

  function cleanText(text) {
    return String(text || "")
      .replace(/\s+/g, " ")
      .replace(/^\d+[\.\u3001]\s*/, "")
      .trim();
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  }

  function expectedReferenceCount() {
    const text = document.body ? document.body.innerText || "" : "";
    const match = text.match(HEADER_RE);
    return match ? Number(match[1]) || 0 : 0;
  }

  function currentChatTitle() {
    const title = cleanText(document.title || "");
    const heading = Array.from(document.querySelectorAll("h1, h2, [data-testid], div, span"))
      .filter((el) => isVisible(el))
      .map((el) => cleanText(el.innerText || el.textContent || ""))
      .find((text) => text && text.length <= 40 && !HEADER_RE.test(text) && !/^count:/.test(text));

    return heading || title;
  }

  function findReferenceHeader() {
    const candidates = Array.from(document.querySelectorAll("div, button, span"))
      .filter((el) => isVisible(el) && HEADER_RE.test(el.innerText || el.textContent || ""))
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return (ar.width * ar.height) - (br.width * br.height);
      });

    return candidates[0] || null;
  }

  function findReferenceMessageRoot() {
    const header = findReferenceHeader();
    if (!header) return document.body;

    let node = header;
    while (node && node !== document.body) {
      if (node.getAttribute && node.getAttribute("data-message-id")) return node;
      node = node.parentElement;
    }

    node = header;
    for (let i = 0; i < 8 && node && node.parentElement; i += 1) {
      node = node.parentElement;
      const text = node.innerText || "";
      if (HEADER_RE.test(text) && node.querySelectorAll("a[href]").length > 0) return node;
    }

    return document.body;
  }

  function clickReferenceHeader() {
    const header = findReferenceHeader();
    if (!header) return false;

    header.scrollIntoView({ block: "center", inline: "nearest" });
    const rect = header.getBoundingClientRect();
    const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2) || header;

    target.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, pointerType: "mouse" }));
    target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    target.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, pointerType: "mouse" }));
    target.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
    target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    return true;
  }

  function referenceAnchors() {
    const root = findReferenceMessageRoot();
    const anchors = Array.from(root.querySelectorAll("a[href]"));
    if (anchors.length > 0) return anchors;
    return Array.from(document.querySelectorAll("a[href]"));
  }

  function isReferenceHref(href) {
    try {
      const url = new URL(href, location.href);
      const host = url.hostname.replace(/^www\./, "");
      if (!url.protocol.startsWith("http")) return false;
      if (host === "doubao.com" && url.pathname.startsWith("/chat")) return false;
      if (host === "doubao.com" && url.pathname.startsWith("/bot")) return false;
      if (host === "doubao.com") return false;
      return true;
    } catch (_) {
      return false;
    }
  }

  function isChatHref(href) {
    try {
      const url = new URL(href, location.href);
      const host = url.hostname.replace(/^www\./, "");
      if (host !== "doubao.com") return false;
      if (!url.pathname.startsWith("/chat/")) return false;
      if (url.pathname.startsWith("/chat/create-image")) return false;
      if (url.pathname.startsWith("/chat/drive")) return false;
      return true;
    } catch (_) {
      return false;
    }
  }

  function findLatestChatLink() {
    const historyLabel = Array.from(document.querySelectorAll("div, span"))
      .filter((el) => isVisible(el))
      .find((el) => cleanText(el.innerText || el.textContent || "") === "\u5386\u53f2\u5bf9\u8bdd");

    const historyTop = historyLabel ? historyLabel.getBoundingClientRect().bottom : 0;
    const sidebarRight = Math.min(360, window.innerWidth * 0.35);
    const excludedTexts = new Set([
      "\u4e3b\u5bf9\u8bdd",
      "\u65b0\u5bf9\u8bdd",
      "\u65b0\u529e\u516c\u4efb\u52a1",
      "AI \u521b\u4f5c",
      "\u4e91\u76d8",
      "\u66f4\u591a",
      "\u8c46\u5305"
    ]);

    const chatLinks = Array.from(document.querySelectorAll("a[href]"))
      .filter((a) => {
        if (!isVisible(a)) return false;
        const href = a.getAttribute("href") || "";
        if (!/^\/chat\/\d+/.test(href) && !/^https?:\/\/www\.doubao\.com\/chat\/\d+/.test(href)) return false;
        const rect = a.getBoundingClientRect();
        const text = cleanText(a.innerText || a.textContent || "");
        if (!text || excludedTexts.has(text)) return false;
        return rect.left < sidebarRight && rect.top > historyTop && rect.width >= 80 && rect.height >= 18;
      })
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

    if (chatLinks[0]) return chatLinks[0];

    function clickableAncestor(el) {
      let node = el;
      while (node && node !== document.body) {
        const role = node.getAttribute && node.getAttribute("role");
        const href = node.getAttribute && node.getAttribute("href");
        const style = window.getComputedStyle(node);
        if (href || role === "button" || role === "link" || style.cursor === "pointer") return node;
        node = node.parentElement;
      }
      return el;
    }

    function validHistoryItem(el) {
      if (!isVisible(el)) return false;
      const rect = el.getBoundingClientRect();
      const text = cleanText(el.innerText || el.textContent || "");
      if (!text || text.length > 80) return false;
      if (excludedTexts.has(text)) return false;
      if (HEADER_RE.test(text)) return false;
      if (rect.left >= sidebarRight || rect.top <= historyTop) return false;
      if (rect.height < 18 || rect.width < 80) return false;
      return true;
    }

    const cards = Array.from(document.querySelectorAll("a[href], [role='button'], [role='link']"))
      .filter(validHistoryItem)
      .map((el) => clickableAncestor(el))
      .filter((el, index, arr) => arr.indexOf(el) === index)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

    if (cards[0]) return cards[0];

    const links = Array.from(document.querySelectorAll("a[href]"))
      .filter((a) => {
        if (!isVisible(a)) return false;
        if (!isChatHref(a.getAttribute("href"))) return false;

        const rect = a.getBoundingClientRect();
        const text = cleanText(a.innerText || a.textContent || "");
        if (!text || text.length > 80) return false;
        if (excludedTexts.has(text)) return false;

        return rect.left < sidebarRight && rect.top > historyTop;
      })
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

    return links[0] || null;
  }

  async function openLatestChat() {
    const link = findLatestChatLink();
    if (!link) return false;

    link.scrollIntoView({ block: "center", inline: "nearest" });
    const href = link.getAttribute && link.getAttribute("href");
    if (href && /^\/chat\/\d+/.test(href)) {
      location.href = new URL(href, location.href).href;
      await sleep(2200);
      return true;
    }
    if (href && /^https?:\/\/www\.doubao\.com\/chat\/\d+/.test(href)) {
      location.href = href;
      await sleep(2200);
      return true;
    }
    link.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, pointerType: "mouse" }));
    link.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    link.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, cancelable: true, pointerType: "mouse" }));
    link.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
    link.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    if (typeof link.click === "function") link.click();
    await sleep(2200);
    return true;
  }

  function extractReferences() {
    const expected = expectedReferenceCount();
    const seen = new Set();
    const rows = [];

    referenceAnchors().forEach((a) => {
      const title = cleanText(
        a.innerText ||
        a.textContent ||
        a.getAttribute("aria-label") ||
        a.getAttribute("title") ||
        ""
      );
      const href = new URL(a.getAttribute("href"), location.href).href;

      if (!title || !isReferenceHref(href)) return;
      const key = `${title}|${href}`;
      if (seen.has(key)) return;
      seen.add(key);

      rows.push({
        index: rows.length + 1,
        title,
        href,
        source: "search_query_result a[href]"
      });
    });

    return rows.slice(0, expected).map((item, index) => ({
      ...item,
      index: index + 1
    }));
  }

  async function extractBackendReferences() {
    const conversationId = (location.pathname.match(/\/chat\/(\d+)/) || [])[1];
    let endpoint = performance.getEntriesByType("resource")
      .map((entry) => entry.name)
      .find((url) => url.includes("/im/chain/single?"));
    if (!conversationId) return [];
    if (!endpoint) {
      function storedJson(key) {
        try {
          return JSON.parse(localStorage.getItem(key) || "{}");
        } catch (_) {
          return {};
        }
      }
      const deviceId = storedJson("samantha_web_web_id").web_id || "";
      const teaId = storedJson("__tea_cache_tokens_497858").web_id || deviceId;
      const query = new URLSearchParams({
        version_code: "20800",
        language: "zh",
        device_platform: "web",
        doubao_device_platform: "web",
        aid: "497858",
        real_aid: "497858",
        pkg_type: "release_version",
        device_id: deviceId,
        pc_version: "3.29.6",
        doubao_pc_version: "3.29.6",
        web_id: teaId,
        tea_uuid: teaId,
        region: "CN",
        sys_region: "CN",
        samantha_web: "1",
        web_platform: "browser",
        "use-olympus-account": "1",
        web_tab_id: crypto.randomUUID()
      });
      endpoint = `${location.origin}/im/chain/single?${query}`;
    }

    const request = {
      cmd: 3100,
      uplink_body: {
        pull_singe_chain_uplink_body: {
          conversation_id: conversationId,
          anchor_index: Number.MAX_SAFE_INTEGER,
          conversation_type: 3,
          direction: 1,
          limit: 20,
          ext: {},
          filter: { index_list: [] },
          evaluate_ab_params: "",
          evaluate_common_params: ""
        }
      },
      sequence_id: crypto.randomUUID(),
      channel: 2,
      version: "1"
    };
    const response = await fetch(endpoint, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json; encoding=utf-8",
        "Agw-Js-Conv": "str"
      },
      body: JSON.stringify(request)
    });
    if (!response.ok) return [];

    const payload = await response.json();
    const messages = payload &&
      payload.downlink_body &&
      payload.downlink_body.pull_singe_chain_downlink_body &&
      payload.downlink_body.pull_singe_chain_downlink_body.messages || [];
    const seen = new Set();
    const rows = [];

    messages.forEach((message) => {
      (message.content_block || []).forEach((block) => {
        const searchBlock = block &&
          block.content &&
          block.content.search_query_result_block;
        if (!searchBlock) return;
        (searchBlock.results || []).forEach((result) => {
          const card = Object.values(result || {})
            .find((value) => value && typeof value === "object" && value.url);
          if (!card || !isReferenceHref(card.url)) return;
          const title = cleanText(card.title || card.summary || card.sitename || card.url);
          const href = new URL(card.url, location.href).href;
          const key = `${title}|${href}`;
          if (!title || seen.has(key)) return;
          seen.add(key);
          rows.push({
            index: rows.length + 1,
            title,
            href,
            source: "doubao /im/chain/single"
          });
        });
      });
    });

    const expected = expectedReferenceCount();
    return (expected > 0 ? rows.slice(0, expected) : rows).map((item, index) => ({
      ...item,
      index: index + 1
    }));
  }

  function answerText() {
    const root = findReferenceMessageRoot();
    const clone = (root || document.body).cloneNode(true);
    clone.querySelectorAll("script, style, textarea, input, button, svg").forEach((el) => el.remove());
    // Product extraction must only see the assistant answer. Reference cards are
    // rendered inside the same message root, so remove their links/blocks before
    // reading text; otherwise source titles can be misread as recommended products.
    clone.querySelectorAll("a[href], [data-pluginidentifier*='search_query_result']").forEach((el) => el.remove());
    let text = String(clone.innerText || clone.textContent || "")
      .replace(/\r/g, "\n")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    text = text
      .replace(HEADER_RE, " ")
      .replace(/\u76f8\u5173\u89c6\u9891[\s\S]*$/g, " ")
      .replace(/[ \t]{2,}/g, " ")
      .trim();
    return text;
  }

  async function expandAndExtract() {
    const expected = expectedReferenceCount();

    for (let i = 0; i < 8; i += 1) {
      const rows = extractReferences();
      if (rows.length >= expected) return rows;

      clickReferenceHeader();
      await sleep(700);
    }

    const rows = extractReferences();
    if (rows.length === 0 || (expected > 0 && rows.length < expected)) {
      try {
        const backendRows = await extractBackendReferences();
        if (backendRows.length > rows.length) return backendRows;
      } catch (error) {
        console.warn("Doubao backend reference fallback failed", error);
      }
    }
    return rows;
  }

  function createPanel() {
    if (document.getElementById(PANEL_ID)) return;

    const panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.style.position = "fixed";
    panel.style.right = "18px";
    panel.style.top = "82px";
    panel.style.zIndex = "2147483647";
    panel.style.width = "380px";
    panel.style.maxWidth = "calc(100vw - 36px)";
    panel.style.padding = "10px";
    panel.style.border = "1px solid #d9d9d9";
    panel.style.borderRadius = "8px";
    panel.style.background = "#fff";
    panel.style.boxShadow = "0 10px 28px rgba(0,0,0,.18)";
    panel.style.fontSize = "13px";
    panel.style.color = "#111";
    panel.style.pointerEvents = "auto";

    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.alignItems = "center";
    row.style.justifyContent = "space-between";
    row.style.gap = "8px";
    row.style.marginBottom = "8px";

    const status = document.createElement("div");
    status.id = "__doubao_ref_panel_status";
    status.textContent = "idle";

    const buttonWrap = document.createElement("div");
    buttonWrap.style.display = "flex";
    buttonWrap.style.gap = "6px";

    const grabButton = document.createElement("button");
    grabButton.id = "__doubao_ref_button";
    grabButton.type = "button";
    grabButton.textContent = "grab";
    grabButton.style.border = "0";
    grabButton.style.borderRadius = "6px";
    grabButton.style.padding = "6px 10px";
    grabButton.style.background = "#1677ff";
    grabButton.style.color = "#fff";
    grabButton.style.cursor = "pointer";

    const latestGrabButton = document.createElement("button");
    latestGrabButton.id = LATEST_GRAB_BUTTON_ID;
    latestGrabButton.type = "button";
    latestGrabButton.textContent = "latest+grab";
    latestGrabButton.style.border = "0";
    latestGrabButton.style.borderRadius = "6px";
    latestGrabButton.style.padding = "6px 10px";
    latestGrabButton.style.background = "#0f766e";
    latestGrabButton.style.color = "#fff";
    latestGrabButton.style.cursor = "pointer";

    const copyButton = document.createElement("button");
    copyButton.id = "__doubao_ref_copy";
    copyButton.type = "button";
    copyButton.textContent = "copy";
    copyButton.style.border = "1px solid #d9d9d9";
    copyButton.style.borderRadius = "6px";
    copyButton.style.padding = "6px 10px";
    copyButton.style.background = "#fff";
    copyButton.style.color = "#111";
    copyButton.style.cursor = "pointer";

    const textarea = document.createElement("textarea");
    textarea.id = PANEL_TEXTAREA_ID;
    textarea.value = window.localStorage.getItem(STORAGE_KEY) || "";
    textarea.placeholder = "JSON result";
    textarea.style.width = "100%";
    textarea.style.height = "190px";
    textarea.style.boxSizing = "border-box";
    textarea.style.resize = "vertical";
    textarea.style.border = "1px solid #d9d9d9";
    textarea.style.borderRadius = "6px";
    textarea.style.padding = "8px";
    textarea.style.fontSize = "12px";
    textarea.style.lineHeight = "1.45";

    function stop(event) {
      event.preventDefault();
      event.stopPropagation();
    }

    grabButton.addEventListener("pointerdown", stop, true);
    grabButton.addEventListener("click", (event) => {
      stop(event);
      runExtract();
    }, true);

    latestGrabButton.addEventListener("pointerdown", stop, true);
    latestGrabButton.addEventListener("click", (event) => {
      stop(event);
      runLatestAndExtract();
    }, true);

    copyButton.addEventListener("pointerdown", stop, true);
    copyButton.addEventListener("click", async (event) => {
      stop(event);
      textarea.focus();
      textarea.select();
      try {
        await navigator.clipboard.writeText(textarea.value);
      } catch (_) {
        document.execCommand("copy");
      }
    }, true);

    buttonWrap.appendChild(latestGrabButton);
    buttonWrap.appendChild(grabButton);
    buttonWrap.appendChild(copyButton);
    row.appendChild(status);
    row.appendChild(buttonWrap);
    panel.appendChild(row);
    panel.appendChild(textarea);
    document.documentElement.appendChild(panel);
  }

  function updatePanel(json, payload) {
    createPanel();
    const textarea = document.getElementById(PANEL_TEXTAREA_ID);
    const status = document.getElementById("__doubao_ref_panel_status");
    if (textarea) textarea.value = json;
    if (status) {
      if (payload.status === "running") {
        status.textContent = "running";
      } else {
        status.textContent = payload.ok ? `count: ${payload.count}` : `error: ${payload.error || "unknown"}`;
      }
    }
  }

  function setHiddenState(payload, json) {
    ensureHiddenNode(RESULT_ID, "textarea").value = json;
    ensureHiddenNode(STATUS_ID, "input").value = payload.status || (payload.ok ? "done" : "error");
    ensureHiddenNode(COUNT_ID, "input").value = String(payload.count || 0);
    ensureHiddenNode(COMPLETE_ID, "input").value = payload.complete ? "true" : "false";
    ensureHiddenNode(CHAT_TITLE_ID, "input").value = payload.chatTitle || "";
  }

  function setRunningState() {
    const payload = {
      ok: true,
      status: "running",
      count: 0,
      expectedCount: expectedReferenceCount(),
      complete: false,
      url: location.href,
      chatTitle: currentChatTitle(),
      extractedAt: new Date().toISOString(),
      items: []
    };
    const json = JSON.stringify(payload);
    setHiddenState(payload, json);
    window.localStorage.setItem(STORAGE_KEY, json);
    updatePanel(json, payload);
  }

  function saveResult(rows) {
    const expected = Math.max(expectedReferenceCount(), rows.length);
    const payload = {
      ok: true,
      status: "done",
      count: rows.length,
      expectedCount: expected,
      complete: rows.length >= expected,
      url: location.href,
      title: document.title,
      chatTitle: currentChatTitle(),
      answerText: answerText(),
      extractedAt: new Date().toISOString(),
      items: rows
    };
    const json = JSON.stringify(payload);
    setHiddenState(payload, json);
    window.localStorage.setItem(STORAGE_KEY, json);
    updatePanel(json, payload);
    window.dispatchEvent(new CustomEvent("DOUBAO_REFS_READY", { detail: payload }));
    return payload;
  }

  async function runExtract() {
    setRunningState();

    try {
      const rows = await expandAndExtract();
      return saveResult(rows);
    } catch (error) {
      const payload = {
        ok: false,
        status: "error",
        count: 0,
        expectedCount: expectedReferenceCount(),
        complete: false,
        error: String(error && error.message ? error.message : error),
        url: location.href,
        chatTitle: currentChatTitle(),
        extractedAt: new Date().toISOString(),
        items: []
      };
      const json = JSON.stringify(payload);
      setHiddenState(payload, json);
      window.localStorage.setItem(STORAGE_KEY, json);
      updatePanel(json, payload);
      return payload;
    }
  }

  async function runLatestAndExtract() {
    setRunningState();

    try {
      const opened = await openLatestChat();
      if (!opened) throw new Error("cannot find latest history chat");
      await sleep(800);
      const rows = await expandAndExtract();
      return saveResult(rows);
    } catch (error) {
      const payload = {
        ok: false,
        status: "error",
        count: 0,
        expectedCount: expectedReferenceCount(),
        complete: false,
        error: String(error && error.message ? error.message : error),
        url: location.href,
        chatTitle: currentChatTitle(),
        extractedAt: new Date().toISOString(),
        items: []
      };
      const json = JSON.stringify(payload);
      setHiddenState(payload, json);
      window.localStorage.setItem(STORAGE_KEY, json);
      updatePanel(json, payload);
      return payload;
    }
  }

  let lastCommand = "";
  window.setInterval(() => {
    const command = window.localStorage.getItem(COMMAND_KEY) || "";
    if (command && command !== lastCommand) {
      lastCommand = command;
      runExtract();
    }
  }, 300);

  window.addEventListener("message", (event) => {
    if (event && event.data && event.data.type === "DOUBAO_EXTRACT_REFS") {
      runExtract();
    }
  });

  ensureHiddenNode(RESULT_ID, "textarea");
  ensureHiddenNode(STATUS_ID, "input").value = "idle";
  ensureHiddenNode(COUNT_ID, "input").value = "0";
  ensureHiddenNode(COMPLETE_ID, "input").value = "false";
  ensureHiddenNode(CHAT_TITLE_ID, "input").value = "";
  createPanel();
})();
