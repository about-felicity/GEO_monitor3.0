const MODEL_ORDER = ["doubao", "yuanbao", "wenxin"];
const MODEL_NAMES = { doubao: "豆包", yuanbao: "腾讯元宝", wenxin: "文心一言" };
const WORKER_BUILD = "1.8.24";
const ADAPTIVE_STORAGE_KEY = "geoAdaptiveElementsV1";
const ADAPTIVE_IDENTIFIERS = new Set(["question-input", "new-conversation", "answer-body"]);
const TAB_PATTERNS = {
  doubao: ["https://www.doubao.com/*", "https://doubao.com/*"],
  yuanbao: ["https://yuanbao.tencent.com/*"],
  wenxin: ["https://wenxin.baidu.com/*"]
};
const MODEL_INTERNAL_HOSTS = {
  doubao: ["doubao.com", "byteimg.com", "bytedance.com"],
  yuanbao: ["yuanbao.tencent.com", "tencent.com", "qq.com"],
  wenxin: ["wenxin.baidu.com", "baidu.com"]
};

let polling = false;
let executing = false;
let statusUpdates = Promise.resolve();
let adaptiveUpdates = Promise.resolve();

if (chrome.storage.local.setAccessLevel) {
  chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" }).catch(() => {});
}

async function session() {
  const [volatile, persisted] = await Promise.all([
    chrome.storage.session.get(["config", "running", "status"]),
    chrome.storage.local.get(["savedConfig", "autoStart"])
  ]);
  return {
    ...volatile,
    config: volatile.config || persisted.savedConfig || null,
    running: typeof volatile.running === "boolean" ? volatile.running : Boolean(persisted.autoStart)
  };
}

async function commitStatus(patch) {
  const current = (await chrome.storage.session.get("status")).status || {};
  const now = new Date().toISOString();
  let logs = Array.isArray(current.logs) ? current.logs : [];
  if (patch.message && patch.message !== current.message && patch.log !== false) {
    logs = [...logs, {
      time: now,
      level: patch.level || (patch.phase === "error" ? "error" : "info"),
      model: patch.model || current.model || "",
      message: String(patch.message)
    }].slice(-40);
  }
  const cleanPatch = { ...patch };
  delete cleanPatch.log;
  delete cleanPatch.level;
  const status = {
    ...current,
    ...cleanPatch,
    modelProgress: {
      ...(current.modelProgress || {}),
      ...(cleanPatch.modelProgress || {})
    },
    logs,
    updatedAt: now
  };
  await chrome.storage.session.set({ status });
  const connection = await session();
  const tabs = await chrome.tabs.query({ url: Object.values(TAB_PATTERNS).flat() });
  await Promise.all(tabs.filter((tab) => Number.isInteger(tab.id)).map((tab) =>
    chrome.tabs.sendMessage(tab.id, {
      type: "GEO_STATUS",
      status,
      running: Boolean(connection.running),
      hasConfig: Boolean(connection.config?.token)
    }).catch(() => {})
  ));
  return status;
}

function setStatus(patch) {
  const update = () => commitStatus(patch);
  statusUpdates = statusUpdates.then(update, update);
  return statusUpdates;
}

function saveAdaptiveElement(model, identifier, fingerprint) {
  const update = async () => {
    if (!MODEL_ORDER.includes(model) || !ADAPTIVE_IDENTIFIERS.has(identifier)) throw new Error("自适应元素标识无效");
    const serialized = JSON.stringify(fingerprint || {});
    if (serialized.length < 20 || serialized.length > 12000) throw new Error("自适应元素特征大小无效");
    const stored = await chrome.storage.local.get(ADAPTIVE_STORAGE_KEY);
    const elements = stored?.[ADAPTIVE_STORAGE_KEY] || {};
    elements[`${model}:${identifier}`] = JSON.parse(serialized);
    const allowed = Object.fromEntries(Object.entries(elements).filter(([key]) => {
      const [savedModel, savedIdentifier] = key.split(":", 2);
      return MODEL_ORDER.includes(savedModel) && ADAPTIVE_IDENTIFIERS.has(savedIdentifier);
    }));
    await chrome.storage.local.set({ [ADAPTIVE_STORAGE_KEY]: allowed });
  };
  adaptiveUpdates = adaptiveUpdates.then(update, update);
  return adaptiveUpdates;
}

