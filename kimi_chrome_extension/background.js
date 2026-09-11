"use strict";

const DEFAULT_QUESTIONS = [
  "推荐一款护发精油", "推荐一款护发素", "推荐一款控油蓬松洗发水", "推荐一款沐浴精油",
  "推荐一款眉毛增长液", "推荐一款祛痘精华液", "推荐一款美白面霜", "推荐一款造型喷雾",
  "推荐一款染发剂", "推荐一款睫毛增长液", "推荐一款防脱洗发水", "推荐一款防脱精华液", "推荐一款面膜"
];
const SETTINGS_SCHEMA_VERSION = 3;
const DEFAULT_SETTINGS = {
  questions: DEFAULT_QUESTIONS,
  rounds: 100,
  questionMode: "interleaved",
  intervalMinSeconds: 15,
  intervalMaxSeconds: 45,
  timeoutSeconds: 240,
  stableSeconds: 10,
  minAnswerLength: 60,
  maxRetries: 3,
  retryDelaySeconds: 20,
  receiverUrl: "http://127.0.0.1:8767",
  targetUrl: "https://www.kimi.com/"
};

const SUPPORTED = [
  "https://*.kimi.com/*", "https://kimi.com/*"
];

function storageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}

function storageSet(value) {
  return new Promise((resolve) => chrome.storage.local.set(value, resolve));
}

function tabsQuery(query) {
  return new Promise((resolve) => chrome.tabs.query(query, resolve));
}

function sendToTab(tabId, payload) {
  return new Promise((resolve) => chrome.tabs.sendMessage(tabId, payload, (response) => {
    if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
    else resolve(response || { ok: true });
  }));
}

function matchesTarget(url) {
  try {
    const parsed = new URL(url || "");
    const host = parsed.hostname.toLowerCase();
    return host === "kimi.com" || host.endsWith(".kimi.com");
  } catch (_) {
    return false;
  }
}

async function settings() {
  const data = await storageGet(["settings"]);
  return { ...DEFAULT_SETTINGS, ...(data.settings || {}) };
}

async function findTargetTab() {
  const active = await tabsQuery({ active: true, currentWindow: true });
  if (active[0] && matchesTarget(active[0].url)) return active[0];
  const tabs = await tabsQuery({ url: SUPPORTED });
  return tabs.find((tab) => matchesTarget(tab.url)) || null;
}

async function injectIntoTab(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["injected.js"], world: "MAIN", injectImmediately: true });
    await chrome.scripting.executeScript({ target: { tabId }, files: ["core.js", "adaptive.js", "content.js"], world: "ISOLATED", injectImmediately: true });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  }
}

async function updateBadge(job) {
  let text = "";
  let color = "#475569";
  if (job?.state === "running") { text = String(Math.min(999, (job.cursor || 0) + 1)); color = "#16a34a"; }
  if (job?.state === "paused") { text = "停"; color = "#d97706"; }
  if (job?.state === "error") { text = "!"; color = "#dc2626"; }
  if (job?.state === "completed") { text = "✓"; color = "#2563eb"; }
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
}

