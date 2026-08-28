const $ = (id) => document.getElementById(id);
const NAMES = { doubao: "豆包", yuanbao: "腾讯元宝", wenxin: "文心一言" };

function render(state = {}) {
  const status = state.status || {};
  const phases = { connected: "已连接", running: "采集中", error: "异常", stopped: "已停止", idle: "空闲" };
  $("phase").textContent = phases[status.phase] || (state.running ? "连接中" : "未启动");
  $("message").textContent = status.message || "请先打开并登录三个模型网页。";
  $("models").innerHTML = Object.keys(NAMES).map((model) => {
    const item = status.readiness?.[model] || {};
    const progress = status.modelProgress?.[model];
    const progressText = progress ? ` · ${progress.completed}/${progress.total} 轮` : "";
    return `<div class="model ${item.ready ? "ready" : ""}"><b><i></i>${NAMES[model]}</b><span>${item.message || "等待检测"}${progressText}</span></div>`;
  }).join("");
  if (state.config) {
    $("server").value = state.config.server || $("server").value;
    $("token").value = state.config.token || "";
    $("workerId").value = state.config.workerId || $("workerId").value;
    $("interval").value = state.config.intervalSeconds || $("interval").value;
    $("saved").hidden = !state.config.token;
  }
}

async function status() {
  const value = await chrome.runtime.sendMessage({ type: "GEO_GET_STATUS" });
  render(value);
}

$("start").addEventListener("click", async () => {
  const token = $("token").value.trim();
  if (!token) {
    $("message").textContent = "请填写 Worker 密钥";
    return;
  }
  $("message").textContent = "正在连接并检测三个网页…";
  const result = await chrome.runtime.sendMessage({
    type: "GEO_START",
    config: {
      server: $("server").value.trim(),
      token,
      workerId: $("workerId").value.trim(),
      intervalSeconds: Number($("interval").value || 15),
      timeoutSeconds: 240
    }
  });
  if (!result?.ok) $("message").textContent = result?.error || "启动失败";
  await status();
});

$("stop").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "GEO_STOP" });
  await status();
});

$("tasks").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "GEO_OPEN_TASKS" });
});

$("forget").addEventListener("click", async () => {
  if (!confirm("清除本机保存的 Worker 密钥和连接配置？")) return;
  await chrome.runtime.sendMessage({ type: "GEO_FORGET_CONFIG" });
  $("token").value = "";
  $("saved").hidden = true;
  await status();
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "GEO_STATUS") render({ running: true, status: message.status });
});

status();
setInterval(status, 2000);