async function api(config, path, options = {}) {
  const response = await fetch(config.server.replace(/\/$/, "") + path, {
    ...options,
    cache: "no-store",
    headers: {
      "Authorization": `Bearer ${config.token}`,
      "Content-Type": "application/json; charset=utf-8",
      ...(options.headers || {})
    }
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok || !value.ok) throw new Error(value.error || `服务器返回 HTTP ${response.status}`);
  return value;
}

async function modelTab(model) {
  const tabs = await chrome.tabs.query({ url: TAB_PATTERNS[model] });
  return tabs.find((tab) => Number.isInteger(tab.id)) || null;
}

function supportedModel(urlValue) {
  try {
    const url = new URL(String(urlValue || ""));
    return MODEL_ORDER.find((model) => TAB_PATTERNS[model].some((pattern) => {
      const host = new URL(pattern.replace("*", "")).hostname;
      return url.hostname === host;
    })) || "";
  } catch (_) {
    return "";
  }
}

async function injectTab(tabId, url) {
  if (!Number.isInteger(tabId) || !supportedModel(url)) return;
  await chrome.scripting.insertCSS({ target: { tabId }, files: ["content.css"] }).catch(() => {});
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] }).catch(() => {});
}

async function injectKnownTabs() {
  const tabs = await chrome.tabs.query({ url: Object.values(TAB_PATTERNS).flat() });
  await Promise.all(tabs.map((tab) => injectTab(tab.id, tab.url)));
}

async function waitForTabComplete(tabId, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tab;
    await sleep(250);
  }
  throw new Error("任务管理页面加载超时");
}

async function openTaskManager() {
  const state = await session();
  const server = String(state.config?.server || "https://www.ifbcy.com/geo").replace(/\/$/, "");
  const token = String(state.config?.token || "");
  if (!token) throw new Error("请先在插件中设置 Worker 密钥并连接");
  const tab = await chrome.tabs.create({ url: `${server}/tasks` });
  await waitForTabComplete(tab.id);
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: (workerToken) => {
      sessionStorage.setItem("monitorTaskKey", workerToken);
      const input = document.getElementById("key");
      if (!input) throw new Error("管理页未找到授权输入框");
      input.value = workerToken;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    },
    args: [token]
  });
}

async function sendToModel(model, message) {
  const tab = await modelTab(model);
  if (!tab) throw new Error(`请先打开并登录${MODEL_NAMES[model]}网页`);
  try {
    return await chrome.tabs.sendMessage(tab.id, message);
  } catch (_) {
    await injectTab(tab.id, tab.url);
    return chrome.tabs.sendMessage(tab.id, message);
  }
}