async function startJob(request) {
  const nextSettings = { ...DEFAULT_SETTINGS, ...(request.settings || {}) };
  nextSettings.questions = Array.from(new Set((nextSettings.questions || []).map((value) => String(value || "").trim()).filter(Boolean)));
  if (!nextSettings.questions.length) throw new Error("请至少填写一个问题");
  const tab = await findTargetTab();
  if (!tab?.id) throw new Error("请先在 Chrome 打开Kimi网页版对话页面，再点击开始");
  let probe = await sendToTab(tab.id, { type: "QM_PROBE" });
  if (!probe.ok) {
    await injectIntoTab(tab.id);
    probe = await sendToTab(tab.id, { type: "QM_PROBE" });
  }
  if (!probe.ok) throw new Error("当前页面尚未加载监控插件，请刷新Kimi页面后重试");
  if (probe.loginRequired || !probe.hasComposer || !probe.hasNewChat) {
    if (probe.loginRequired) throw new Error("Kimi 账号尚未登录，请先完成登录");
    const missing = [!probe.hasComposer ? "Kimi输入框" : "", !probe.hasNewChat ? "“新对话”控件" : ""].filter(Boolean).join("和");
    throw new Error(`页面检测未通过：未找到${missing}。请确认已登录Kimi并停留在对话首页，然后刷新页面重试。`);
  }
  const schedule = buildSchedule(nextSettings.questions, nextSettings.rounds, nextSettings.questionMode);
  const requestedJobId = String(request.jobId || "").trim();
  const existingData = await storageGet(["job", "results", "pendingSync"]);
  const existing = existingData.job || null;
  const sameSchedule = existing && JSON.stringify(existing.schedule || []) === JSON.stringify(schedule);
  const existingResults = (existingData.results || []).filter((item) => item.run_id === requestedJobId);
  const completedSuccessfully = existing?.state === "completed" &&
    existingResults.length === schedule.length && existingResults.every((item) => item.status !== "failed");
  if (requestedJobId && existing?.id === requestedJobId && sameSchedule &&
      (["running", "paused"].includes(existing.state) || completedSuccessfully)) {
    const resumed = existing.state === "paused" ? { ...existing, state: "running" } : existing;
    await storageSet({ settings: nextSettings, job: resumed });
    await updateBadge(resumed);
    if (resumed.state === "running") await sendToTab(tab.id, { type: "QM_RUN" });
    return { ok: true, job: resumed, resumed: true, tab: { id: tab.id, url: tab.url, title: tab.title } };
  }
  if (requestedJobId) {
    await storageSet({
      results: (existingData.results || []).filter((item) => item.run_id !== requestedJobId),
      pendingSync: (existingData.pendingSync || []).filter((item) => item.run_id !== requestedJobId)
    });
  }
  const job = {
    id: requestedJobId || crypto.randomUUID(),
    state: "running",
    cursor: 0,
    schedule,
    inFlight: null,
    ownerTabId: tab.id,
    startedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    lastError: ""
  };
  await storageSet({ settings: nextSettings, job });
  await updateBadge(job);
  await sendToTab(tab.id, { type: "QM_RUN" });
  return { ok: true, job, tab: { id: tab.id, url: tab.url, title: tab.title } };
}

function buildSchedule(questions, rounds, mode) {
  const count = Math.max(1, Math.min(Number(rounds) || 1, 10000));
  const output = [];
  if (mode === "sequential") {
    questions.forEach((prompt, questionIndex) => {
      for (let round = 1; round <= count; round += 1) output.push({ prompt, questionIndex, questionRound: round });
    });
  } else {
    for (let round = 1; round <= count; round += 1) {
      questions.forEach((prompt, questionIndex) => output.push({ prompt, questionIndex, questionRound: round }));
    }
  }
  return output.map((item, globalIndex) => ({ ...item, globalIndex }));
}

async function mutateJob(mutator) {
  const data = await storageGet(["job"]);
  const job = data.job ? { ...data.job } : null;
  const next = await mutator(job);
  if (next) {
    next.updatedAt = new Date().toISOString();
    await storageSet({ job: next });
    await updateBadge(next);
  }
  return next;
}

async function postResult(result) {
  const currentSettings = await settings();
  const url = String(currentSettings.receiverUrl || "").replace(/\/$/, "") + "/api/results";
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) {
    return { ok: false, error: "接收地址必须是本机 127.0.0.1 或 localhost" };
  }
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result)
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  }
}

async function postEvent(event) {
  const currentSettings = await settings();
  const url = String(currentSettings.receiverUrl || "").replace(/\/$/, "") + "/api/events";
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) return { ok: false, error: "invalid_local_receiver" };
  try {
    const response = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(event)
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error && error.message || error) };
  }
}

async function receiverGet(path) {
  const currentSettings = await settings();
  const url = String(currentSettings.receiverUrl || "").replace(/\/$/, "") + path;
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//i.test(url)) throw new Error("invalid_local_receiver");
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error("HTTP " + response.status);
  return response.json();
}

