(function () {
  const EXTENSION_BUILD = "1.8.24";
  if (window.__geoMonitorExtensionLoaded === EXTENSION_BUILD) return;
  document.getElementById("__geo_monitor_control_panel")?.remove();
  window.__geoMonitorExtensionLoaded = EXTENSION_BUILD;

  const SITES = {
    doubao: {
      hosts: ["doubao.com"],
      inputs: ["textarea[data-testid='chat_input_input']", "[data-testid='chat_input'] textarea", "[data-chat-input] textarea", "[contenteditable='true'][role='textbox']", "textarea:not([disabled])", "[contenteditable='true']"],
      sendButtons: ["button[data-testid='chat_input_send_button']", "[data-testid='chat_input_send_button']"],
      answers: ["[data-message-id]", "[data-testid*='message']", "[class*='message'] [class*='markdown']", "main [class*='markdown']"],
      newChats: ["[data-testid='create_conversation_button']", "[aria-label='新对话']", "[aria-label='创建新对话']", "[data-testid*='new-chat']", "[class*='new-chat']", "a[href='/chat']", "a[href='/chat/']"],
      freshPath: "/chat/",
      loginMarkers: ["扫码登录", "验证码登录", "登录后使用"],
      internalHosts: ["doubao.com", "byteimg.com", "bytedance.com"]
    },
    yuanbao: {
      hosts: ["yuanbao.tencent.com"],
      inputs: ["#chat-content textarea:not([disabled])", "#chat-input textarea:not([disabled])", "main textarea:not([disabled])", "[data-testid*='input'] textarea:not([disabled])", "[class*='chat-input'] textarea:not([disabled])", "[class*='input-box'] textarea:not([disabled])", "[contenteditable='true'][role='textbox']", "[class*='editor'][contenteditable='true']", "textarea:not([disabled])", "[contenteditable='true']"],
      sendButtons: ["#chat-content [aria-label*='发送']", "main [aria-label*='发送']", "[data-testid*='send']", "[class*='send-btn']", "[class*='send-button']", "[class*='sendButton']", "[class*='chat-input'] button", "[class*='input-box'] button", "[class*='send']"],
      answers: ["#chat-content [data-role='assistant']", "#chat-content [class*='agent-message']", "#chat-content [class*='agent']", "#chat-content [class*='markdown']", "#chat-content [class*='hyc-content']", "#chat-content [class*='answer']", "#chat-content [class*='message']"],
      newChats: ["[data-desc='new-chat']", "#app_chat_new_chat_button [data-desc='new-chat']", "#app_chat_new_chat_button button", "[aria-label='新建对话']", "[class*='new-chat']", "[class*='create-chat']", "[class*='add-chat']"],
      freshPath: "/",
      loginMarkers: ["请登录后输入内容", "请使用微信扫描二维码登录", "未登录", "Not logged in", "Log In with WeChat", "Please scan the QR code"],
      internalHosts: ["yuanbao.tencent.com", "tencent.com", "qq.com"]
    },
    wenxin: {
      hosts: ["wenxin.baidu.com"],
      inputs: ["#chat-input-box:not([disabled])", "#chat-textarea:not([disabled])", "textarea:not([disabled])", "[contenteditable='true'][role='textbox']", "[contenteditable='true']"],
      sendButtons: [".cs-input-ds-send-btn", "[data-module='send']", "button[aria-label*='发送']", "[class*='send'] button", "button[class*='send']"],
      answers: [".cs-rank-container.last .ai-entry", ".cs-rank-container.last", "#conversation-flow-content .cs-rank-container", ".ai-entry-block", "main [class*='answer']", "main [class*='markdown']", "[class*='chat-message']", "[class*='message'] [class*='content']"],
      newChats: [".new-dialog-container-button-sample", ".new-dialog-icon", ".fold-aside-new-dialog-container", "[class*='new-dialog']", "[class*='new-chat']", "[class*='new-conversation']", "[class*='create-chat']"],
      freshPath: "/",
      loginMarkers: ["扫码登录", "手机号登录", "登录后使用"],
      internalHosts: ["baidu.com"]
    }
  };

  const PANEL_ID = "__geo_monitor_control_panel";
  let adaptiveFingerprints = {};
  const adaptiveSaved = new Map();

  const adaptiveReady = chrome.runtime.sendMessage({ type: "GEO_GET_ADAPTIVE_ELEMENTS" }).then((value) => {
    adaptiveFingerprints = value?.elements || {};
  }).catch(() => {});

  function modelId() {
    const host = location.hostname.replace(/^www\./, "").toLowerCase();
    return Object.keys(SITES).find((id) => SITES[id].hosts.some((item) => host === item || host.endsWith("." + item))) || "";
  }

  const MODEL = modelId();
  const SITE = SITES[MODEL];

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function visible(element) {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 8 && rect.height > 8 && style.display !== "none" && style.visibility !== "hidden";
  }

  function adaptiveKey(identifier) {
    return `${MODEL}:${identifier}`;
  }

  function normalized(value) {
    return String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, " ").trim();
  }

  function stringSimilarity(leftValue, rightValue) {
    const left = normalized(leftValue);
    const right = normalized(rightValue);
    if (!left && !right) return 1;
    if (!left || !right) return 0;
    if (left === right) return 1;
    if (left.includes(right) || right.includes(left)) return Math.min(left.length, right.length) / Math.max(left.length, right.length);
    if (left.length < 2 || right.length < 2) return 0;
    const pairs = new Map();
    for (let index = 0; index < left.length - 1; index += 1) {
      const pair = left.slice(index, index + 2);
      pairs.set(pair, (pairs.get(pair) || 0) + 1);
    }
    let overlap = 0;
    for (let index = 0; index < right.length - 1; index += 1) {
      const pair = right.slice(index, index + 2);
      const count = pairs.get(pair) || 0;
      if (count) {
        overlap += 1;
        pairs.set(pair, count - 1);
      }
    }
    return (2 * overlap) / (left.length + right.length - 2);
  }

  function adaptiveAttributes(element) {
    const result = {};
    for (const name of ["role", "aria-label", "title", "placeholder", "contenteditable", "data-desc", "data-testid", "id", "class", "href"]) {
      let value = element?.getAttribute?.(name) || "";
      if (name === "class") value = value.split(/\s+/).filter(Boolean).slice(0, 10).sort().join(" ");
      if (value) result[name] = String(value).slice(0, 240);
    }
    return result;
  }

  function adaptivePath(element) {
    const path = [];
    for (let node = element; node && node !== document.documentElement && path.length < 7; node = node.parentElement) {
      path.unshift(node.tagName.toLowerCase());
    }
    return path;
  }

  function elementFingerprint(element) {
    const parent = element?.parentElement;
    return {
      tag: element?.tagName?.toLowerCase() || "",
      text: controlLabel(element).slice(0, 180),
      attributes: adaptiveAttributes(element),
      path: adaptivePath(element),
      parentTag: parent?.tagName?.toLowerCase() || "",
      parentAttributes: adaptiveAttributes(parent),
      parentText: normalized(parent?.innerText || "").slice(0, 180),
      siblings: parent ? [...parent.children].filter((node) => node !== element).slice(0, 12).map((node) => node.tagName.toLowerCase()) : []
    };
  }

  function objectSimilarity(left = {}, right = {}) {
    const keys = [...new Set([...Object.keys(left), ...Object.keys(right)])];
    if (!keys.length) return 1;
    return keys.reduce((sum, key) => sum + (key in left && key in right ? stringSimilarity(left[key], right[key]) : 0), 0) / keys.length;
  }

  function adaptiveScore(original, element) {
    const current = elementFingerprint(element);
    return (
      (original.tag === current.tag ? 0.14 : 0) +
      stringSimilarity(original.text, current.text) * 0.22 +
      objectSimilarity(original.attributes, current.attributes) * 0.20 +
      stringSimilarity((original.path || []).join("/"), current.path.join("/")) * 0.12 +
      stringSimilarity(original.parentTag, current.parentTag) * 0.08 +
      objectSimilarity(original.parentAttributes, current.parentAttributes) * 0.10 +
      stringSimilarity(original.parentText, current.parentText) * 0.07 +
      stringSimilarity((original.siblings || []).join("/"), current.siblings.join("/")) * 0.07
    );
  }

  function rememberAdaptive(identifier, element) {
    if (!element || element.closest?.(`#${PANEL_ID}`)) return;
    const key = adaptiveKey(identifier);
    const fingerprint = elementFingerprint(element);
    if (identifier === "answer-body") {
      fingerprint.text = "";
      fingerprint.parentText = "";
    }
    const serialized = JSON.stringify(fingerprint);
    if (adaptiveSaved.get(key) === serialized) return;
    adaptiveSaved.set(key, serialized);
    adaptiveReady.then(() => {
      adaptiveFingerprints = { ...adaptiveFingerprints, [key]: fingerprint };
      return chrome.runtime.sendMessage({
        type: "GEO_SAVE_ADAPTIVE_ELEMENT",
        model: MODEL,
        identifier,
        fingerprint
      });
    }).catch(() => {});
  }

  function relocateAdaptive(identifier, candidateSelector, threshold = 0.58) {
    const fingerprint = adaptiveFingerprints[adaptiveKey(identifier)];
    if (!fingerprint) return null;
    let best = null;
    let bestScore = 0;
    for (const element of [...document.querySelectorAll(candidateSelector)].slice(0, 1200)) {
      if (!visible(element) || element.closest?.(`#${PANEL_ID}`) || element.disabled || element.getAttribute("aria-disabled") === "true") continue;
      const score = adaptiveScore(fingerprint, element);
      if (score > bestScore) {
        best = element;
        bestScore = score;
      }
    }
    if (!best || bestScore < threshold) return null;
    rememberAdaptive(identifier, best);
    return best;
  }

  function inputValue(element) {
    return normalized(element?.isContentEditable ? element.innerText || element.textContent : element?.value);
  }

  function inputScore(element, selectorIndex) {
    const rect = element.getBoundingClientRect();
    const label = normalized(`${element.getAttribute("placeholder") || ""} ${element.getAttribute("aria-label") || ""} ${element.getAttribute("data-testid") || ""}`);
    let score = Math.max(0, 70 - selectorIndex * 4);
    score += Math.min(45, rect.width / 20);
    score += Math.max(0, Math.min(35, rect.top / Math.max(1, innerHeight) * 35));
    if (element.tagName === "TEXTAREA") score += 24;
    if (element.getAttribute("role") === "textbox") score += 18;
    if (element.isContentEditable) score += 12;
    if (/输入|消息|提问|问问|发消息|chat|prompt/.test(label)) score += 35;
    if (element.closest("main,#chat-content,[data-testid='chat_input'],[data-chat-input]")) score += 30;
    if (element.closest("nav,aside,[class*='sidebar'],[data-history-container]")) score -= 120;
    return score;
  }

  function findInput() {
    if (!SITE) return null;
    const ranked = [];
    const seen = new Set();
    SITE.inputs.forEach((selector, selectorIndex) => {
      for (const element of document.querySelectorAll(selector)) {
        if (seen.has(element) || element.closest?.(`#${PANEL_ID}`)) continue;
        seen.add(element);
        if (!visible(element) || element.disabled || element.readOnly || element.getAttribute("aria-disabled") === "true") continue;
        ranked.push({ element, score: inputScore(element, selectorIndex) });
      }
    });
    ranked.sort((left, right) => right.score - left.score);
    if (ranked[0]?.element) {
      rememberAdaptive("question-input", ranked[0].element);
      return ranked[0].element;
    }
    return relocateAdaptive("question-input", "textarea,input,[contenteditable],[role='textbox'],[data-testid],[aria-label]", 0.60);
  }

  function pageText() {
    let text = String(document.body?.innerText || "");
    const panel = document.getElementById(PANEL_ID);
    if (panel) text = text.replace(String(panel.innerText || ""), "");
    return text;
  }

  function verificationChallenge(textValue = pageText()) {
    const text = String(textValue || "");
    const patterns = [
      /请选择所有符合[^\n]{0,80}的图片/,
      /拖拽到下方/,
      /请完成(?:下列)?验证/,
      /人机验证/,
      /安全验证/,
      /拖动滑块/,
      /verify you are human/i,
      /captcha/i
    ];
    const matched = patterns.find((pattern) => pattern.test(text));
    return {
      detected: Boolean(matched),
      message: matched ? "检测到网页安全验证" : ""
    };
  }

  function checkReady() {
    const input = findInput();
    const text = pageText();
    const challenge = verificationChallenge(text);
    const marker = SITE?.loginMarkers.find((item) => text.includes(item)) || "";
    return {
      ok: Boolean(SITE && input && !marker && !challenge.detected),
      model: MODEL,
      url: location.href,
      title: document.title,
      challenge,
      message: !SITE ? "当前页面不是受支持的模型" : challenge.detected ? `GEO_CHALLENGE_REQUIRED：${challenge.message}` : marker ? `检测到未登录：${marker}` : input ? `网页已登录并找到输入框（插件 ${EXTENSION_BUILD}）` : "未找到可用输入框"
    };
  }

  function answerNodes(question = "") {
    const nodes = [];
    const questionLinked = new Set();
    const questionText = normalized(question);
    if (questionText) {
      const roots = MODEL === "yuanbao"
        ? [document.querySelector("#chat-content"), document.querySelector("main")]
        : MODEL === "wenxin"
          ? [document.querySelector("#conversation-flow-content"), document.querySelector("main")]
          : [document.querySelector("main")];
      for (const root of roots.filter(Boolean)) {
        for (const element of root.querySelectorAll("div,section,article,p,[data-message-id],[data-testid],[role]")) {
          const text = normalized(element.innerText);
          if (!text || text.length > questionText.length + 24 || !text.includes(questionText)) continue;
          let current = element;
          for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
            let sibling = current.nextElementSibling;
            for (let offset = 0; sibling && offset < 3; offset += 1, sibling = sibling.nextElementSibling) {
              if (visible(sibling) && normalized(sibling.innerText).length >= 40) {
                nodes.push(sibling);
                questionLinked.add(sibling);
              }
            }
          }
        }
      }
    }
    for (const selector of SITE.answers) nodes.push(...document.querySelectorAll(selector));
    let activeInput = null;
    try { activeInput = findInput(); } catch (_) {}
    const found = [...new Set(nodes)].filter((element) => {
      const text = String(element.innerText || "").trim();
      if (!visible(element) || text.length < 20) return false;
      if (activeInput && (element === activeInput || element.contains(activeInput))) return false;
      return true;
    });
    if (found.length) {
      const ranked = found.map((element, index) => {
        const text = String(element.innerText || "").replace(/\s+/g, " ").trim();
        const compact = normalized(text);
        const attributes = normalized(`${element.className || ""} ${element.getAttribute("data-role") || ""} ${element.getAttribute("data-testid") || ""} ${element.getAttribute("role") || ""}`);
        let score = Math.min(text.length, 12000) + index;
        if (questionLinked.has(element)) score += 8000;
        if (/assistant|agent|answer|markdown|ai-entry|ai_entry|bot/.test(attributes)) score += 3000;
        if (questionText && (compact === questionText || (compact.includes(questionText) && text.length <= question.length + 40))) score -= 20000;
        if (/今天能帮你做什么|下载元宝电脑版|体验高效\s*ai\s*助手/.test(text)) score -= 20000;
        if (/Instant\s+AI Image\s+Writing\s+Solve\s+Deep Research|Presentation Maker\s+Data Insights\s+Investment Analysis/i.test(text)) score -= 20000;
        if (text.length > 16000) score -= text.length;
        return { element, score };
      }).sort((left, right) => right.score - left.score);
      const selected = ranked[0]?.score > 0 ? ranked[0].element : found[found.length - 1];
      rememberAdaptive("answer-body", selected);
      return [selected];
    }
    const relocated = relocateAdaptive("answer-body", "main article,main section,main [data-message-id],main [class],main [role]", 0.64);
    return relocated && String(relocated.innerText || "").trim().length >= 20 ? [relocated] : [];
  }

  function conversationState() {
    const nodes = answerNodes();
    const last = nodes[nodes.length - 1] || null;
    return {
      url: location.href,
      nodeCount: nodes.length,
      lastBody: String(last?.innerText || "").replace(/\s+/g, " ").trim().slice(-1000)
    };
  }

  function controlLabel(element) {
    return `${element?.innerText || ""} ${element?.getAttribute?.("aria-label") || ""} ${element?.getAttribute?.("title") || ""}`
      .replace(/\s+/g, " ").trim();
  }

  function clickableControl(element) {
    if (!element) return null;
    if (element.matches("button,a,[role='button']")) return element;
    return element.querySelector("button,a,[role='button']")
      || element.closest("button,a,[role='button']") || element;
  }

  function newConversationControl() {
    const candidates = [];
    for (const selector of SITE.newChats || []) {
      for (const element of document.querySelectorAll(selector)) {
        candidates.push({ element: clickableControl(element), official: true });
      }
    }
    for (const element of document.querySelectorAll("button,a,[role='button'],[aria-label],[title]")) {
      const label = controlLabel(element);
      if (/(新对话|新建对话|开始新对话|开启新对话|发起新对话|创建新对话|新会话|New\s*Chat)/i.test(label)) {
        candidates.push({ element: clickableControl(element), official: false });
      }
    }
    const seen = new Set();
    const unique = candidates.filter(({ element }) => {
      if (!element || seen.has(element)) return false;
      seen.add(element);
      if (element.closest(`#${PANEL_ID}`)) return false;
      return !element.disabled && element.getAttribute("aria-disabled") !== "true";
    });
    const labeled = unique.filter(({ element }) =>
      /(新对话|新建对话|开始新对话|开启新对话|发起新对话|创建新对话|新会话|New\s*Chat)/i.test(controlLabel(element))
    );
    const selected = labeled.find(({ element }) => visible(element))
      || unique.find(({ element }) => visible(element))
      || labeled[0] || unique[0] || null;
    if (selected) {
      rememberAdaptive("new-conversation", selected.element);
      return selected;
    }
    const relocated = relocateAdaptive("new-conversation", "button,a,[role='button'],[aria-label],[title],[data-desc],[data-testid],[tabindex]", 0.58);
    return relocated ? { element: clickableControl(relocated), official: false, adaptive: true } : null;
  }

  function startNewConversation(sendResponse) {
    const selected = newConversationControl();
    const before = conversationState();
    if (!selected) {
      const targetUrl = new URL(SITE.freshPath || "/", location.origin).href;
      sendResponse({ ok: true, model: MODEL, before, targetUrl, action: "official-route-fallback" });
      window.setTimeout(() => {
        if (location.href === targetUrl) location.reload();
        else location.assign(targetUrl);
      }, 80);
      return;
    }
    const control = selected.element;
    sendResponse({ ok: true, model: MODEL, before, action: selected.adaptive ? "adaptive-control" : selected.official ? "official-control" : "labeled-control" });
    window.setTimeout(() => {
      if (visible(control)) control.scrollIntoView({ block: "center" });
      if (control instanceof HTMLAnchorElement && control.href) {
        const destination = new URL(control.href, location.href);
        if (destination.origin === location.origin) {
          location.assign(destination.href);
          return;
        }
      }
      control.click();
    }, 80);
  }

  function forceNewConversation(sendResponse) {
    const targetUrl = new URL(SITE.freshPath || "/", location.origin).href;
    const before = conversationState();
    sendResponse({ ok: true, model: MODEL, before, targetUrl, action: "forced-official-route" });
    window.setTimeout(() => {
      if (location.href === targetUrl) location.reload();
      else location.assign(targetUrl);
    }, 80);
  }

  function unwrapSourceUrl(rawValue) {
    try {
      let url = new URL(String(rawValue || ""), location.href);
      const host = url.hostname.replace(/^www\./, "").toLowerCase();
      const internal = SITE.internalHosts.some((item) => host === item || host.endsWith("." + item));
      if (internal) {
        for (const key of ["url", "target", "redirect", "redirect_url", "u", "href", "dest"]) {
          const nested = url.searchParams.get(key);
          if (!nested) continue;
          try {
            const decoded = decodeURIComponent(nested);
            const candidate = new URL(decoded, location.href);
            if (/^https?:$/.test(candidate.protocol)) {
              url = candidate;
              break;
            }
          } catch (_) {}
        }
      }
      if (!/^https?:$/.test(url.protocol)) return null;
      const finalHost = url.hostname.replace(/^www\./, "").toLowerCase();
      if (SITE.internalHosts.some((item) => finalHost === item || finalHost.endsWith("." + item))) return null;
      url.hash = "";
      return url;
    } catch (_) {
      return null;
    }
  }

  function answerRoots(node) {
    if (!node) return [document.querySelector("main") || document];
    const roots = [node];
    const answerLength = String(node.innerText || "").length;
    let parent = node.parentElement;
    for (let depth = 0; parent && depth < 4; depth += 1, parent = parent.parentElement) {
      if (parent === document.body || parent === document.documentElement || parent.tagName === "MAIN") break;
      const parentLength = String(parent.innerText || "").length;
      if (parentLength <= Math.max(8000, answerLength * 6)) roots.push(parent);
    }
    return [...new Set(roots)];
  }

  function externalSources(root) {
    const seen = new Set();
    const sources = [];
    for (const scope of answerRoots(root)) {
      const candidates = scope.querySelectorAll("a[href],[data-url],[data-href],[data-source-url]");
      for (const element of candidates) {
        const rawUrl = element.href || element.getAttribute("data-url") || element.getAttribute("data-href") || element.getAttribute("data-source-url") || "";
        const url = unwrapSourceUrl(rawUrl);
        if (!url || seen.has(url.href)) continue;
        const title = String(element.innerText || element.title || element.getAttribute("aria-label") || url.hostname).replace(/\s+/g, " ").trim();
        seen.add(url.href);
        sources.push({ title: title || url.hostname, url: url.href, href: url.href });
      }
    }
    return sources;
  }

  function citationMarkerCount(root) {
    const markers = new Set();
    for (const scope of answerRoots(root)) {
      for (const element of scope.querySelectorAll("[class*='citation'],[class*='reference'],[class*='source'],[data-citation],[data-reference]")) {
        if (visible(element)) markers.add(element);
      }
    }
    return markers.size;
  }

  function declaredSourceCount(body) {
    const text = String(body || "");
    const match = text.match(/(?:共\s*)?(?:参考|引用)\s*(\d{1,3})\s*篇(?:资料|来源|信源)?/);
    return match ? Number(match[1]) : 0;
  }

  function conversationTail(question) {
    if (!question) return "";
    const roots = MODEL === "yuanbao"
      ? [document.querySelector("#chat-content"), document.querySelector("main"), document.querySelector("[role='main']"), document.body]
      : MODEL === "wenxin"
        ? [document.querySelector("#conversation-flow-content"), document.querySelector("main"), document.querySelector("[role='main']")]
        : [document.querySelector("main"), document.querySelector("[role='main']")];
    const panel = document.getElementById(PANEL_ID);
    let best = "";
    for (const root of [...new Set(roots.filter(Boolean))]) {
      let text = String(root.innerText || "").replace(/\r/g, "");
      if (panel && root.contains(panel)) text = text.replace(String(panel.innerText || "").replace(/\r/g, ""), "");
      const index = text.lastIndexOf(question);
      if (index < 0) continue;
      let tail = text.slice(index + question.length).trim();
      tail = tail.replace(/\n(?:Send Message|发消息或按住空格说话|询问AI任何问题)[\s\S]*$/i, "").trim();
      if (/^Instant\s+AI Image\s+Writing\s+Solve\s+Deep Research/i.test(tail)) continue;
      if (tail.length >= 60 && tail.length > best.length) best = tail;
    }
    return best;
  }

  function snapshot(question = "") {
    const nodes = answerNodes(question);
    const node = nodes[nodes.length - 1] || null;
    const nodeBody = String(node?.innerText || "").replace(/\n{3,}/g, "\n\n").trim();
    const tailBody = conversationTail(question);
    const body = tailBody.length > nodeBody.length ? tailBody : nodeBody;
    const text = pageText();
    const challenge = verificationChallenge(text);
    const sourceRoot = MODEL === "yuanbao" && tailBody.length > nodeBody.length ? document.body : node;
    const sources = externalSources(sourceRoot);
    const citationCount = citationMarkerCount(sourceRoot);
    const busySelectors = [
      "[data-module='stop']",
      "[data-testid*='stop_generation']",
      "button[aria-label*='停止']",
      "button[title*='停止']",
      "button[aria-label*='Stop']",
      "button[title*='Stop']"
    ];
    const busy = busySelectors.some((selector) =>
      [...document.querySelectorAll(selector)].some((element) => visible(element))
    );
    return {
      body,
      nodeCount: nodes.length,
      sources,
      citationCount,
      expected_source_count: declaredSourceCount(body) || Math.max(sources.length, citationCount),
      challenge,
      busy,
      pageText: text.slice(-5000),
      questionSeen: Boolean(question && text.includes(question.slice(0, Math.min(30, question.length)))),
      url: location.href
    };
  }

  function replaceInput(input, question) {
    input.scrollIntoView({ block: "center" });
    input.focus();
    if (input.isContentEditable) {
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(input);
      selection.removeAllRanges();
      selection.addRange(range);
      input.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: question }));
      const inserted = document.execCommand("insertText", false, question);
      if (!inserted || !inputValue(input).includes(normalized(question))) input.textContent = question;
    } else {
      const setter = Object.getOwnPropertyDescriptor(input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, "value")?.set;
      if (setter) setter.call(input, question);
      else input.value = question;
    }
    input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: question }));
    input.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: question }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return inputValue(input);
  }

  function sendButtonScore(button, input, selectorIndex = 20) {
    const rect = button.getBoundingClientRect();
    const inputRect = input.getBoundingClientRect();
    const label = normalized(`${button.innerText || ""} ${button.getAttribute("aria-label") || ""} ${button.getAttribute("title") || ""} ${button.getAttribute("data-testid") || ""} ${button.className || ""}`);
    let score = Math.max(0, 80 - selectorIndex * 4);
    if (/发送|send|submit|提问|chat_input_send_button/.test(label)) score += 120;
    if (/语音|voice|麦克风|microphone|上传|upload|附件|add|模型|mode/.test(label)) score -= 140;
    if (/停止|stop|中止|cancel/.test(label)) score -= 220;
    if (rect.width >= 24 && rect.width <= 72 && rect.height >= 24 && rect.height <= 72) score += 25;
    if (rect.right >= inputRect.right - 160 && rect.left <= inputRect.right + 90) score += 35;
    if (rect.top >= inputRect.top - 60 && rect.top <= inputRect.bottom + 100) score += 35;
    score += Math.max(0, 55 - Math.abs(inputRect.right - rect.right) / 3);
    const container = input.closest("form,[data-testid='chat_input'],[data-chat-input],[class*='chat-input'],[class*='input-box']");
    if (container?.contains(button)) score += 55;
    return score;
  }

  function findSendButton(input) {
    const labels = /发送|send|submit|提问/i;
    const ranked = [];
    const seen = new Set();
    (SITE.sendButtons || []).forEach((selector, selectorIndex) => {
      for (const element of document.querySelectorAll(selector)) {
        const button = clickableControl(element);
        if (!button || seen.has(button)) continue;
        seen.add(button);
        if (!visible(button) || button.closest(`#${PANEL_ID}`)) continue;
        ranked.push({ button, score: sendButtonScore(button, input, selectorIndex) });
      }
    });
    for (const button of document.querySelectorAll("button,[role='button']")) {
      if (seen.has(button) || button.closest(`#${PANEL_ID}`) || !visible(button)) continue;
      const label = `${button.innerText || ""} ${button.getAttribute("aria-label") || ""} ${button.getAttribute("title") || ""}`;
      const score = sendButtonScore(button, input, 20);
      if (labels.test(label) || score >= 70) ranked.push({ button, score });
    }
    ranked.sort((left, right) => right.score - left.score);
    return ranked[0]?.button || null;
  }

  function pressEnter(input) {
    const options = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
    input.dispatchEvent(new KeyboardEvent("keydown", options));
    input.dispatchEvent(new KeyboardEvent("keypress", options));
    input.dispatchEvent(new KeyboardEvent("keyup", options));
  }

  function questionAppearedOutsideInput(input, question) {
    const probe = normalized(question).slice(0, 30);
    if (!probe) return false;
    const root = MODEL === "yuanbao"
      ? document.querySelector("#chat-content") || document.querySelector("main")
      : MODEL === "wenxin"
        ? document.querySelector("#conversation-flow-content") || document.querySelector("main")
        : document.querySelector("main");
    if (!root) return false;
    for (const element of root.querySelectorAll("[data-message-id],[data-testid*='message'],[class*='message'],[class*='bubble'],[class*='query'],.cs-rank-container,.ai-entry")) {
      if (element === input || element.contains(input) || !visible(element)) continue;
      if (element.closest("aside,nav,[class*='sidebar'],[class*='history']")) continue;
      if (normalized(element.innerText).includes(probe)) return true;
    }
    return false;
  }

  async function submitQuestion(input, question, baseline) {
    const deadline = Date.now() + 6000;
    let button = null;
    while (Date.now() < deadline) {
      const challenge = verificationChallenge();
      if (challenge.detected) {
        throw new Error(`GEO_CHALLENGE_REQUIRED：${MODEL}检测到网页安全验证`);
      }
      button = findSendButton(input);
      if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") break;
      await sleep(250);
    }
    let method = "enter";
    if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") {
      method = `button:${button.getAttribute("data-testid") || button.getAttribute("aria-label") || button.tagName.toLowerCase()}`;
      button.click();
    } else {
      pressEnter(input);
    }
    const verifyDeadline = Date.now() + 6000;
    while (Date.now() < verifyDeadline) {
      await sleep(350);
      const current = snapshot(question);
      if (current.challenge?.detected) {
        throw new Error(`GEO_CHALLENGE_REQUIRED：${MODEL}检测到网页安全验证`);
      }
      const submitted = !inputValue(input)
        || current.busy
        || current.url !== baseline.url
        || current.nodeCount > baseline.nodeCount
        || questionAppearedOutsideInput(input, question);
      if (submitted) return method;
    }
    if (method !== "enter") {
      pressEnter(input);
      await sleep(900);
      const current = snapshot(question);
      if (current.challenge?.detected) {
        throw new Error(`GEO_CHALLENGE_REQUIRED：${MODEL}检测到网页安全验证`);
      }
      if (!inputValue(input) || questionAppearedOutsideInput(input, question) || current.busy) return `${method}+enter`;
    }
    const inputLabel = input.getAttribute("data-testid") || input.getAttribute("aria-label") || input.tagName.toLowerCase();
    throw new Error(`发送动作未生效：${MODEL} 输入框=${inputLabel}，发送方式=${method}`);
  }

  async function waitForAnswer(question, baseline, timeoutMs = 240000, stableMs = 6000) {
    const deadline = Date.now() + Math.max(30000, timeoutMs);
    let previous = "";
    let stableSince = 0;
    while (Date.now() < deadline) {
      await sleep(1200);
      const challenge = verificationChallenge();
      if (challenge.detected) {
        throw new Error(`GEO_CHALLENGE_REQUIRED：${MODEL}检测到网页安全验证`);
      }
      const current = snapshot(question);
      const changed = current.body && (current.body !== baseline.body || current.nodeCount > baseline.nodeCount);
      const questionSeen = current.questionSeen || current.pageText.includes(question.slice(0, Math.min(30, question.length)));
      if (changed && questionSeen && !current.busy) {
        if (current.body === previous) {
          stableSince = stableSince || Date.now();
          if (Date.now() - stableSince >= stableMs) {
            return {
              ok: true,
              model: MODEL,
              body: current.body,
              sources: current.sources,
              url: current.url,
              capture_mode: "chrome_extension_logged_in_tab",
              expected_source_count: declaredSourceCount(current.body) || Math.max(current.sources.length, current.citationCount),
              source_capture_complete: (declaredSourceCount(current.body) || current.citationCount) <= current.sources.length
            };
          }
        } else {
          stableSince = Date.now();
        }
        previous = current.body;
      } else {
        stableSince = 0;
      }
    }
    throw new Error(`回答等待超时：当前正文 ${previous.length} 字`);
  }

  async function runQuestion(question, timeoutMs = 240000, stableMs = 6000) {
    const ready = checkReady();
    if (!ready.ok) throw new Error(ready.message);
    const input = findInput();
    const baseline = snapshot();
    const filled = replaceInput(input, question);
    await sleep(450);
    if (!filled.includes(normalized(question))) throw new Error(`${MODEL} 输入框未成功写入问题`);
    await submitQuestion(input, question, baseline);
    return waitForAnswer(question, baseline, timeoutMs, stableMs);
  }

  let panelRestoreTimer = 0;

  function restorePanelAfterTrustedInput() {
    window.clearTimeout(panelRestoreTimer);
    const panel = document.getElementById(PANEL_ID);
    if (panel) panel.style.visibility = "";
  }

  function prepareTrustedQuestion() {
    const ready = checkReady();
    if (!ready.ok) throw new Error(ready.message);
    const panel = document.getElementById(PANEL_ID);
    if (panel) panel.style.visibility = "hidden";
    window.clearTimeout(panelRestoreTimer);
    panelRestoreTimer = window.setTimeout(restorePanelAfterTrustedInput, 15000);
    const input = findInput();
    input.scrollIntoView({ block: "center" });
    input.focus();
    const rect = input.getBoundingClientRect();
    const current = snapshot();
    return {
      ok: true,
      model: MODEL,
      baseline: { body: current.body, nodeCount: current.nodeCount, url: current.url },
      target: {
        x: Math.max(rect.left + 12, Math.min(rect.right - 12, rect.left + Math.min(80, rect.width / 2))),
        y: Math.max(rect.top + 12, Math.min(rect.bottom - 12, rect.top + rect.height / 2))
      },
      inputLabel: input.getAttribute("data-testid") || input.id || input.getAttribute("aria-label") || input.tagName.toLowerCase()
    };
  }

  function verifyTrustedInput(question) {
    const input = findInput();
    const value = inputValue(input);
    return { ok: value.includes(normalized(question)), length: value.length };
  }

  function fillQuestionFallback(question) {
    const input = findInput();
    const value = replaceInput(input, question);
    return { ok: value.includes(normalized(question)), length: value.length, model: MODEL };
  }

  function trustedSendTarget() {
    const input = findInput();
    const button = findSendButton(input);
    if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") {
      button.scrollIntoView({ block: "center", inline: "center" });
      const rect = button.getBoundingClientRect();
      if (rect.width >= 2 && rect.height >= 2) {
        return {
          ok: true,
          model: MODEL,
          target: { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 },
          label: button.getAttribute("data-testid") || button.getAttribute("aria-label") || String(button.className || "").slice(0, 100) || button.tagName.toLowerCase()
        };
      }
    }
    const composer = input.closest("form,[data-testid='chat_input'],[class*='chat-input'],[class*='input-box'],[class*='input-area'],[class*='editor-container']") || input.parentElement;
    const rect = composer?.getBoundingClientRect?.();
    if (!rect || rect.width < 80 || rect.height < 32) return { ok: false, error: `${MODEL} 未找到可点击的发送按钮或输入容器` };
    return {
      ok: true,
      model: MODEL,
      target: { x: rect.right - 30, y: rect.bottom - 28 },
      label: `composer-bottom-right:${String(composer.className || composer.id || composer.tagName).slice(0, 100)}`
    };
  }

  async function verifyTrustedSubmission(question, baseline) {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      await sleep(300);
      const input = findInput();
      const current = snapshot(question);
      if (current.challenge?.detected) {
        throw new Error(`GEO_CHALLENGE_REQUIRED：${MODEL}检测到网页安全验证`);
      }
      const submitted = !inputValue(input)
        || current.busy
        || questionAppearedOutsideInput(input, question);
      if (submitted) return { ok: true, model: MODEL };
    }
    const input = findInput();
    return {
      ok: false,
      model: MODEL,
      error: `${MODEL} 发送后未检测到提交（输入框仍有 ${inputValue(input).length} 字）`
    };
  }

  function fallbackSubmit() {
    const input = findInput();
    const button = findSendButton(input);
    const form = input.closest("form");
    if (form?.requestSubmit) {
      if (button?.matches("button[type='submit'],input[type='submit']")) form.requestSubmit(button);
      else form.requestSubmit();
      return { ok: true, model: MODEL, method: "form.requestSubmit" };
    }
    if (button && !button.disabled && button.getAttribute("aria-disabled") !== "true") {
      button.click();
      return { ok: true, model: MODEL, method: "button.click" };
    }
    pressEnter(input);
    return { ok: true, model: MODEL, method: "synthetic-enter" };
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    panel = document.createElement("aside");
    panel.id = PANEL_ID;
    panel.innerHTML = `
      <header><i></i><b>GEO · ${escapeHtml(SITE?.hosts?.[0] ? ({ doubao: "豆包", yuanbao: "腾讯元宝", wenxin: "文心一言" })[MODEL] : "未知模型")}</b><span class="geo-page-status">检测中</span><button type="button" title="收起">—</button></header>
      <div class="geo-body"><div class="geo-state"><div><small>网页登录</small><b class="geo-login">检测中</b></div><div><small>任务连接</small><b class="geo-connection">等待配置</b></div></div><div class="geo-control-actions"><button class="geo-config-button" type="button">设置 Worker 密钥并连接</button><button class="geo-tasks-button" type="button">任务管理</button></div><progress class="geo-progress" max="100" value="0"></progress><p class="geo-message">等待客户提交诊断任务</p><div class="geo-logs"><div class="geo-empty">暂无运行日志</div></div></div>`;
    panel.querySelector("header button").addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const minimized = panel.dataset.minimized === "true";
      panel.dataset.minimized = minimized ? "false" : "true";
      event.currentTarget.textContent = minimized ? "—" : "+";
    });
    panel.querySelector(".geo-config-button").addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      chrome.runtime.sendMessage({ type: "GEO_OPEN_SETTINGS" });
    });
    panel.querySelector(".geo-tasks-button").addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      chrome.runtime.sendMessage({ type: "GEO_OPEN_TASKS" });
    });
    panel.addEventListener("pointerdown", (event) => event.stopPropagation());
    document.documentElement.appendChild(panel);
    return panel;
  }

  function renderPanel(extensionState = {}) {
    const panel = ensurePanel();
    const ready = checkReady();
    const status = extensionState.status || {};
    const modelState = status.readiness?.[MODEL] || {};
    const modelProgress = status.modelProgress?.[MODEL];
    panel.dataset.ready = ready.ok ? "true" : "false";
    panel.querySelector(".geo-page-status").textContent = ready.ok ? "页面已识别" : "需要登录";
    panel.querySelector(".geo-login").textContent = ready.ok ? "已登录 · 输入框正常" : ready.message;
    const hasConfig = Boolean(extensionState.hasConfig);
    panel.querySelector(".geo-connection").textContent = extensionState.running
      ? ({ connected: "已连接", running: "任务执行中", error: "连接异常", stopped: "已停止", idle: "空闲" }[status.phase] || "正在连接")
      : hasConfig ? "已停止 · 配置已保存" : "尚未设置 Worker";
    const configButton = panel.querySelector(".geo-config-button");
    configButton.textContent = hasConfig ? "恢复或修改连接" : "设置 Worker 密钥并连接";
    configButton.hidden = Boolean(extensionState.running && status.phase !== "error" && status.phase !== "stopped");
    const completed = Number(status.completed || 0);
    const total = Number(status.total || 0);
    panel.querySelector(".geo-progress").value = total ? Math.min(100, Math.round(completed * 100 / total)) : 0;
    panel.querySelector(".geo-message").textContent = modelProgress ? `${({ doubao: "豆包", yuanbao: "腾讯元宝", wenxin: "文心一言" })[MODEL]} ${modelProgress.completed}/${modelProgress.total} 轮完成` : status.message || modelState.message || "等待客户提交诊断任务";
    const logs = Array.isArray(status.logs) ? status.logs.slice(-12).reverse() : [];
    panel.querySelector(".geo-logs").innerHTML = logs.length ? logs.map((item) => `<div class="geo-log ${item.level === "error" ? "error" : ""}"><time>${escapeHtml(String(item.time || "").slice(11, 19))}</time><span>${escapeHtml(item.message || "")}</span></div>`).join("") : '<div class="geo-empty">暂无运行日志</div>';
  }

  async function syncPanelAndPoll() {
    try {
      const ready = checkReady();
      await chrome.runtime.sendMessage({ type: "GEO_PAGE_STATUS", model: MODEL, ready });
      await chrome.runtime.sendMessage({ type: "GEO_POLL" });
      const state = await chrome.runtime.sendMessage({ type: "GEO_GET_STATUS" });
      renderPanel(state || {});
    } catch (error) {
      renderPanel({ running: false, status: { phase: "error", message: String(error?.message || error), readiness: { [MODEL]: { ready: false } } } });
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "GEO_STATUS") {
      renderPanel({
        running: Boolean(message.running),
        hasConfig: Boolean(message.hasConfig),
        status: message.status || {}
      });
      sendResponse({ ok: true });
      return false;
    }
    if (message?.type === "GEO_CHECK_READY") {
      sendResponse(checkReady());
      return false;
    }
    if (message?.type === "GEO_RUN_QUESTION") {
      runQuestion(String(message.question || ""), Number(message.timeoutMs || 240000), Number(message.stableMs || 6000))
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, model: MODEL, error: String(error?.message || error) }));
      return true;
    }
    if (message?.type === "GEO_PREPARE_TRUSTED_QUESTION") {
      try {
        sendResponse(prepareTrustedQuestion());
      } catch (error) {
        restorePanelAfterTrustedInput();
        sendResponse({ ok: false, model: MODEL, error: String(error?.message || error) });
      }
      return false;
    }
    if (message?.type === "GEO_VERIFY_TRUSTED_INPUT") {
      try {
        sendResponse(verifyTrustedInput(String(message.question || "")));
      } catch (error) {
        sendResponse({ ok: false, error: String(error?.message || error) });
      }
      return false;
    }
    if (message?.type === "GEO_FILL_QUESTION_FALLBACK") {
      try {
        sendResponse(fillQuestionFallback(String(message.question || "")));
      } catch (error) {
        sendResponse({ ok: false, error: String(error?.message || error) });
      }
      return false;
    }
    if (message?.type === "GEO_GET_TRUSTED_SEND_TARGET") {
      try {
        sendResponse(trustedSendTarget());
      } catch (error) {
        sendResponse({ ok: false, error: String(error?.message || error) });
      }
      return false;
    }
    if (message?.type === "GEO_VERIFY_TRUSTED_SUBMISSION") {
      verifyTrustedSubmission(String(message.question || ""), message.baseline || {})
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
      return true;
    }
    if (message?.type === "GEO_FALLBACK_SUBMIT") {
      try {
        sendResponse(fallbackSubmit());
      } catch (error) {
        sendResponse({ ok: false, error: String(error?.message || error) });
      }
      return false;
    }
    if (message?.type === "GEO_WAIT_FOR_ANSWER") {
      waitForAnswer(String(message.question || ""), message.baseline || {}, Number(message.timeoutMs || 240000), Number(message.stableMs || 6000))
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, model: MODEL, error: String(error?.message || error) }));
      return true;
    }
    if (message?.type === "GEO_CAPTURE_SNAPSHOT") {
      try {
        sendResponse({ ok: true, model: MODEL, ...snapshot(String(message.question || "")) });
      } catch (error) {
        sendResponse({ ok: false, model: MODEL, error: String(error?.message || error) });
      }
      return false;
    }
    if (message?.type === "GEO_RESTORE_PANEL") {
      restorePanelAfterTrustedInput();
      sendResponse({ ok: true });
      return false;
    }
    if (message?.type === "GEO_START_NEW_CONVERSATION") {
      startNewConversation(sendResponse);
      return false;
    }
    if (message?.type === "GEO_FORCE_NEW_CONVERSATION") {
      forceNewConversation(sendResponse);
      return false;
    }
    if (message?.type === "GEO_GET_CONVERSATION_STATE") {
      sendResponse({ ok: true, model: MODEL, state: conversationState(), ready: checkReady() });
      return false;
    }
    return false;
  });

  ensurePanel();
  syncPanelAndPoll();
  window.setInterval(syncPanelAndPoll, 5000);
})();