async function startFreshConversation(model) {
  const opened = await sendToModel(model, { type: "GEO_START_NEW_CONVERSATION" });
  if (!opened?.ok) throw new Error(opened?.error || `${MODEL_NAMES[model]}未能创建新对话`);
  const before = opened.before || {};
  const startedAt = Date.now();
  const deadline = startedAt + 35000;
  let forced = null;
  let forcedAt = 0;
  let lastMessage = "正在等待新对话页面就绪";
  while (Date.now() < deadline) {
    await sleep(900);
    try {
      const current = await sendToModel(model, { type: "GEO_GET_CONVERSATION_STATE" });
      const state = current?.state || {};
      lastMessage = String(current?.ready?.message || lastMessage);
      if (current?.ready?.challenge?.detected) throw new Error(lastMessage);
      const cleared = Number(state.nodeCount || 0) === 0;
      const navigated = Boolean(state.url && before.url && state.url !== before.url);
      const replaced = Number(state.nodeCount || 0) < Number(before.nodeCount || 0)
        && String(state.lastBody || "") !== String(before.lastBody || "");
      let forcedLanding = false;
      if (forced?.targetUrl && state.url) {
        const target = new URL(forced.targetUrl);
        const actual = new URL(state.url);
        forcedLanding = target.origin === actual.origin
          && target.pathname.replace(/\/+$/, "") === actual.pathname.replace(/\/+$/, "")
          && Date.now() - forcedAt >= 1500;
      }
      if (current?.ready?.ok && (cleared || navigated || replaced || forcedLanding)) return;
    } catch (_) {
      // A full-page navigation temporarily removes the content script. The
      // next send retries injection and validation in the new conversation.
    }
    if (!forced && Date.now() - startedAt >= 6000) {
      try {
        forced = await sendToModel(model, { type: "GEO_FORCE_NEW_CONVERSATION" });
        forcedAt = Date.now();
        lastMessage = "新对话按钮未完成切换，已自动回到模型首页重建会话";
      } catch (_) {
        // The next loop retries after any in-progress navigation completes.
      }
    }
  }
  throw new Error(`${MODEL_NAMES[model]}新对话未在 35 秒内就绪：${lastMessage}`);
}

async function readiness() {
  const pairs = await Promise.all(MODEL_ORDER.map(async (model) => {
    try {
      const result = await sendToModel(model, { type: "GEO_CHECK_READY" });
      return [model, {
        ready: Boolean(result?.ok),
        message: `${String(result?.message || "")}；后台代码 ${WORKER_BUILD}`
      }];
    } catch (error) {
      return [model, { ready: false, message: `${String(error?.message || error)}；后台代码 ${WORKER_BUILD}` }];
    }
  }));
  return Object.fromEntries(pairs);
}

function schedule(task) {
  const questions = (task.questions || []).map(String);
  const rounds = Math.max(1, Number(task.rounds || 1));
  if (task.question_mode === "sequential") {
    return questions.flatMap((question) => Array.from({ length: rounds }, () => question));
  }
  return Array.from({ length: rounds }, () => questions).flat();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function compactText(value) {
  return String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, "");
}

function analyzeLocally(captured, task) {
  const body = String(captured?.body || "");
  const targets = [task.brand_name, task.product_name]
    .map((value) => String(value || "").trim())
    .filter(Boolean);
  const compactBody = compactText(body);
  const matchedTerms = targets.filter((target) => compactBody.includes(compactText(target)));
  const recommended = matchedTerms.length > 0;
  let rank = null;
  if (recommended) {
    for (const line of body.split(/\r?\n/)) {
      if (!targets.some((target) => compactText(line).includes(compactText(target)))) continue;
      const match = line.match(/^\s*(?:第\s*)?(\d{1,2})\s*(?:[.、)）:：]|名)/);
      if (match) {
        const value = Number(match[1]);
        if (value >= 1 && value <= 50) rank = value;
      }
      break;
    }
  }
  return {
    mode: "local_chrome_extension",
    version: 1,
    recommended,
    rank,
    matched_terms: matchedTerms,
    source_count: Array.isArray(captured?.sources) ? captured.sources.length : 0,
    analyzed_at: new Date().toISOString()
  };
}