async function logEvent(request) {
  const data = await storageGet(["job", "eventLogs"]);
  const job = data.job || {};
  const item = job.schedule?.[job.cursor] || job.inFlight || {};
  const event = {
    timestamp: new Date().toISOString(), level: request.level || "info", event: request.event || "event",
    run_id: job.id || "", round: Number(item.globalIndex ?? job.cursor ?? 0) + 1,
    prompt: item.prompt || "", page_tab_id: request.tabId || null, details: request.details || {}
  };
  const logs = Array.isArray(data.eventLogs) ? data.eventLogs : [];
  logs.push(event);
  await storageSet({ eventLogs: logs.slice(-5000) });
  const posted = await postEvent(event);
  return { ok: true, posted: posted.ok, event };
}

async function storeResult(result) {
  const data = await storageGet(["results", "pendingSync"]);
  const results = Array.isArray(data.results) ? data.results : [];
  if (!results.some((item) => item.result_id === result.result_id)) results.push(result);
  const sync = await postResult(result);
  const pending = Array.isArray(data.pendingSync) ? data.pendingSync.filter((item) => item.result_id !== result.result_id) : [];
  if (!sync.ok) pending.push(result);
  await storageSet({ results, pendingSync: pending.slice(-5000), lastReceiverError: sync.ok ? "" : sync.error });
  const job = await mutateJob((current) => {
    if (!current) return current;
    if (current.inFlight && current.inFlight.globalIndex !== result.round - 1) return current;
    current.cursor = Math.max(current.cursor || 0, result.round);
    current.inFlight = null;
    current.lastError = "";
    if (current.cursor >= current.schedule.length) {
      current.state = "completed";
      current.finishedAt = new Date().toISOString();
    }
    return current;
  });
  return { ok: true, synced: sync.ok, job };
}

async function retryPending() {
  const data = await storageGet(["pendingSync"]);
  const pending = Array.isArray(data.pendingSync) ? data.pendingSync : [];
  if (!pending.length) return;
  const remaining = [];
  for (const result of pending.slice(0, 50)) {
    const sync = await postResult(result);
    if (!sync.ok) remaining.push(result);
  }
  remaining.push(...pending.slice(50));
  await storageSet({ pendingSync: remaining, lastReceiverError: remaining.length ? "本地接收器暂时不可用" : "" });
}

async function initializeStorage() {
  const data = await storageGet(["settings", "results", "pendingSync", "eventLogs", "settingsSchemaVersion", "job", "lastReceiverError"]);
  const resetForRemoteRun = Number(data.settingsSchemaVersion || 0) < SETTINGS_SCHEMA_VERSION;
  await storageSet({
    settings: resetForRemoteRun ? { ...DEFAULT_SETTINGS } : { ...DEFAULT_SETTINGS, ...(data.settings || {}) },
    settingsSchemaVersion: SETTINGS_SCHEMA_VERSION,
    results: resetForRemoteRun ? [] : (Array.isArray(data.results) ? data.results : []),
    pendingSync: resetForRemoteRun ? [] : (Array.isArray(data.pendingSync) ? data.pendingSync : []),
    eventLogs: resetForRemoteRun ? [] : (Array.isArray(data.eventLogs) ? data.eventLogs : []),
    job: resetForRemoteRun ? null : (data.job || null),
    lastReceiverError: resetForRemoteRun ? "" : (data.lastReceiverError || "")
  });
  chrome.alarms.create("retry-local-sync", { periodInMinutes: 1 });
  return { resetForRemoteRun };
}

const initialization = initializeStorage();

