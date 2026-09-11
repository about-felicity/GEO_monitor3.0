(function () {
  "use strict";
  if (window.__quarkMonitorContentLoaded) return;
  window.__quarkMonitorContentLoaded = true;

  const Core = globalThis.QuarkMonitorCore;
  const Adaptive = globalThis.KimiAdaptiveUI;
  const ADAPTIVE_STORAGE_KEY = "kimiAdaptiveUiProfiles";
  let adaptiveProfiles = {};
  const adaptiveSavedAt = new Map();
  const adaptiveLoggedAt = new Map();
  let runnerActive = false;
  let currentSources = [];
  let networkSourceCount = 0;

  window.addEventListener("quark-monitor:sources", (event) => {
    const detail = event.detail || {};
    if (detail.reset) {
      currentSources = [];
      networkSourceCount = 0;
      return;
    }
    const added = (detail.items || []).map((item) => ({ ...item, via: item.via || "network" }));
    networkSourceCount += added.length;
    currentSources = Core.dedupeSources(currentSources.concat(added));
  });

  function message(payload) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(payload, (response) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(response || {});
      });
    });
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (element) => {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width > 2 && box.height > 2;
  };
  const label = (element) => Core.normalizeText([
    element.innerText, element.textContent, element.getAttribute("aria-label"),
    element.getAttribute("title"), element.getAttribute("data-testid")
  ].filter(Boolean).join(" "));

  chrome.storage.local.get([ADAPTIVE_STORAGE_KEY], (data) => {
    adaptiveProfiles = data?.[ADAPTIVE_STORAGE_KEY] && typeof data[ADAPTIVE_STORAGE_KEY] === "object"
      ? data[ADAPTIVE_STORAGE_KEY] : {};
  });

  function rememberAdaptive(role, element) {
    if (!Adaptive || !(element instanceof Element)) return element;
    const now = Date.now();
    if (now - Number(adaptiveSavedAt.get(role) || 0) < 5000) return element;
    const next = Adaptive.fingerprint(element, role, { width: innerWidth, height: innerHeight });
    if (!next) return element;
    const before = JSON.stringify(adaptiveProfiles[role] || {});
    const after = JSON.stringify(next);
    adaptiveSavedAt.set(role, now);
    if (before === after) return element;
    adaptiveProfiles = { ...adaptiveProfiles, [role]: next };
    chrome.storage.local.set({ [ADAPTIVE_STORAGE_KEY]: adaptiveProfiles });
    return element;
  }

  function adaptiveFind(role, candidates, threshold) {
    if (!Adaptive || !adaptiveProfiles[role]) return null;
    const found = Adaptive.relocate(adaptiveProfiles[role], Array.from(candidates || []).filter(visible), role, {
      threshold, viewport: { width: innerWidth, height: innerHeight }
    });
    if (!found) return null;
    const now = Date.now();
    if (now - Number(adaptiveLoggedAt.get(role) || 0) >= 30000) {
      adaptiveLoggedAt.set(role, now);
      logEvent("adaptive_ui_match", "warning", {
        ui_role: role, similarity_score: found.score,
        tag: found.element.tagName.toLowerCase(), test_id: found.element.getAttribute("data-testid") || ""
      });
    }
    return found.element;
  }

  function allClickable() {
    return Array.from(document.querySelectorAll("button,a,[role='button'],[tabindex],div[class*='button']")).filter(visible);
  }

  function findNewChatButton() {
    const direct = document.querySelector(".new-chat-btn,[aria-label='新建会话'],[aria-label='新对话']");
    if (direct && visible(direct)) return rememberAdaptive("new_chat", direct);
    const patterns = [
      /^\+?\s*(?:创建)?新对话$/i,
      /^\+?\s*新建(?:对话|会话)(?:\s+Ctrl\s*K)?$/i,
      /new\s*chat/i,
      /start\s*new\s*chat/i
    ];
    const semantic = Array.from(document.querySelectorAll(
      ".new-chat-btn,[data-testid*='new_chat'],[data-testid*='new-chat'],[aria-label*='新对话'],[aria-label*='新建会话'],[title*='新对话'],div,span"
    )).filter((element) => {
      if (!visible(element)) return false;
      const text = label(element);
      return patterns.some((pattern) => pattern.test(text)) && text.length <= 30 && element.childElementCount <= 8;
    });
    const candidates = Array.from(new Set(allClickable().concat(semantic)));
    const found = candidates
      .map((element) => {
        const text = label(element);
        const box = element.getBoundingClientRect();
        let score = patterns.some((pattern) => pattern.test(text)) ? 100 : 0;
        if (/新建?(?:对话|会话)|new\s*chat/i.test(text)) score += 40;
        if (box.left < innerWidth * 0.3) score += 20;
        if (box.top < innerHeight * 0.3) score += 10;
        if (text.length > 40) score -= 50;
        return { element, score };
      })
      .filter((item) => item.score >= 60)
      .sort((a, b) => b.score - a.score)[0]?.element ||
      adaptiveFind("new_chat", document.querySelectorAll("button,[role='button'],[tabindex],div,span,a"), 68);
    return found ? rememberAdaptive("new_chat", found) : null;
  }

  function composerScore(element) {
    if (!visible(element) || element.disabled || element.readOnly) return -Infinity;
    const box = element.getBoundingClientRect();
    const minimumWidth = Math.min(280, innerWidth * 0.28);
    const inMainArea = box.right >= innerWidth * 0.42 && box.bottom >= innerHeight * 0.2;
    if (box.width < minimumWidth || !inMainArea) return -Infinity;
    const text = [element.getAttribute("placeholder"), element.getAttribute("aria-label"), element.getAttribute("data-placeholder")].filter(Boolean).join(" ");
    let score = 0;
    if (element.matches(".chat-input-editor[contenteditable='true'][role='textbox']")) score += 140;
    if (element.getAttribute("data-testid") === "chat_input_input") score += 120;
    if (/发消息|提问|问问|输入|发送|唤起插件|message|ask|chat|plugin/i.test(text)) score += 50;
    if (element.matches("textarea")) score += 25;
    if (element.matches("[contenteditable='true']")) score += 20;
    if (box.top > innerHeight * 0.55) score += 25;
    if (box.width > Math.min(420, innerWidth * 0.35)) score += 20;
    if (box.height > 160) score -= 15;
    return score;
  }

  function findComposer() {
    const candidates = Array.from(document.querySelectorAll(
      ".chat-input-editor[contenteditable='true'][role='textbox'],[data-testid='chat_input_input'],textarea,[contenteditable='true'],input[type='text'],div[role='textbox']"
    ));
    const found = candidates.map((element) => ({ element, score: composerScore(element) }))
      .filter((item) => Number.isFinite(item.score) && item.score >= 40)
      .sort((a, b) => b.score - a.score)[0]?.element ||
      adaptiveFind("composer", document.querySelectorAll(
        "textarea,input,[contenteditable],[role='textbox'],[class*='input'],[class*='editor']"
      ), 70);
    return found ? rememberAdaptive("composer", found) : null;
  }

  function composerValue(element) {
    if (!element) return "";
    return Core.normalizeText("value" in element ? element.value : element.innerText || element.textContent || "");
  }

  async function waitForComposer(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const composer = findComposer();
      if (composer) return composer;
      await sleep(500);
    }
    const error = retryableError("Kimi新对话页面尚未加载出可用输入框", "DM_COMPOSER_NOT_READY");
    error.reloadPage = true;
    throw error;
  }

  async function createNewChat() {
    const button = findNewChatButton();
    if (!button) {
      const error = retryableError("未找到“新对话”按钮，页面可能尚未加载完整", "QM_NEW_CHAT_NOT_READY");
      error.reloadPage = true;
      throw error;
    }
    await logEvent("new_chat_click", "info", { previous_url: location.href });
    button.click();
    await sleep(1000);
    const composer = await waitForComposer(15000);
    const deadline = Date.now() + 2500;
    while (Date.now() < deadline && composerValue(composer)) await sleep(250);
    if (composerValue(composer)) {
      const draftLength = composerValue(composer).length;
      clearComposerValue(composer);
      await sleep(500);
      await logEvent("old_draft_cleared", "info", { draft_length: draftLength });
    }
    if (composerValue(composer)) {
      await logEvent("draft_clear_deferred", "warning", {
        draft_length: composerValue(composer).length,
        note: "Kimi Lexical 编辑器恢复了旧草稿，将在写入新问题时强制替换并校验"
      });
    }
    return composer;
  }

  function clearComposerValue(element) {
    element.focus();
    if (element.matches("[data-lexical-editor='true'],.chat-input-editor")) {
      document.execCommand("selectAll", false);
      document.execCommand("delete", false);
      return;
    }
    try {
      element.dispatchEvent(new InputEvent("beforeinput", {
        bubbles: true, cancelable: true, inputType: "deleteContentBackward", data: null
      }));
    } catch (_) {}
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (setter) setter.call(element, "");
      else element.value = "";
    } else {
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(element);
      selection.removeAllRanges();
      selection.addRange(range);
      try { document.execCommand("delete", false); } catch (_) { element.textContent = ""; }
      if (Core.compact(composerValue(element))) element.textContent = "";
    }
    element.dispatchEvent(new InputEvent("input", {
      bubbles: true, inputType: "deleteContentBackward", data: null
    }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setComposerValue(element, value) {
    element.focus();
    if (element.matches("[data-lexical-editor='true'],.chat-input-editor")) {
      document.execCommand("selectAll", false);
      document.execCommand("delete", false);
      document.execCommand("insertText", false, value);
      return;
    }
    try {
      element.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: value }));
    } catch (_) {}
    if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
      const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (setter) setter.call(element, value);
      else element.value = value;
    } else {
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(element);
      selection.removeAllRanges();
      selection.addRange(range);
      try { document.execCommand("delete", false); } catch (_) {}
      try { document.execCommand("insertText", false, value); } catch (_) { element.textContent = value; }
      if (!Core.compact(composerValue(element))) element.textContent = value;
    }
    element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function findSendButton(composer) {
    const direct = document.querySelector(".send-button-container:has(svg[name='Send']),[data-testid='chat_input_send_button']");
    if (direct && visible(direct) && !direct.disabled && direct.getAttribute("aria-disabled") !== "true") {
      return rememberAdaptive("send_button", direct);
    }
    const composerBox = composer.getBoundingClientRect();
    const regions = [];
    let parent = composer.parentElement;
    for (let i = 0; parent && i < 6; i += 1, parent = parent.parentElement) regions.push(parent);
    const raw = regions.flatMap((region) => Array.from(region.querySelectorAll(
      "button,[role='button'],[tabindex],[class*='send'],[class*='submit'],[data-testid],svg[name='Send'],svg"
    )));
    const candidates = Array.from(new Set(raw.map((element) => {
      if (element.tagName.toLowerCase() !== "svg") return element;
      return element.closest("button,[role='button'],[tabindex],[class*='send'],[class*='submit']") || element.parentElement;
    }).filter(Boolean))).filter((element) => visible(element) && !element.disabled && element.getAttribute("aria-disabled") !== "true");
    const found = candidates.map((element) => {
      const text = label(element) + " " + String(element.className?.baseVal || element.className || "") + " " + String(element.innerHTML || "").slice(0, 300);
      const box = element.getBoundingClientRect();
      const horizontallyNear = box.right >= composerBox.left - 40 && box.left <= composerBox.right + 140;
      const verticallyNear = box.bottom >= composerBox.top - 80 && box.top <= composerBox.bottom + 80;
      if (!horizontallyNear || !verticallyNear) return { element, score: -Infinity };
      let score = 0;
      if (/^发送$|发送消息|send/i.test(text)) score += 100;
      if (/arrow|submit|send/i.test(element.className || "")) score += 40;
      if (box.left >= composerBox.left + composerBox.width * 0.65) score += 20;
      if (Math.abs(box.bottom - composerBox.bottom) < 100) score += 20;
      if (box.width <= 80 && box.height <= 80) score += 10;
      if (getComputedStyle(element).cursor === "pointer") score += 12;
      score += Math.max(0, Math.min(20, (box.left - composerBox.left) / Math.max(1, composerBox.width) * 20));
      if (/语音|麦克风|录音|附件|上传|voice|microphone|upload/i.test(text)) score -= 120;
      return { element, score };
    }).filter((item) => item.score >= 35).sort((a, b) => b.score - a.score)[0]?.element ||
      adaptiveFind("send_button", document.querySelectorAll(
        "button,[role='button'],[tabindex],[class*='send'],[class*='submit'],svg"
      ), 72);
    return found ? rememberAdaptive("send_button", found) : null;
  }

  function dispatchEnter(composer) {
    composer.focus();
    for (const type of ["keydown", "keypress", "keyup"]) {
      composer.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter", code: "Enter", keyCode: 13, which: 13,
        bubbles: true, cancelable: true, composed: true
      }));
    }
  }

  function sendDiagnostics(composer) {
    const box = composer.getBoundingClientRect();
    const nearby = Array.from(document.querySelectorAll("button,[role='button'],[tabindex],[class*='send'],[class*='submit']"))
      .filter(visible)
      .map((element) => {
        const itemBox = element.getBoundingClientRect();
        return { element, itemBox, distance: Math.abs(itemBox.bottom - box.bottom) + Math.abs(itemBox.right - box.right) };
      })
      .sort((a, b) => a.distance - b.distance)
      .slice(0, 8)
      .map(({ element, itemBox }) => ({
        tag: element.tagName.toLowerCase(), text: label(element).slice(0, 80),
        class: String(element.className?.baseVal || element.className || "").slice(0, 120),
        disabled: Boolean(element.disabled), ariaDisabled: element.getAttribute("aria-disabled"),
        x: Math.round(itemBox.x), y: Math.round(itemBox.y), width: Math.round(itemBox.width), height: Math.round(itemBox.height)
      }));
    return JSON.stringify({ composer: {
      tag: composer.tagName.toLowerCase(), role: composer.getAttribute("role"),
      contenteditable: composer.getAttribute("contenteditable"), class: String(composer.className || "").slice(0, 160),
      x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), height: Math.round(box.height)
    }, nearby });
  }

  async function submitPrompt(composer, prompt) {
    setComposerValue(composer, prompt);
    await sleep(800);
    if (Core.compact(composerValue(composer)) !== Core.compact(prompt)) {
      const error = retryableError("问题写入输入框后校验失败", "QM_COMPOSER_WRITE_FAILED");
      error.reloadPage = true;
      throw error;
    }
    await logEvent("prompt_prepared", "info", { prompt, composer: sendDiagnostics(composer) });
    let activeComposer = composer;
    let send = null;
    const sendReadyDeadline = Date.now() + 6000;
    while (Date.now() < sendReadyDeadline) {
      const current = findComposer();
      if (current && current !== activeComposer) {
        activeComposer = current;
        if (Core.compact(composerValue(activeComposer)) !== Core.compact(prompt)) {
          setComposerValue(activeComposer, prompt);
          await sleep(400);
        }
      }
      send = findSendButton(activeComposer);
      if (send) break;
      await sleep(400);
    }
    if (send) send.click();
    else dispatchEnter(activeComposer);
    const deadline = Date.now() + 15000;
    const attemptedAt = Date.now();
    let enterRetried = !send;
    while (Date.now() < deadline) {
      const cleared = !Core.compact(composerValue(findComposer()));
      const present = Core.compact(document.body.innerText).includes(Core.compact(prompt));
      if (cleared && present) {
        await logEvent("prompt_submitted", "info", { prompt, page_url: location.href });
        return;
      }
      if (!enterRetried && Date.now() - attemptedAt > 2500) {
        enterRetried = true;
        dispatchEnter(activeComposer);
      }
      await sleep(500);
    }
    const diagnostic = sendDiagnostics(activeComposer);
    await logEvent("send_unconfirmed", "warning", { prompt, page_url: location.href, diagnostic });
    const error = retryableError("问题已写入，但Kimi页面没有确认发送；将刷新页面后重试", "DM_SUBMIT_UNCONFIRMED");
    error.reloadPage = true;
    throw error;
  }

  function findPromptElement(prompt) {
    const target = Core.compact(prompt);
    if (!target) return null;
    const nodes = Array.from(document.querySelectorAll("p,div,span,[data-message-id],[class*='message']"));
    return nodes.filter(visible).map((element) => ({
      element,
      text: Core.compact(element.innerText || element.textContent || "")
    })).filter((item) => item.text === target || (item.text.startsWith(target) && item.text.length < target.length + 30))
      .sort((a, b) => (a.element.childElementCount - b.element.childElementCount))[0]?.element || null;
  }

  function follows(element, reference) {
    if (!reference || !element || element === reference || element.contains(reference)) return false;
    return Boolean(reference.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING);
  }

  function answerCandidates(prompt) {
    const promptElement = findPromptElement(prompt);
    const kimiAssistant = Array.from(document.querySelectorAll(".chat-content-item-assistant")).filter(visible).slice(-1)[0];
    if (kimiAssistant) {
      const finalAnswer = kimiAssistant.querySelector(
        ".toolcall-rollup__tail .markdown-container," +
        ".toolcall-rollup > .toolcall-rollup__part > .markdown-container:not(.toolcall-content-text)," +
        ".segment-content > .markdown-container,.chat-content-item-assistant > .markdown-container"
      );
      if (!finalAnswer || !visible(finalAnswer)) return null;
      const finalText = Core.normalizeText(finalAnswer.innerText || finalAnswer.textContent || "");
      if (finalText.length < 20) return null;
      rememberAdaptive("answer", finalAnswer);
      return {
        element: finalAnswer,
        text: Core.cleanAnswerText(finalText, prompt),
        rawText: finalText,
        afterPrompt: follows(finalAnswer, promptElement),
        hasCopyAction: Array.from(kimiAssistant.querySelectorAll("button,[role='button']")).some((item) => /复制|copy/i.test(label(item))),
        hasCitation: Boolean(finalAnswer.querySelector("a[href],sup,[class*='cite'],[class*='reference']")),
        isMessageLike: true,
        isRootLike: false,
        containsSidebar: false,
        selectorHint: ".chat-content-item-assistant .markdown-container",
        score: 999
      };
    }
    const nodes = new Set(Array.from(document.querySelectorAll(
      "[data-message-id],[data-testid*='message'],[data-role*='assistant'],[class*='assistant'],[class*='bot'],[class*='answer'],[class*='response'],[class*='message'],[class*='markdown'],[class*='content']"
    )));
    if (promptElement) {
      const fallbackNodes = Array.from(document.querySelectorAll(
        "main article,main section,main div,article,section,[role='article'],[role='region']"
      )).slice(0, 8000);
      for (const element of fallbackNodes) {
        if (!visible(element) || !follows(element, promptElement)) continue;
        const text = Core.normalizeText(element.innerText || element.textContent || "");
        if (text.length < 60 || text.length > 30000) continue;
        if (/直接提问|新对话/.test(text) || element.querySelector("textarea,[contenteditable='true'],div[role='textbox']")) continue;
        nodes.add(element);
      }
    }
    for (const button of allClickable().filter((item) => /复制|copy/i.test(label(item)))) {
      let parent = button.parentElement;
      for (let i = 0; parent && i < 6; i += 1, parent = parent.parentElement) {
        const length = Core.normalizeText(parent.innerText).length;
        if (length >= 30 && length <= 100000) nodes.add(parent);
      }
    }
    const adaptiveAnswer = adaptiveFind("answer", document.querySelectorAll(
      "main article,main section,main div,article,section,[role='article'],[role='region'],[class*='message'],[class*='markdown']"
    ), 76);
    if (adaptiveAnswer) nodes.add(adaptiveAnswer);
    const items = [];
    for (const element of nodes) {
      if (!visible(element)) continue;
      const text = Core.normalizeText(element.innerText || "");
      if (text.length < 20 || text.length > 120000) continue;
      const box = element.getBoundingClientRect();
      const classText = String(element.className || "");
      items.push({
        element,
        text,
        afterPrompt: follows(element, promptElement),
        hasCopyAction: Array.from(element.querySelectorAll("button,[role='button']")).some((item) => /复制|copy/i.test(label(item))),
        hasCitation: Boolean(element.querySelector("sup,[class*='cite'],[class*='reference'],a[href]")),
        isMessageLike: /assistant|bot|answer|response|message|markdown/i.test(classText),
        isRootLike: element === document.body || element.matches("main,[id='app'],[id='root']") || text.length > 50000,
        containsSidebar: /新对话/.test(text) && box.left < innerWidth * 0.25,
        selectorHint: [element.tagName.toLowerCase(), element.id ? "#" + element.id : "", classText ? "." + classText.split(/\s+/).slice(0, 3).join(".") : ""].join("")
      });
    }
    const chosen = Core.chooseAnswerCandidate(items, prompt);
    if (chosen?.element) rememberAdaptive("answer", chosen.element);
    return chosen;
  }

  function generationBusy() {
    const kimiAssistant = Array.from(document.querySelectorAll(".chat-content-item-assistant")).filter(visible).slice(-1)[0];
    if (kimiAssistant && !kimiAssistant.querySelector(
      ".toolcall-rollup__tail .markdown-container," +
      ".toolcall-rollup > .toolcall-rollup__part > .markdown-container:not(.toolcall-content-text)," +
      ".segment-content > .markdown-container,.chat-content-item-assistant > .markdown-container"
    )) return true;
    return allClickable().some((element) => /停止(生成|回答|响应)|stop\s*(generating|answer|response)|取消生成/i.test(label(element)));
  }

  function retryableError(message, code) {
    const error = new Error(message);
    error.code = code || "QM_TRANSIENT";
    error.retryable = true;
    return error;
  }

  async function waitForAnswer(prompt, settings, submittedAt) {
    const timeoutMs = Math.max(30, Number(settings.timeoutSeconds) || 240) * 1000;
    const stableNeeded = Math.max(3, Math.ceil((Number(settings.stableSeconds) || 10) / 2));
    const deadline = Date.now() + timeoutMs;
    let last = "";
    let stable = 0;
    let contentStable = 0;
    let bestCandidate = null;
    let lastDiagnosticAt = submittedAt || Date.now();
    let lastBusy = false;
    let emptySince = Date.now();
    while (Date.now() < deadline) {
      const control = await message({ type: "GET_JOB" });
      if (!control.job || ["stopped", "error"].includes(control.job.state)) throw new Error("任务已停止");
      while (control.job?.state === "paused") {
        await sleep(1000);
        const again = await message({ type: "GET_JOB" });
        if (again.job?.state !== "paused") break;
      }
      const bodyText = document.body.innerText || "";
      const pageFailure = Core.detectTransientFailure(bodyText);
      if (pageFailure && Date.now() - submittedAt >= 3000) {
        throw retryableError(`Kimi页面返回“${pageFailure}”`, "DM_PAGE_TRANSIENT");
      }
      const blockingState = Core.detectBlockingState(bodyText);
      if (blockingState && Date.now() - submittedAt >= 3000) {
        const error = retryableError(`Kimi页面提示“${blockingState}”`, "DM_PAGE_BLOCKED");
        error.reloadPage = true;
        throw error;
      }
      const candidate = answerCandidates(prompt);
      const text = candidate?.text || "";
      const busy = generationBusy();
      const minimumLength = Math.max(30, Number(settings.minAnswerLength) || 60);
      const equivalent = text.length >= minimumLength && Core.answerTextEquivalent(text, last);
      contentStable = equivalent ? contentStable + 1 : 0;
      stable = !busy && equivalent ? stable + 1 : 0;
      if (candidate && text.length >= minimumLength && (!bestCandidate || text.length >= bestCandidate.text.length)) {
        bestCandidate = candidate;
      }
      if (candidate || busy) emptySince = Date.now();
      last = text;
      lastBusy = busy;
      if (stable >= stableNeeded && Date.now() - submittedAt >= 8000) {
        await logEvent("answer_stable", "info", { prompt, reply_chars: text.length, stable_polls: stable });
        return { ...candidate, completionMode: "stable" };
      }
      const stuckThreshold = Math.max(12, stableNeeded * 3);
      if (contentStable >= stuckThreshold && Date.now() - submittedAt >= 45000) {
        await logEvent("answer_ui_state_fallback", "warning", {
          prompt, reply_chars: text.length, stable_polls: contentStable,
          generation_busy: busy, selector_hint: candidate?.selectorHint || ""
        });
        return { ...candidate, completionMode: "ui_state_fallback" };
      }
      if (Date.now() - lastDiagnosticAt >= 60000) {
        lastDiagnosticAt = Date.now();
        await logEvent("answer_wait_diagnostic", "info", {
          prompt, reply_chars: text.length, stable_polls: contentStable,
          generation_busy: busy, has_candidate: Boolean(candidate), selector_hint: candidate?.selectorHint || ""
        });
      }
      if (!candidate && !busy && Date.now() - emptySince >= 45000) {
        await logEvent("empty_answer_detected", "warning", {
          prompt, empty_seconds: Math.round((Date.now() - emptySince) / 1000),
          page_url: location.href, body_chars: Core.normalizeText(bodyText).length,
          prompt_visible: Boolean(findPromptElement(prompt))
        });
        const error = retryableError("问题已发送，但Kimi45秒内没有生成任何可识别的回答", "DM_EMPTY_RESPONSE");
        error.reloadPage = true;
        throw error;
      }
      await sleep(2000);
    }
    if (bestCandidate?.text?.length >= Math.max(30, Number(settings.minAnswerLength) || 60)) {
      await logEvent("answer_timeout_fallback", "warning", {
        prompt, reply_chars: bestCandidate.text.length, stable_polls: contentStable,
        generation_busy: lastBusy, selector_hint: bestCandidate.selectorHint || ""
      });
      return { ...bestCandidate, completionMode: "timeout_fallback" };
    }
    throw retryableError(`等待回答完成超时，且未找到可保存的有效正文（候选 ${bestCandidate?.text?.length || 0} 字）`, "QM_ANSWER_TIMEOUT");
  }

  function sourceElements(root) {
    const scope = root?.element || document;
    const anchors = Array.from(scope.querySelectorAll("a[href],[data-href],[data-url]"));
    const overlays = Array.from(document.querySelectorAll(
      "[role='tooltip'] a[href],[role='dialog'] a[href],[class*='popover'] a[href],[class*='tooltip'] a[href]," +
      "[class*='reference'] a[href],[class*='source'] a[href],[class*='card'] a[href]"
    ));
    const visibleExternal = Array.from(document.querySelectorAll("a[href],[data-href],[data-url]"))
      .filter((element) => visible(element) && Core.isExternalUrl(element.href || element.getAttribute("data-href") || element.getAttribute("data-url")));
    return Array.from(new Set(anchors.concat(overlays, visibleExternal)));
  }

  function sourceCardText(element) {
    const pieces = [
      element.getAttribute("data-title"), element.getAttribute("title"), element.getAttribute("aria-label"),
      element.innerText, element.textContent
    ];
    let parent = element;
    for (let depth = 0; parent && depth < 6; depth += 1, parent = parent.parentElement) {
      const looksLikeCard = parent.matches?.(
        "article,li,[role='tooltip'],[role='dialog'],[class*='popover'],[class*='tooltip']," +
        "[class*='reference'],[class*='source'],[class*='card']"
      );
      const text = Core.normalizeText(parent.innerText || parent.textContent || "");
      if (looksLikeCard && text.length >= 3 && text.length <= 3000) pieces.push(text);
    }
    return pieces.filter(Boolean).join("\n");
  }

  function collectDomSources(candidate) {
    return Core.dedupeSources(sourceElements(candidate).map((element) => {
      const url = element.href || element.getAttribute("data-href") || element.getAttribute("data-url");
      const isKimiCitation = element.matches?.(".pua-ref-cite-tag") && !Core.normalizeText([
        element.getAttribute("data-title"), element.getAttribute("title"),
        element.getAttribute("aria-label"), element.innerText, element.textContent
      ].filter(Boolean).join(" "));
      return {
        url,
        title: isKimiCitation ? "" : Core.pickSourceTitle(sourceCardText(element), url),
        via: isKimiCitation ? "kimi_citation_pending_title" : "dom"
      };
    }));
  }

  function citationMarkers(candidate) {
    if (!candidate?.element) return [];
    return Array.from(candidate.element.querySelectorAll("sup,button,[role='button'],[class*='cite'],[class*='reference'],[class*='source']"))
      .filter((element) => visible(element) && /^\s*\d{1,3}\s*$/.test(element.innerText || element.textContent || ""))
      .slice(0, 60);
  }

  async function revealAndCollectSources(candidate) {
    let collected = collectDomSources(candidate);
    for (const marker of citationMarkers(candidate)) {
      try {
        marker.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
        marker.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        marker.focus({ preventScroll: true });
      } catch (_) {}
      await sleep(180);
      collected = Core.dedupeSources(collected.concat(collectDomSources(candidate)));
    }
    return collected;
  }

  function expectedSourceCount(candidate) {
    const values = citationMarkers(candidate).map((element) => Number((element.innerText || element.textContent || "").trim())).filter(Number.isFinite);
    const body = candidate?.rawText || candidate?.text || "";
    const prefixMatch = body.match(/(?:共参考|参考资料|参考链接|参考|信源|引用|来源)\s*(\d{1,3})\s*(?:篇|条|个)?/);
    const suffixMatches = Array.from(body.matchAll(/(\d{1,3})\s*篇来源/g));
    const suffixCount = suffixMatches.length ? Number(suffixMatches[suffixMatches.length - 1][1]) : 0;
    return Math.max(prefixMatch ? Number(prefixMatch[1]) : 0, suffixCount, ...values, 0);
  }

  async function logEvent(event, level, details) {
    try {
      await message({ type: "LOG_EVENT", event, level: level || "info", details: details || {} });
    } catch (_) {}
  }

  function detectedModel() {
    const text = document.body.innerText || "";
    return text.match(/Kimi(?:[·\s-]*(?:深度思考|联网搜索|极速|专业))?/i)?.[0] || "Kimi";
  }

  async function processItem(job, settings) {
    const item = job.schedule[job.cursor];
    if (!item) return null;
    const prior = job.inFlight || {};
    const retryCount = Math.max(0, Number(prior.retryCount) || 0);
    let submittedAt = prior.submittedAt ? Date.parse(prior.submittedAt) : 0;
    const resuming = prior.globalIndex === item.globalIndex && prior.phase === "submitted";
    if (!resuming) {
      await message({ type: "SET_IN_FLIGHT", inFlight: { ...item, phase: "preparing", retryCount, startedAt: new Date().toISOString(), previousUrl: location.href } });
      currentSources = [];
      networkSourceCount = 0;
      window.dispatchEvent(new CustomEvent("quark-monitor:reset"));
      const composer = await createNewChat();
      await submitPrompt(composer, item.prompt);
      submittedAt = Date.now();
      await message({ type: "SET_IN_FLIGHT", inFlight: { ...item, phase: "submitted", retryCount, startedAt: new Date().toISOString(), submittedAt: new Date(submittedAt).toISOString(), previousUrl: location.href } });
    } else if (!Core.compact(document.body.innerText).includes(Core.compact(item.prompt))) {
      const titleMatches = Core.compact(document.title).includes(Core.compact(item.prompt));
      const hasAssistant = Boolean(document.querySelector(".chat-content-item-assistant"));
      if (!titleMatches && !hasAssistant) {
        throw new Error("检测到中断前问题已提交，但当前页面不是该会话；请打开对应会话后点击继续，程序不会重复提问");
      }
      await logEvent("resume_context_inferred", "warning", {
        prompt: item.prompt, page_url: location.href, title_matches: titleMatches, has_assistant: hasAssistant
      });
    }

    const candidate = await waitForAnswer(item.prompt, settings, submittedAt || Date.now());
    const domSources = await revealAndCollectSources(candidate);
    const expected = expectedSourceCount(candidate);
    const rawSources = Core.dedupeSources(domSources.concat(currentSources));
    const sources = Core.selectCitationSources(rawSources, expected);
    const finishedAt = new Date();
    const startedAt = job.inFlight?.startedAt || new Date(submittedAt || Date.now()).toISOString();
    const result = {
      schema_version: 1,
      result_id: crypto.randomUUID(),
      status: "success",
      skip_reason: "",
      collector_model: "kimi",
      run_id: job.id,
      round: item.globalIndex + 1,
      question_index: item.questionIndex,
      question_round: item.questionRound,
      prompt: item.prompt,
      reply: candidate.text,
      web_body: candidate.text,
      sources,
      expected_source_count: expected,
      page_reported_source_count: expected,
      source_capture_complete: expected === 0 ? true : sources.length >= expected,
      source_count_basis: expected ? "citation_markers" : "no_page_count",
      page_url: location.href,
      page_title: document.title,
      detected_model: detectedModel(),
      started_at: startedAt,
      submitted_at: new Date(submittedAt || Date.now()).toISOString(),
      finished_at: finishedAt.toISOString(),
      duration_ms: Math.max(0, finishedAt.getTime() - Date.parse(startedAt)),
      capture: {
        answer_selector_hint: candidate.selectorHint || "",
        answer_completion_mode: candidate.completionMode || "stable",
        network_source_events: networkSourceCount,
        dom_source_count: domSources.length,
        raw_source_count: rawSources.length,
        filtered_source_count: sources.length
      }
    };
    await logEvent("capture_complete", "info", {
      prompt: item.prompt, reply_chars: candidate.text.length,
      source_count: sources.length, raw_source_count: rawSources.length,
      expected_source_count: expected, source_capture_complete: result.source_capture_complete,
      page_url: location.href, sources
    });
    return result;
  }

  async function runLoop() {
    if (runnerActive) return;
    runnerActive = true;
    try {
      const claim = await message({ type: "CLAIM_RUNNER" });
      if (!claim.claimed || !claim.job) return;
      while (true) {
        const context = await message({ type: "GET_JOB" });
        const job = context.job;
        const settings = context.settings || {};
        if (!job || job.state === "stopped" || job.state === "completed" || job.state === "error") break;
        if (job.state === "paused") { await sleep(1000); continue; }
        if (job.cursor >= job.schedule.length) {
          await message({ type: "COMPLETE_JOB" });
          break;
        }
        try {
          const result = await processItem(job, settings);
          if (!result) break;
          await message({ type: "STORE_RESULT", result });
          const next = await message({ type: "GET_JOB" });
          if (next.job?.cursor < next.job?.schedule?.length) {
            const min = Math.max(1, Number(settings.intervalMinSeconds) || 15);
            const max = Math.max(min, Number(settings.intervalMaxSeconds) || 45);
            await sleep((min + Math.random() * (max - min)) * 1000);
          }
        } catch (error) {
          const current = await message({ type: "GET_JOB" });
          const inFlight = current.job?.inFlight || {};
          const attempts = Math.max(0, Number(inFlight.retryCount) || 0);
          const maxRetries = Math.max(0, Math.min(10, Number(settings.maxRetries) || 3));
          if (error?.retryable && attempts < maxRetries) {
            const nextAttempt = attempts + 1;
            const delay = Math.max(5, Number(settings.retryDelaySeconds) || 20);
            await logEvent("round_retry_scheduled", "warning", {
              error: String(error && error.message || error), retry_attempt: nextAttempt,
              max_retries: maxRetries, retry_seconds: delay
            });
            await message({ type: "SET_IN_FLIGHT", inFlight: {
              ...(job.schedule[job.cursor] || inFlight), phase: "retry_pending", retryCount: nextAttempt,
              lastFailure: String(error && error.message || error), failedAt: new Date().toISOString()
            } });
            await sleep(delay * 1000);
            if (error.reloadPage) {
              await logEvent("page_reload_for_retry", "warning", {
                error: String(error && error.message || error), retry_attempt: nextAttempt
              });
              location.reload();
              return;
            }
            continue;
          }
          if (error?.retryable && attempts >= maxRetries) {
            const failedItem = job.schedule[job.cursor] || job.inFlight || {};
            const finishedAt = new Date();
            const startedAt = current.job?.inFlight?.startedAt || finishedAt.toISOString();
            const failure = {
              schema_version: 1, result_id: crypto.randomUUID(), status: "failed",
              skip_reason: String(error && error.message || error), collector_model: "kimi",
              run_id: job.id, round: Number(failedItem.globalIndex ?? job.cursor) + 1,
              question_index: failedItem.questionIndex, question_round: failedItem.questionRound,
              prompt: failedItem.prompt || "", reply: "", web_body: "", sources: [],
              expected_source_count: 0, page_reported_source_count: 0,
              source_capture_complete: false, source_count_basis: "failed_round",
              page_url: location.href, page_title: document.title, detected_model: detectedModel(),
              started_at: startedAt, submitted_at: current.job?.inFlight?.submittedAt || startedAt,
              finished_at: finishedAt.toISOString(),
              duration_ms: Math.max(0, finishedAt.getTime() - Date.parse(startedAt)),
              capture: { failure_code: error.code || "QM_TRANSIENT", retry_count: attempts }
            };
            await logEvent("round_failed_continuing", "error", {
              error: failure.skip_reason, retry_count: attempts, next_round: failure.round + 1
            });
            await message({ type: "STORE_RESULT", result: failure });
            if (error.reloadPage) {
              await logEvent("page_reload_for_retry", "warning", {
                error: "本轮已记录失败；刷新页面后继续下一轮", retry_attempt: "下一轮"
              });
              await sleep(3000);
              location.reload();
              return;
            }
            await sleep(Math.max(20, Number(settings.intervalMinSeconds) || 15) * 1000);
            continue;
          }
          await logEvent("round_error", "error", { error: String(error && error.message || error) });
          await message({ type: "JOB_ERROR", error: String(error && error.message || error) });
          break;
        }
      }
    } finally {
      runnerActive = false;
    }
  }

  chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
    if (request?.type === "QM_RUN") {
      runLoop();
      sendResponse({ ok: true });
    } else if (request?.type === "QM_PROBE") {
      const composer = findComposer();
      const newChat = findNewChatButton();
      const loginRequired = Array.from(document.querySelectorAll(
        ".phone-form, .login-modal, .login-dialog, button.login-button, [class*='login-popup']"
      )).some((element) => visible(element) && /登录|log\s*in|sign\s*in/i.test(
        String(element.innerText || element.textContent || "")
      ));
      sendResponse({
        ok: true, url: location.href, title: document.title,
        hasComposer: Boolean(composer), hasNewChat: Boolean(newChat),
        loginRequired,
        composerTestId: composer?.getAttribute("data-testid") || "",
        composerTag: composer?.tagName?.toLowerCase() || "",
        newChatText: newChat ? label(newChat).slice(0, 80) : ""
      });
    }
    return false;
  });

  message({ type: "GET_JOB" }).then((context) => {
    if (context.job?.state === "running") runLoop();
  }).catch(() => {});
})();