async function requestId(taskId, model, index, question) {
  const bytes = new TextEncoder().encode([taskId, model, index, question].join("\0"));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return "ext-" + [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

async function heartbeat(config, task, payload) {
  return api(config, `/api/worker/tasks/${task.id}/heartbeat`, {
    method: "POST",
    body: JSON.stringify({ lease_token: task.lease_token, ...payload })
  });
}

async function finish(config, task, status, error = "") {
  return api(config, `/api/worker/tasks/${task.id}/finish`, {
    method: "POST",
    body: JSON.stringify({ lease_token: task.lease_token, status, error })
  });
}

function normalizedSourceUrl(rawValue, model) {
  try {
    let value = String(rawValue || "").replace(/\\u0026/gi, "&").replace(/\\\//g, "/").trim();
    let url = new URL(value);
    if (!/^https?:$/.test(url.protocol)) return null;
    const internalHosts = MODEL_INTERNAL_HOSTS[model] || [];
    const internal = (candidate) => {
      const host = candidate.hostname.replace(/^www\./, "").toLowerCase();
      return internalHosts.some((item) => host === item || host.endsWith(`.${item}`));
    };
    if (internal(url)) {
      for (const key of ["url", "target", "redirect", "redirect_url", "u", "href", "dest", "source_url"]) {
        const nested = url.searchParams.get(key);
        if (!nested) continue;
        try {
          const candidate = new URL(decodeURIComponent(nested));
          if (/^https?:$/.test(candidate.protocol)) {
            url = candidate;
            break;
          }
        } catch (_) {}
      }
    }
    if (internal(url)) return null;
    if (/\.(?:avif|bmp|css|gif|ico|jpe?g|js|mjs|png|svg|webp|woff2?|ttf)(?:$|[~?])/i.test(url.pathname + url.search)) return null;
    url.hash = "";
    return url.href;
  } catch (_) {
    return null;
  }
}

function extractStructuredSources(payload, model) {
  const urlKeys = new Set(["url", "href", "link", "sourceurl", "source_url", "pageurl", "referurl", "redirecturl", "targeturl"]);
  const titleKeys = ["title", "name", "text", "source", "site_name", "sitename", "abstract"];
  const seenObjects = new WeakSet();
  const sources = new Map();
  let visited = 0;
  const repairMojibake = (value) => {
    const text = String(value || "");
    if (!/[ÃÂäåæçèé]/.test(text)) return text;
    try {
      const windows1252 = {
        "€": 0x80, "‚": 0x82, "ƒ": 0x83, "„": 0x84, "…": 0x85, "†": 0x86,
        "‡": 0x87, "ˆ": 0x88, "‰": 0x89, "Š": 0x8a, "‹": 0x8b, "Œ": 0x8c,
        "Ž": 0x8e, "‘": 0x91, "’": 0x92, "“": 0x93, "”": 0x94, "•": 0x95,
        "–": 0x96, "—": 0x97, "˜": 0x98, "™": 0x99, "š": 0x9a, "›": 0x9b,
        "œ": 0x9c, "ž": 0x9e, "Ÿ": 0x9f
      };
      const bytes = Uint8Array.from([...text], (character) => {
        if (Object.hasOwn(windows1252, character)) return windows1252[character];
        const code = character.charCodeAt(0);
        if (code > 255) throw new Error("not Windows-1252");
        return code;
      });
      const decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      return /[\u3400-\u9fff]/.test(decoded) ? decoded : text;
    } catch (_) {
      return text;
    }
  };
  const add = (rawUrl, title = "") => {
    const url = normalizedSourceUrl(rawUrl, model);
    if (!url || sources.has(url)) return;
    let hostname = url;
    try { hostname = new URL(url).hostname; } catch (_) {}
    sources.set(url, { title: repairMojibake(title || hostname).replace(/\s+/g, " ").trim().slice(0, 300), url, href: url, capture_origin: "network_response" });
  };
  const walk = (value, depth = 0) => {
    if (depth > 14 || visited > 50000 || value == null) return;
    visited += 1;
    if (typeof value === "string") {
      const text = value.trim();
      if (text.length > 1 && text.length < 2_000_000 && (text.startsWith("{") || text.startsWith("["))) {
        try { walk(JSON.parse(text), depth + 1); } catch (_) {}
      }
      return;
    }
    if (typeof value !== "object" || seenObjects.has(value)) return;
    seenObjects.add(value);
    if (Array.isArray(value)) {
      value.forEach((item) => walk(item, depth + 1));
      return;
    }
    const title = titleKeys.map((key) => value[key]).find((item) => typeof item === "string") || "";
    for (const [key, item] of Object.entries(value)) {
      if (typeof item === "string" && urlKeys.has(key.toLowerCase())) add(item, title);
      walk(item, depth + 1);
    }
  };
  walk(payload);
  return [...sources.values()];
}

function extractResponseSources(body, model) {
  const sources = new Map();
  const addAll = (items) => items.forEach((item) => sources.set(item.url, item));
  const text = String(body || "");
  try { addAll(extractStructuredSources(JSON.parse(text), model)); } catch (_) {}
  for (const line of text.split(/\r?\n/)) {
    const candidate = line.replace(/^data:\s*/i, "").trim();
    if (!candidate || candidate === "[DONE]" || (!candidate.startsWith("{") && !candidate.startsWith("["))) continue;
    try { addAll(extractStructuredSources(JSON.parse(candidate), model)); } catch (_) {}
  }
  const decoded = text.replace(/\\u0026/gi, "&").replace(/\\\//g, "/");
  for (const match of decoded.matchAll(/https?:\/\/[^"'\s<>\\]+/g)) {
    const url = normalizedSourceUrl(match[0].replace(/[),.;\]}]+$/, ""), model);
    if (!url || sources.has(url)) continue;
    let title = url;
    try { title = new URL(url).hostname; } catch (_) {}
    sources.set(url, { title, url, href: url, capture_origin: "network_response" });
  }
  return [...sources.values()];
}

function extractResponseAnswer(body, question) {
  const candidates = [];
  const seenObjects = new WeakSet();
  let visited = 0;
  const add = (value) => {
    let text = String(value || "").replace(/<[^>]+>/g, " ").replace(/\\n/g, "\n").replace(/\s+/g, " ").trim();
    if (text.length < 20 || /^https?:\/\//i.test(text)) return;
    const normalizedText = compactText(text);
    const normalizedQuestion = compactText(question);
    if (!normalizedText || normalizedText === normalizedQuestion) return;
    if (normalizedText.includes(normalizedQuestion) && text.length <= question.length + 30) return;
    candidates.push(text);
  };
  const walk = (value, key = "", depth = 0) => {
    if (depth > 14 || visited > 50000 || value == null) return;
    visited += 1;
    if (typeof value === "string") {
      if (/^(text|content|answer|reply|output|markdown|message)$/i.test(key)) add(value);
      return;
    }
    if (typeof value !== "object" || seenObjects.has(value)) return;
    seenObjects.add(value);
    if (Array.isArray(value)) {
      value.forEach((item) => walk(item, key, depth + 1));
      return;
    }
    Object.entries(value).forEach(([childKey, item]) => walk(item, childKey, depth + 1));
  };
  const parse = (text) => {
    try { walk(JSON.parse(text)); } catch (_) {}
  };
  const text = String(body || "");
  parse(text);
  for (const line of text.split(/\r?\n/)) {
    const candidate = line.replace(/^data:\s*/i, "").trim();
    if (candidate && candidate !== "[DONE]" && (candidate.startsWith("{") || candidate.startsWith("["))) parse(candidate);
  }
  const unique = [...new Set(candidates)];
  unique.sort((left, right) => right.length - left.length);
  return unique[0] || "";
}

function validAnswerBody(body, question) {
  const text = String(body || "").replace(/\s+/g, " ").trim();
  const normalizedText = compactText(text);
  const normalizedQuestion = compactText(question);
  if (text.length < 60 || !normalizedText || normalizedText === normalizedQuestion) return false;
  if (normalizedText.includes(normalizedQuestion) && text.length <= question.length + 40) return false;
  if (text.length < 300 && /今天能帮你做什么|下载元宝电脑版|体验高效\s*AI\s*助手/.test(text)) return false;
  if (text.length < 300 && /Instant\s+AI Image\s+Writing\s+Solve\s+Deep Research|Presentation Maker\s+Data Insights\s+Investment Analysis/i.test(text)) return false;
  return true;
}

function isVerificationChallenge(error) {
  return /GEO_CHALLENGE_REQUIRED|网页安全验证|人机验证|图片验证|滑块验证/i.test(String(error?.message || error || ""));
}

async function reloadAfterChallenge(model) {
  const tab = await modelTab(model);
  if (!tab || !Number.isInteger(tab.id)) throw new Error(`请先打开并登录${MODEL_NAMES[model]}网页`);
  await chrome.tabs.reload(tab.id, { bypassCache: true });
  const deadline = Date.now() + 45000;
  let lastMessage = "刷新后等待页面就绪";
  while (Date.now() < deadline) {
    await sleep(1200);
    try {
      const ready = await sendToModel(model, { type: "GEO_CHECK_READY" });
      lastMessage = String(ready?.message || lastMessage);
      if (ready?.ok) return true;
    } catch (_) {}
  }
  return false;
}

async function runQuestionSilently(model, question, timeoutMs, stableMs = 6000) {
  const captured = await sendToModel(model, {
    type: "GEO_RUN_QUESTION",
    question,
    timeoutMs,
    stableMs
  });
  if (!captured?.ok) {
    throw new Error(captured?.error || `${MODEL_NAMES[model]}静默提问失败`);
  }
  const body = String(captured.body || "");
  if (!validAnswerBody(body, question)) {
    throw new Error(`${MODEL_NAMES[model]}未识别到有效回答正文：页面 ${body.length} 字`);
  }
  const sources = Array.isArray(captured.sources) ? captured.sources : [];
  const expectedSourceCount = Number(captured.expected_source_count || sources.length);
  return {
    ...captured,
    body,
    body_capture_complete: true,
    body_capture_origin: "page_dom",
    sources,
    expected_source_count: expectedSourceCount,
    source_capture_complete: Boolean(captured.source_capture_complete),
    source_capture_origins: { dom: sources.length, network: 0 }
  };
}

async function runModel(config, task, model, plan, control) {
  const completedRounds = new Set(
    (task.completed_rounds?.[model] || []).map((value) => Number(value)).filter(Number.isFinite)
  );
  for (let index = 0; index < plan.length; index += 1) {
    const roundNumber = index + 1;
    if (completedRounds.has(roundNumber)) continue;
    if (control.requested || control.pauseRequested) return;
    const question = plan[index];
    const progress = await heartbeat(config, task, {
      model,
      question,
      message: `${MODEL_NAMES[model]}正在执行第 ${index + 1}/${plan.length} 轮（三模型并行）`
    });
    if (progress.cancel_requested) {
      control.requested = true;
      return;
    }
    if (progress.pause_requested) {
      control.pauseRequested = true;
      return;
    }
    let captured = null;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        await setStatus({ model, message: `${MODEL_NAMES[model]}第 ${index + 1}/${plan.length} 轮正在创建全新对话` });
        await startFreshConversation(model);
        await setStatus({ model, message: `${MODEL_NAMES[model]}第 ${index + 1}/${plan.length} 轮正在静默提问并采集` });
        captured = await runQuestionSilently(
          model,
          question,
          Math.min(300, Math.max(60, Number(config.timeoutSeconds || 240))) * 1000,
          6000
        );
        break;
      } catch (error) {
        if (!isVerificationChallenge(error) || attempt >= 3) throw error;
        await setStatus({
          model,
          level: "warning",
          message: `${MODEL_NAMES[model]}出现安全验证，正在刷新并重新执行本轮（${attempt}/2）`
        });
        const recovered = await reloadAfterChallenge(model);
        if (!recovered) {
          await setStatus({
            model,
            level: "warning",
            message: `${MODEL_NAMES[model]}刷新后验证仍存在，准备再次刷新并重试本轮`
          });
        }
      }
    }
    if (!captured?.ok) throw new Error(captured?.error || `${MODEL_NAMES[model]}未返回回答`);
    const now = new Date().toISOString();
    const analysis = analyzeLocally(captured, task);
    const record = {
      collector_model: model,
      model_id: model,
      serial: `${model}-chrome-extension`,
      round: roundNumber,
      question,
      prompt: question,
      reply: String(captured.body || ""),
      web_body: String(captured.body || ""),
      sources: Array.isArray(captured.sources) ? captured.sources : [],
      analysis,
      recommended: analysis.recommended,
      rank: analysis.rank,
      status: "success",
      started_at: now,
      finished_at: now,
      capture_mode: "chrome_extension_logged_in_tab",
      capture_label: "Chrome 插件登录网页直采 · 本机分析",
      body_capture_complete: Boolean(captured.body_capture_complete),
      body_capture_origin: String(captured.body_capture_origin || "page_dom"),
      expected_source_count: Number(captured.expected_source_count || 0),
      source_capture_complete: Boolean(captured.source_capture_complete),
      source_capture_origins: captured.source_capture_origins || { dom: 0, network: 0 },
      page_url: String(captured.url || ""),
      remote_task_id: task.id,
      customer_slug: String(task.customer_slug || ""),
      brand_name: String(task.brand_name || ""),
      product_name: String(task.product_name || ""),
      task_kind: String(task.task_kind || "")
    };
    const uploaded = await api(config, `/api/worker/tasks/${task.id}/result`, {
      method: "POST",
      body: JSON.stringify({
        lease_token: task.lease_token,
        model_id: model,
        request_id: await requestId(task.id, model, roundNumber, question),
        record
      })
    });
    completedRounds.add(roundNumber);
    await setStatus({
      completed: Number(uploaded.completed_steps || 0),
      modelProgress: { [model]: { completed: completedRounds.size, total: plan.length } },
      message: `${MODEL_NAMES[model]}第 ${roundNumber}/${plan.length} 轮完成，本机分析已回传报告`
    });
    if (uploaded.cancel_requested) control.requested = true;
    if (uploaded.pause_requested) control.pauseRequested = true;
    if (index < plan.length - 1 && !control.requested && !control.pauseRequested) {
      await sleep(Math.max(3, Number(config.intervalSeconds || 15)) * 1000);
    }
  }
}

async function runTask(config, task) {
  executing = true;
  const plan = schedule(task);
  const selected = new Set(task.models || []);
  const models = MODEL_ORDER.filter((item) => selected.has(item));
  await setStatus({
    phase: "running",
    taskId: task.id,
    completed: Number(task.completed_steps || 0),
    total: Number(task.total_steps || 0),
    modelProgress: Object.fromEntries(models.map((model) => [model, {
      completed: Number(task.model_progress?.[model]?.completed || 0),
      total: plan.length
    }])),
    message: Number(task.completed_steps || 0) > 0
      ? "已恢复任务，三模型从断点继续并行诊断"
      : "豆包、腾讯元宝、文心一言开始并行诊断"
  });
  try {
    const control = { requested: false, pauseRequested: false };
    const outcomes = await Promise.allSettled(
      models.map((model) => runModel(config, task, model, plan, control))
    );
    if (control.requested) {
      await finish(config, task, "cancelled");
      await setStatus({ phase: "idle", message: "任务已取消", taskId: "" });
      return;
    }
    if (control.pauseRequested) {
      await finish(config, task, "paused");
      await setStatus({ phase: "idle", message: "任务已暂停，点击继续后将从断点执行", taskId: "" });
      return;
    }
    const failures = outcomes
      .map((outcome, index) => outcome.status === "rejected" ? `${MODEL_NAMES[models[index]]}：${String(outcome.reason?.message || outcome.reason)}` : "")
      .filter(Boolean);
    if (failures.length) throw new Error(failures.join("；"));
    await finish(config, task, "completed");
    await setStatus({ phase: "idle", taskId: "", message: "三模型并行诊断已完成" });
  } catch (error) {
    const message = String(error?.message || error);
    try { await finish(config, task, "failed", `ChromeExtensionError: ${message}`); } catch (_) {}
    await setStatus({ phase: "error", message, error: message });
  } finally {
    executing = false;
  }
}

async function poll() {
  if (polling || executing) return;
  polling = true;
  try {
    const state = await session();
    if (!state.running || !state.config?.token || !state.config?.server) return;
    const ready = await readiness();
    await setStatus({ phase: "connected", readiness: ready, message: "已连接服务器，等待任务", error: "" });
    const response = await api(state.config, "/api/worker/claim", {
      method: "POST",
      body: JSON.stringify({ worker_id: state.config.workerId || "chrome-extension", readiness: ready })
    });
    if (response.task) await runTask(state.config, response.task);
  } catch (error) {
    await setStatus({ phase: "error", message: String(error?.message || error), error: String(error?.message || error) });
  } finally {
    polling = false;
    const state = await session();
    if (state.running && !executing) setTimeout(poll, 5000);
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "GEO_START") {
    const config = {
      server: String(message.config?.server || "https://www.ifbcy.com/geo"),
      token: String(message.config?.token || ""),
      workerId: String(message.config?.workerId || "chrome-office"),
      intervalSeconds: Math.max(3, Number(message.config?.intervalSeconds || 15)),
      timeoutSeconds: Math.max(30, Number(message.config?.timeoutSeconds || 240))
    };
    Promise.all([
      chrome.storage.local.set({ savedConfig: config, autoStart: true }),
      chrome.storage.session.set({ config, running: true })
    ]).then(poll).then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "GEO_STOP") {
    Promise.all([
      chrome.storage.local.set({ autoStart: false }),
      chrome.storage.session.set({ running: false })
    ]).then(() => setStatus({ phase: "stopped", message: "插件采集已停止；密钥仍保存在本机" })).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === "GEO_FORGET_CONFIG") {
    Promise.all([
      chrome.storage.local.remove(["savedConfig", "autoStart"]),
      chrome.storage.session.remove(["config", "running"])
    ]).then(() => setStatus({ phase: "stopped", message: "已清除本机保存的 Worker 配置" })).then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "GEO_GET_STATUS") {
    session().then((value) => {
      if (sender.tab) {
        sendResponse({
          ok: true,
          running: Boolean(value.running),
          hasConfig: Boolean(value.config?.token),
          status: value.status || {}
        });
        return;
      }
      sendResponse({ ok: true, ...value, hasConfig: Boolean(value.config?.token) });
    });
    return true;
  }
  if (message?.type === "GEO_OPEN_SETTINGS") {
    chrome.runtime.openOptionsPage().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "GEO_OPEN_TASKS") {
    openTaskManager().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "GEO_GET_ADAPTIVE_ELEMENTS") {
    chrome.storage.local.get(ADAPTIVE_STORAGE_KEY).then((value) => sendResponse({ ok: true, elements: value?.[ADAPTIVE_STORAGE_KEY] || {} })).catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "GEO_SAVE_ADAPTIVE_ELEMENT") {
    saveAdaptiveElement(String(message.model || ""), String(message.identifier || ""), message.fingerprint)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "GEO_PAGE_STATUS") {
    chrome.storage.session.get("status").then(({ status = {} }) => {
      const model = String(message.model || "");
      if (!MODEL_ORDER.includes(model)) return;
      return setStatus({
        readiness: {
          ...(status.readiness || {}),
          [model]: {
            ready: Boolean(message.ready?.ok),
            message: String(message.ready?.message || "")
          }
        },
        log: false
      });
    }).then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "GEO_POLL") {
    poll().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

chrome.alarms.create("geo-monitor-poll", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "geo-monitor-poll") poll();
});
chrome.runtime.onInstalled.addListener(() => injectKnownTabs().then(poll));
chrome.runtime.onStartup.addListener(() => injectKnownTabs().then(poll));
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "loading" || changeInfo.url) injectTab(tabId, changeInfo.url || tab.url);
});
injectKnownTabs().then(poll).catch(() => {});
