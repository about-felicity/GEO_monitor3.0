"use strict";

const $ = (id) => document.getElementById(id);
let latestContext = null;
let resultCache = [];
let logCache = [];
let activePane = "taskPane";
const ALL_QUESTIONS = [
  "推荐一款护发精油", "推荐一款护发素", "推荐一款控油蓬松洗发水", "推荐一款沐浴精油",
  "推荐一款眉毛增长液", "推荐一款祛痘精华液", "推荐一款美白面霜", "推荐一款造型喷雾",
  "推荐一款染发剂", "推荐一款睫毛增长液", "推荐一款防脱洗发水", "推荐一款防脱精华液", "推荐一款面膜"
];
$("extensionVersion").textContent = chrome.runtime.getManifest().version;

function call(payload) {
  return new Promise((resolve) => chrome.runtime.sendMessage(payload, (response) => {
    if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
    else resolve(response || { ok: false, error: "无响应" });
  }));
}

function readSettings() {
  return {
    questions: $("questions").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
    rounds: Number($("rounds").value) || 1,
    questionMode: $("questionMode").value,
    intervalMinSeconds: Number($("intervalMin").value) || 15,
    intervalMaxSeconds: Number($("intervalMax").value) || 45,
    timeoutSeconds: Number($("timeout").value) || 240,
    stableSeconds: 10,
    minAnswerLength: 60,
    maxRetries: Math.max(0, Number($("maxRetries").value) || 3),
    retryDelaySeconds: Math.max(5, Number($("retryDelay").value) || 20),
    receiverUrl: $("receiverUrl").value.trim(),
    targetUrl: $("targetUrl").value.trim()
  };
}

function showError(value) {
  $("errorBox").textContent = value || "";
  $("errorBox").classList.toggle("show", Boolean(value));
}

function updateQuestionCount() {
  const count = $("questions").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean).length;
  $("questionCountHint").textContent = `已加载 ${count} / ${ALL_QUESTIONS.length} 个问题`;
  $("questionCountHint").classList.toggle("complete", count === ALL_QUESTIONS.length);
}

function toast(value) {
  $("toast").textContent = value;
  $("toast").classList.add("show");
  setTimeout(() => $("toast").classList.remove("show"), 1800);
}

function renderContext(context, populate) {
  latestContext = context;
  const settings = context.settings || {};
  const job = context.job;
  if (populate) {
    $("questions").value = (settings.questions || []).join("\n");
    $("rounds").value = settings.rounds || 1;
    $("questionMode").value = settings.questionMode || "interleaved";
    $("intervalMin").value = settings.intervalMinSeconds || 15;
    $("intervalMax").value = settings.intervalMaxSeconds || 45;
    $("timeout").value = settings.timeoutSeconds || 240;
    $("maxRetries").value = settings.maxRetries ?? 3;
    $("retryDelay").value = settings.retryDelaySeconds || 20;
    $("receiverUrl").value = settings.receiverUrl || "http://127.0.0.1:8767";
    $("targetUrl").value = settings.targetUrl || "https://www.kimi.com/";
    updateQuestionCount();
  }
  const labels = { running: "运行中", paused: "已暂停", error: "需要处理", completed: "已完成", stopped: "已停止" };
  const stateLabel = labels[job?.state] || "未开始";
  $("statusBadge").textContent = stateLabel;
  $("statusBadge").className = "badge " + (job?.state || "");
  $("headerStatus").textContent = stateLabel;
  $("headerStatus").className = "badge " + (job?.state || "");
  const total = job?.schedule?.length || 0;
  const cursor = Math.min(job?.cursor || 0, total);
  $("progressText").textContent = `${cursor} / ${total}`;
  $("progressBar").style.width = total ? `${cursor / total * 100}%` : "0";
  $("resultCount").textContent = context.resultCount || 0;
  $("resultTabCount").textContent = context.resultCount || 0;
  $("pendingCount").textContent = context.pendingCount || 0;
  $("currentRound").textContent = job?.state === "running" && cursor < total ? String(cursor + 1) : "-";
  showError(job?.lastError || context.lastReceiverError || "");
  $("pause").disabled = job?.state !== "running";
  $("resume").disabled = !["paused", "error"].includes(job?.state);
  $("stop").disabled = !["running", "paused", "error"].includes(job?.state);
}