chrome.runtime.onInstalled.addListener(async () => {
  await initialization;
  const tabs = await tabsQuery({ url: SUPPORTED });
  await Promise.all(tabs.filter((tab) => tab.id).map((tab) => injectIntoTab(tab.id)));
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "retry-local-sync") retryPending();
});

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  (async () => {
    await initialization;
    switch (request?.type) {
      case "START_JOB": return startJob(request);
      case "GET_CONTEXT": {
        const data = await storageGet(["settings", "job", "results", "pendingSync", "lastReceiverError"]);
        let receiverHealth = null;
        try { receiverHealth = await receiverGet("/api/health"); } catch (_) {}
        return { ok: true, settings: { ...DEFAULT_SETTINGS, ...(data.settings || {}) }, job: data.job || null,
          resultCount: receiverHealth ? Number(receiverHealth.result_count || 0) : (data.results || []).length,
          pendingCount: receiverHealth ? Number(receiverHealth.remote_sync?.pending || 0) : (data.pendingSync || []).length,
          lastReceiverError: data.lastReceiverError || "" };
      }
      case "PROBE_READY": {
        const tab = await findTargetTab();
        if (!tab?.id) return { ok: true, ready: false, message: "请先打开 Kimi 网页" };
        let probe = await sendToTab(tab.id, { type: "QM_PROBE" });
        if (!probe.ok) {
          await injectIntoTab(tab.id);
          probe = await sendToTab(tab.id, { type: "QM_PROBE" });
        }
        const ready = Boolean(probe.ok && !probe.loginRequired && probe.hasComposer && probe.hasNewChat);
        return { ok: true, ready, probe, message: ready
          ? "Kimi Chrome 插件已登录并就绪"
          : "请在 Kimi 插件专用 Chrome 中完成登录并停留在对话页" };
      }
      case "GET_JOB": {
        const data = await storageGet(["settings", "job"]);
        return { ok: true, settings: { ...DEFAULT_SETTINGS, ...(data.settings || {}) }, job: data.job || null };
      }
      case "GET_ALL_RESULTS": {
        const data = await storageGet(["results"]);
        try {
          const local = await receiverGet("/api/results?limit=5000");
          return { ok: true, results: local.results || [], source: "local_receiver" };
        } catch (_) {
          return { ok: true, results: data.results || [], source: "extension_storage" };
        }
      }
      case "GET_EVENT_LOGS": {
        const data = await storageGet(["eventLogs"]);
        try {
          const local = await receiverGet("/api/events");
          return { ok: true, events: local.events || [], source: "local_receiver" };
        } catch (_) {
          return { ok: true, events: data.eventLogs || [], source: "extension_storage" };
        }
      }
      case "LOG_EVENT": return logEvent({ ...request, tabId: sender.tab?.id || null });
      case "CLAIM_RUNNER": {
        const tabId = sender.tab?.id;
        const job = await mutateJob((current) => {
          if (!current || !["running", "paused"].includes(current.state)) return current;
          if (!current.ownerTabId) current.ownerTabId = tabId;
          return current;
        });
        return { ok: true, claimed: Boolean(job && tabId && job.ownerTabId === tabId), job, settings: await settings() };
      }
      case "SET_IN_FLIGHT": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, inFlight: request.inFlight }) : job) };
      case "STORE_RESULT": return storeResult(request.result);
      case "PAUSE_JOB": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, state: "paused" }) : job) };
      case "RESUME_JOB": {
        const job = await mutateJob((current) => {
          if (!current) return current;
          const finished = Number(current.cursor || 0) >= Number(current.schedule?.length || 0);
          return finished
            ? { ...current, state: "completed", inFlight: null, lastError: "", finishedAt: current.finishedAt || new Date().toISOString() }
            : { ...current, state: "running", lastError: "" };
        });
        if (job?.state === "running" && job?.ownerTabId) await sendToTab(job.ownerTabId, { type: "QM_RUN" });
        return { ok: true, job };
      }
      case "STOP_JOB": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, state: "stopped" }) : job) };
      case "JOB_ERROR": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, state: "error", lastError: request.error || "未知错误" }) : job) };
      case "COMPLETE_JOB": return { ok: true, job: await mutateJob((job) => job ? ({ ...job, state: "completed", finishedAt: new Date().toISOString() }) : job) };
      case "SAVE_SETTINGS": {
        const value = { ...DEFAULT_SETTINGS, ...(request.settings || {}) };
        await storageSet({ settings: value });
        return { ok: true, settings: value };
      }
      case "OPEN_TARGET": {
        const tab = await chrome.tabs.create({ url: request.url || (await settings()).targetUrl || "https://www.kimi.com/" });
        return { ok: true, tabId: tab.id };
      }
      case "RETRY_SYNC": await retryPending(); return { ok: true };
      default: return { ok: false, error: "unknown_message" };
    }
  })().then(sendResponse).catch((error) => sendResponse({ ok: false, error: String(error && error.message || error) }));
  return true;
});