async function refreshContext(populate) {
  const context = await call({ type: "GET_CONTEXT" });
  if (!context.ok) return showError(context.error);
  renderContext(context, populate);
}

function makeButton(text, handler, className) {
  const button = document.createElement("button");
  button.textContent = text;
  if (className) button.className = className;
  button.addEventListener("click", handler);
  return button;
}

function sourceLine(source, index) {
  const row = document.createElement("div");
  row.className = "sourceLine";
  const number = document.createElement("span");
  number.className = "sourceNumber";
  number.textContent = String(index + 1);
  const link = document.createElement("a");
  link.href = source.url;
  link.target = "_blank";
  link.rel = "noreferrer";
  const rawTitle = String(source.title || "").trim();
  let domain = String(source.domain || "").replace(/^www\./, "").toLowerCase();
  try { domain = domain || new URL(source.url).hostname.replace(/^www\./, "").toLowerCase(); } catch (_) {}
  const compactTitle = rawTitle.replace(/^https?:\/\//i, "").replace(/^www\./i, "").replace(/\/$/, "").toLowerCase();
  link.textContent = !rawTitle || compactTitle === domain || /^https?:\/\//i.test(rawTitle) ? "标题未获取" : rawTitle;
  link.title = source.url;
  row.append(number, link);
  return row;
}

function renderResults() {
  const container = $("resultCards");
  container.replaceChildren();
  $("resultsEmpty").style.display = resultCache.length ? "none" : "block";
  const totalSources = resultCache.reduce((sum, item) => sum + (item.sources || []).length, 0);
  const failedResults = resultCache.filter((item) => item.status && item.status !== "success").length;
  $("resultsSummary").textContent = `${resultCache.length - failedResults} 条正文 · ${failedResults} 条失败 · ${totalSources} 条正式信源`;
  for (const result of resultCache.slice().reverse().slice(0, 100)) {
    const card = document.createElement("article");
    card.className = "card resultCard";
    const head = document.createElement("div");
    head.className = "resultHead";
    const titleWrap = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = result.prompt || "未命名问题";
    const meta = document.createElement("div");
    meta.className = "subtle";
    meta.textContent = `总第 ${result.round || "-"} 轮 · 本题第 ${result.question_round || "-"} 轮 · ${result.detected_model || ""}`;
    titleWrap.append(title, meta);
    const complete = document.createElement("span");
    const failed = result.status && result.status !== "success";
    complete.className = "badge " + (failed ? "error" : (result.source_capture_complete ? "completed" : "paused"));
    complete.textContent = failed ? "本轮失败" : `${(result.sources || []).length}/${result.expected_source_count || 0} 信源`;
    head.append(titleWrap, complete);

    const answerTitle = document.createElement("div");
    answerTitle.className = "resultSectionTitle";
    answerTitle.textContent = failed ? "本轮失败原因" : `模型回答正文（${String(result.reply || "").length} 字）`;
    const answer = document.createElement("pre");
    answer.className = "resultReply";
    answer.textContent = failed ? (result.skip_reason || "本轮未获得回答") : (result.reply || "");
    const answerActions = document.createElement("div");
    answerActions.className = "miniActions";
    answerActions.append(makeButton("复制正文", async () => { await navigator.clipboard.writeText(result.reply || ""); toast("正文已复制"); }));
    if (result.page_url) answerActions.append(makeButton("打开对话", () => chrome.tabs.create({ url: result.page_url })));

    const sourceTitle = document.createElement("div");
    sourceTitle.className = "resultSectionTitle";
    sourceTitle.textContent = "信源链接";
    const sources = document.createElement("div");
    sources.className = "sourceList";
    (result.sources || []).forEach((source, index) => sources.append(sourceLine(source, index)));
    const sourceActions = document.createElement("div");
    sourceActions.className = "miniActions";
    sourceActions.append(makeButton("复制全部链接", async () => {
      await navigator.clipboard.writeText((result.sources || []).map((source) => source.url).join("\n"));
      toast("信源链接已复制");
    }));
    sourceActions.append(makeButton("复制标题+链接", async () => {
      await navigator.clipboard.writeText((result.sources || []).map((source, index) =>
        `${index + 1}. ${source.title || "标题未获取"}\n   ${source.url}`
      ).join("\n"));
      toast("信源标题和链接已复制");
    }));
    card.append(head, answerTitle, answer, answerActions, sourceTitle, sources, sourceActions);
    container.append(card);
  }
}

async function loadResults() {
  $("resultsSummary").textContent = "读取中…";
  const response = await call({ type: "GET_ALL_RESULTS" });
  resultCache = response.results || [];
  $("resultsSource").textContent = response.source === "local_receiver" ? "本机规范化结果" : "插件本地备份（接收器未连接）";
  renderResults();
}

const EVENT_LABELS = {
  new_chat_click: "正在新建对话", old_draft_cleared: "旧草稿已清除", prompt_prepared: "问题已写入",
  prompt_submitted: "问题已发送", answer_stable: "回答已经完成", capture_complete: "正文与信源已采集",
  result_saved: "结果已保存到本机", round_error: "本轮出现异常", historical_result_verified: "历史结果校验完成",
  existing_results_normalized: "历史数据整理完成", source_titles_repaired: "信源标题修复完成", logging_ready: "日志系统已就绪",
  local_only_ready: "本机存储模式已启用"
  , remote_sync_started: "远端回传已启动", remote_sync_queued: "结果等待回传",
  remote_sync_success: "结果回传成功", remote_sync_retry: "远端回传等待重试",
  remote_sync_disabled: "远端回传未启用", remote_sync_host_ready: "回传主机已连接",
  remote_sync_host_waiting: "回传主机暂时不可达", round_retry_scheduled: "本轮将自动重试",
  answer_wait_diagnostic: "正在检查回答状态", answer_ui_state_fallback: "页面状态卡住，已保存正文",
  answer_timeout_fallback: "等待超时，已保留正文", empty_answer_detected: "Kimi未生成回答",
  page_reload_for_retry: "正在刷新页面后重试", round_failed_continuing: "本轮失败，继续后续任务",
  send_unconfirmed: "发送未确认，准备自动恢复"
  , adaptive_ui_match: "页面结构变化，已自适应定位"
};

const DETAIL_LABELS = {
  reply_chars: "正文字数", source_count: "已抓取信源", expected_source_count: "页面显示信源",
  source_capture_complete: "信源是否完整", stable_polls: "稳定检测次数", raw_source_count: "原始链接候选",
  result_count: "结果数量", sources: "信源总数", results: "结果总数", version: "插件版本",
  draft_length: "旧草稿字数", page_url: "对话地址", previous_url: "上一页地址", result_id: "结果编号",
  json_path: "JSON 文件", text_path: "TXT 文件", results_path: "结果文件", backup_path: "备份文件",
  error: "错误原因", prompt: "问题"
  , pending: "待回传数量", retry_seconds: "重试间隔/秒", receiver_url: "接收主机",
  receiver_urls: "接收主机列表", status: "主机状态", request_id: "回传编号"
  , retry_attempt: "第几次重试", max_retries: "最多重试次数", generation_busy: "页面显示正在生成",
  has_candidate: "是否找到正文", selector_hint: "正文区域",
  empty_seconds: "空白等待/秒", body_chars: "页面文字数", prompt_visible: "问题是否仍在页面",
  retry_count: "已重试次数", next_round: "下一轮",
  ui_role: "控件角色", similarity_score: "相似度得分", tag: "元素标签", test_id: "测试标识",
  real_title_count: "真实标题数量", missing_title_count: "尚缺标题数量", unique_title_count: "不同链接标题数"
};

const SUCCESS_EVENTS = new Set(["answer_stable", "capture_complete", "result_saved", "historical_result_verified", "existing_results_normalized", "source_titles_repaired", "logging_ready", "local_only_ready", "remote_sync_started", "remote_sync_success"]);

function countText(value, unit = "") {
  return Number.isFinite(Number(value)) ? `${Number(value)}${unit}` : "未知";
}

function eventSummary(event) {
  const d = event.details || {};
  const sourceProgress = `${countText(d.source_count)}/${countText(d.expected_source_count)}`;
  switch (event.event) {
    case "new_chat_click": return "已点击“新对话”，正在准备本轮问题。";
    case "old_draft_cleared": return `检测到旧草稿并已自动清除（${countText(d.draft_length, " 字")}）。`;
    case "prompt_prepared": return "问题已写入输入框并校验通过，准备发送。";
    case "prompt_submitted": return "问题发送成功，正在等待模型生成回答。";
    case "answer_stable": return `模型回答已完成并保持稳定，共 ${countText(d.reply_chars, " 字")}，准备采集信源。`;
    case "capture_complete": return d.source_capture_complete
      ? `采集完成：正文 ${countText(d.reply_chars, " 字")}，信源 ${sourceProgress}，数量完整。`
      : `采集完成：正文 ${countText(d.reply_chars, " 字")}，但信源仅 ${sourceProgress}，请留意。`;
    case "result_saved": return d.source_capture_complete
      ? `已写入本机：正文 ${countText(d.reply_chars, " 字")}，信源 ${sourceProgress}。`
      : `已写入本机，但信源只有 ${sourceProgress}。`;
    case "round_error": return `本轮已停止：${d.error || "未知错误"}。可回到“任务”页查看红色提示后重试。`;
    case "historical_result_verified": return `历史结果正常：正文 ${countText(d.reply_chars, " 字")}，信源 ${sourceProgress}。`;
    case "existing_results_normalized": return `已整理 ${countText(d.result_count, " 条")}历史结果，并保留原始备份。`;
    case "source_titles_repaired": return `信源标题已修复：${countText(d.real_title_count, " 条")}已显示文章/视频标题，${countText(d.missing_title_count, " 条")}仍明确标为“标题未获取”。`;
    case "logging_ready": return `日志已启用；当前有 ${countText(d.results, " 条")}结果、${countText(d.sources, " 条")}信源。`;
    case "remote_sync_started": return `远端回传已经启用，当前有 ${countText(d.pending, " 条")}结果等待回传。`;
    case "remote_sync_queued": return `本轮结果已安全加入回传队列；当前待回传 ${countText(d.pending, " 条")}。`;
    case "remote_sync_success": return `主机已确认接收，本轮回传成功；剩余 ${countText(d.pending, " 条")}。`;
    case "remote_sync_retry": return `暂时未回传成功：${d.error || "主机不可用"}。结果仍保存在本机，${countText(d.retry_seconds, " 秒")}后自动重试。`;
    case "remote_sync_disabled": return `远端回传未启用：${d.error || "配置不完整"}。`;
    case "remote_sync_host_ready": return "回传主机连接正常，新结果生成后会立即回传。";
    case "remote_sync_host_waiting": return `当前无法连接回传主机：${d.error || "主机不可用"}。新结果仍会安全排队并自动重试。`;
    case "local_only_ready": return "Kimi监控已启用强制本机模式，结果不会发送到任何远端主机。";
    case "round_retry_scheduled": return `遇到临时异常：${d.error || "未知原因"}。将在 ${countText(d.retry_seconds, " 秒")}后进行第 ${countText(d.retry_attempt)} 次重试（最多 ${countText(d.max_retries)} 次）。`;
    case "answer_wait_diagnostic": return `正文候选 ${countText(d.reply_chars, " 字")}；页面生成状态：${d.generation_busy ? "仍在生成" : "未在生成"}；稳定检测 ${countText(d.stable_polls, " 次")}。`;
    case "answer_ui_state_fallback": return `正文已连续稳定 ${countText(d.stable_polls, " 次")}，但页面生成状态未正常释放；已按 ${countText(d.reply_chars, " 字")}正文继续保存。`;
    case "answer_timeout_fallback": return `等待达到上限，但已找到 ${countText(d.reply_chars, " 字")}有效正文；本轮继续采集信源并保存，不再重复提问。`;
    case "empty_answer_detected": return `问题已发送，但Kimi连续 ${countText(d.empty_seconds, " 秒")}没有生成回答；将快速刷新页面重试，不再空等240秒。`;
    case "page_reload_for_retry": return `本次页面状态异常，正在刷新Kimi页面后进行第 ${countText(d.retry_attempt)} 次重试。`;
    case "round_failed_continuing": return `本轮重试 ${countText(d.retry_count, " 次")}后仍未获得回答，已记录失败并继续第 ${countText(d.next_round)} 轮，不再停止整个任务。`;
    case "send_unconfirmed": return "问题已经写入输入框，但Kimi没有确认发送；插件将刷新页面并重新发送本轮问题。";
    case "adaptive_ui_match": return `固定选择器失效后，已按历史结构指纹重新定位“${d.ui_role || "页面控件"}”（相似度 ${countText(d.similarity_score)}）。`;
    default: return "已记录这一运行步骤。";
  }
}

function logState(event) {
  if (event.event === "round_failed_continuing") return { label: "已跳过", className: "error" };
  if (event.level === "error" || event.event === "round_error") return { label: "异常", className: "error" };
  if (["remote_sync_retry", "remote_sync_host_waiting", "round_retry_scheduled", "answer_ui_state_fallback", "answer_timeout_fallback", "empty_answer_detected", "page_reload_for_retry", "send_unconfirmed", "adaptive_ui_match"].includes(event.event)) return { label: "已兜底", className: "waiting" };
  if (SUCCESS_EVENTS.has(event.event)) return { label: "完成", className: "success" };
  return { label: "处理中", className: "progressState" };
}

function readableDetailValue(key, value) {
  if (key === "source_capture_complete") return value ? "完整" : "不完整";
  if (typeof value === "string") return value.length > 600 ? value.slice(0, 600) + "…" : value;
  return JSON.stringify(value, null, 2);
}

function makeTechnicalDetails(event) {
  const details = document.createElement("details");
  details.className = "technicalDetails";
  const summary = document.createElement("summary");
  summary.textContent = "查看技术详情";
  const list = document.createElement("dl");
  const hiddenKeys = new Set(["composer", "sources"]);
  for (const [key, value] of Object.entries(event.details || {})) {
    if (hiddenKeys.has(key)) continue;
    const term = document.createElement("dt");
    term.textContent = DETAIL_LABELS[key] || key;
    const description = document.createElement("dd");
    if ((key.endsWith("url") || key.endsWith("_url")) && /^https?:\/\//i.test(String(value))) {
      const link = document.createElement("a");
      link.href = String(value);
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = String(value);
      description.append(link);
    } else {
      description.textContent = readableDetailValue(key, value);
    }
    list.append(term, description);
  }
  if (!list.children.length) {
    const empty = document.createElement("div");
    empty.className = "subtle";
    empty.textContent = "这一条没有额外技术信息。";
    details.append(summary, empty);
  } else {
    details.append(summary, list);
  }
  return details;
}

function renderLogs() {
  const container = $("logCards");
  container.replaceChildren();
  $("logsEmpty").style.display = logCache.length ? "none" : "block";
  for (const event of logCache.slice().reverse().slice(0, 300)) {
    const card = document.createElement("article");
    const state = logState(event);
    card.className = `logCard ${state.className}Log`;
    const line = document.createElement("div");
    line.className = "logHead";
    const nameWrap = document.createElement("div");
    nameWrap.className = "logName";
    const stateBadge = document.createElement("span");
    stateBadge.className = `logState ${state.className}`;
    stateBadge.textContent = state.label;
    const name = document.createElement("strong");
    name.textContent = EVENT_LABELS[event.event] || "运行记录";
    nameWrap.append(stateBadge, name);
    const time = document.createElement("span");
    time.className = "subtle";
    time.textContent = event.timestamp ? new Date(event.timestamp).toLocaleString() : "";
    line.append(nameWrap, time);
    const meta = document.createElement("div");
    meta.className = "logMeta";
    meta.textContent = event.prompt ? `第 ${event.round || "-"} 轮 · ${event.prompt}` : "系统记录";
    const summary = document.createElement("div");
    summary.className = "logSummary";
    summary.textContent = eventSummary(event);
    card.append(line, meta, summary, makeTechnicalDetails(event));
    container.append(card);
  }
}

async function loadLogs() {
  const response = await call({ type: "GET_EVENT_LOGS" });
  logCache = response.events || [];
  $("logsSource").textContent = response.source === "local_receiver" ? "最新在上 · 点击“查看技术详情”可展开" : "插件本地日志 · 本机接收器未连接";
  renderLogs();
}

function activatePane(paneId) {
  activePane = paneId;
  document.querySelectorAll(".panelPane").forEach((pane) => pane.classList.toggle("active", pane.id === paneId));
  document.querySelectorAll(".panelTab").forEach((tab) => tab.classList.toggle("active", tab.dataset.pane === paneId));
  if (paneId === "resultsPane") loadResults();
  if (paneId === "logsPane") loadLogs();
}

function download(name, value, type) {
  const url = URL.createObjectURL(new Blob([value], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function csvCell(value) { return `"${String(value ?? "").replace(/"/g, '""')}"`; }

document.querySelectorAll(".panelTab").forEach((tab) => tab.addEventListener("click", () => activatePane(tab.dataset.pane)));
$("start").addEventListener("click", async () => {
  showError("");
  const button = $("start");
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = "正在检测Kimi页面…";
  toast("正在检测Kimi页面");
  try {
    const settings = readSettings();
    await call({ type: "SAVE_SETTINGS", settings });
    const response = await call({ type: "START_JOB", settings });
    if (!response.ok) {
      showError(response.error);
      toast("启动失败，请查看红色提示");
    } else {
      toast("任务已开始");
    }
    await refreshContext(false);
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
});
$("pause").addEventListener("click", async () => { await call({ type: "PAUSE_JOB" }); await refreshContext(false); });
$("resume").addEventListener("click", async () => { await call({ type: "RESUME_JOB" }); await refreshContext(false); });
$("stop").addEventListener("click", async () => { await call({ type: "STOP_JOB" }); await refreshContext(false); });
$("retrySync").addEventListener("click", async () => { await call({ type: "RETRY_SYNC" }); await refreshContext(false); });
$("resetAdaptive").addEventListener("click", async () => {
  await chrome.storage.local.remove("kimiAdaptiveUiProfiles");
  toast("自适应 UI 记忆已重置，刷新Kimi页面后生效");
});
$("openTarget").addEventListener("click", async () => {
  const settings = readSettings();
  await call({ type: "SAVE_SETTINGS", settings });
  await call({ type: "OPEN_TARGET", url: settings.targetUrl });
});
$("questions").addEventListener("input", updateQuestionCount);
$("loadAllQuestions").addEventListener("click", async () => {
  $("questions").value = ALL_QUESTIONS.join("\n");
  $("rounds").value = 100;
  updateQuestionCount();
  await call({ type: "SAVE_SETTINGS", settings: readSettings() });
  toast("已载入全部13题，每题100轮");
});
$("refreshResults").addEventListener("click", loadResults);
$("refreshLogs").addEventListener("click", loadLogs);
$("exportJsonl").addEventListener("click", () => download(`kimi_results_${new Date().toISOString().slice(0,10)}.jsonl`, resultCache.map((item) => JSON.stringify(item)).join("\n") + "\n", "application/x-ndjson;charset=utf-8"));
$("exportCsv").addEventListener("click", () => {
  const head = ["status","skip_reason","finished_at","round","question_round","prompt","reply","source_count","source_capture_complete","source_titles","source_urls","source_title_and_urls","page_url"];
  const lines = [head.map(csvCell).join(",")].concat(resultCache.map((item) => [
    item.status || "success",item.skip_reason || "",item.finished_at,item.round,item.question_round,item.prompt,item.reply,(item.sources || []).length,item.source_capture_complete,
    (item.sources || []).map((source) => source.title || "标题未获取").join("\n"),
    (item.sources || []).map((source) => source.url).join("\n"),
    (item.sources || []).map((source) => `${source.title || "标题未获取"}\n${source.url}`).join("\n\n"),item.page_url
  ].map(csvCell).join(",")));
  download(`kimi_results_${new Date().toISOString().slice(0,10)}.csv`, "\ufeff" + lines.join("\r\n"), "text/csv;charset=utf-8");
});

refreshContext(true);
setInterval(() => {
  refreshContext(false);
  if (activePane === "logsPane") loadLogs();
}, 2000);
