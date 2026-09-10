"use client";

import { useCallback, useEffect, useState } from "react";
import { getIcon } from "./ModelIcon";
import { EnterpriseBrandLockup } from "./EnterpriseBrand";

type Task = {
  id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  total_steps: number;
  completed_steps: number;
  current_model?: string;
  message?: string;
  error?: string;
  brand_name?: string;
  product_name?: string;
  questions?: string[];
  model_progress?: Record<string, { completed: number; total: number }>;
  queue_position?: number;
  retry_count?: number;
  created_at?: string;
  events?: Array<{ created_at: string; level: string; message: string }>;
};

type Readiness = {
  ready: boolean;
  online: boolean;
  message: string;
  models: Record<string, { ready: boolean; message: string }>;
  queue?: {
    running: number;
    waiting: number;
    ahead_if_submitted: number;
    requires_queue: boolean;
  };
};

type DiagnosisQuota = { limited: boolean; limit: number; used: number; remaining: number };

const MODEL_NAMES: Record<string, string> = {
  doubao: "豆包",
  yuanbao: "腾讯元宝",
  wenxin: "文心一言",
  quark: "千问",
  deepseek: "DeepSeek",
  kimi: "Kimi",
};

const MODEL_SHOWCASE = [
  { id: "doubao", name: "豆包", description: "真实问答与信源采集", progressModel: "doubao" },
  { id: "yuanbao", name: "腾讯元宝", description: "真实问答与信源采集", progressModel: "yuanbao" },
  { id: "wenxin", name: "文心一言", description: "真实问答与信源采集", progressModel: "wenxin" },
  { id: "quark", name: "千问", description: "真实问答与信源采集", progressModel: "quark" },
  { id: "deepseek", name: "DeepSeek", description: "真实问答与信源采集", progressModel: "deepseek" },
  { id: "kimi", name: "Kimi", description: "真实问答与信源采集", progressModel: "kimi" },
] as const;

function monitorBasePath() {
  const configured = String(import.meta.env.VITE_MONITOR_BASE_PATH || "").trim().replace(/\/$/, "");
  if (configured || typeof window === "undefined") return configured;
  return window.location.pathname === "/geo" || window.location.pathname.startsWith("/geo/") ? "/geo" : "";
}

function apiBase() {
  const localApiPort = import.meta.env.VITE_MONITOR_API_PORT || "8765";
  const configured = monitorBasePath();
  if (typeof window === "undefined") return `http://127.0.0.1:${localApiPort}`;
  if (window.location.port === localApiPort) return "";
  if (window.location.port === "3000") return `${window.location.protocol}//${window.location.hostname}:${localApiPort}`;
  return configured;
}

function reportPath(reportKey: string) {
  const configured = monitorBasePath();
  return `${configured}/${reportKey}` || `/${reportKey}`;
}

function ModelIcon({ model }: { model: string }) {
  const Icon = getIcon(model);
  return Icon ? <Icon className={`model-icon ${model}`} size={34} /> : <span>{model.slice(0, 2)}</span>;
}

function elapsedLabel(createdAt?: string) {
  if (!createdAt) return "刚刚开始";
  const started = new Date(createdAt).getTime();
  if (!Number.isFinite(started)) return "采集中";
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

export function DiagnosisStart() {
  const [brand, setBrand] = useState("");
  const [product, setProduct] = useState("");
  const [question, setQuestion] = useState("");
  const [reportKey, setReportKey] = useState("");
  const [task, setTask] = useState<Task | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [quota, setQuota] = useState<DiagnosisQuota | null>(null);
  const [lastPolledAt, setLastPolledAt] = useState(0);
  const [, setClockTick] = useState(0);

  const clearActiveDiagnosis = useCallback(() => {
    window.sessionStorage.removeItem("geoActiveReportKey");
    setReportKey("");
    setTask(null);
  }, []);

  const loadReadiness = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase()}/api/diagnosis?_=${Date.now()}`, { cache: "no-store" });
      const next = await response.json();
      if (response.ok && next.ok) {
        setReadiness(next.readiness);
        setQuota(next.quota || null);
      }
    } catch (_) {
      setReadiness({
        ready: false,
        online: false,
        message: "采集服务连接失败，正在自动重试…",
        models: Object.fromEntries(Object.keys(MODEL_NAMES).map((model) => [model, { ready: false, message: "自动调度中" }])),
      });
    }
  }, []);

  const loadTask = useCallback(async (key: string) => {
    try {
      const response = await fetch(`${apiBase()}/api/diagnosis/${key}?_=${Date.now()}`, { cache: "no-store" });

      // The task may have been deleted from the management console while this
      // tab still remembers its report key. Treat that as a clean return to the
      // diagnosis form instead of leaving a contradictory stale progress card.
      if (response.status === 404) {
        clearActiveDiagnosis();
        setError("");
        void loadReadiness();
        return;
      }

      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "诊断进度读取失败");
      if (!next.task) {
        clearActiveDiagnosis();
        setError("");
        void loadReadiness();
        return;
      }
      setTask(next.task);
      setReadiness(next.readiness);
      setLastPolledAt(Date.now());
      setError("");
      if (next.task?.status === "completed") {
        window.sessionStorage.removeItem("geoActiveReportKey");
        window.location.assign(reportPath(key));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "诊断进度读取失败");
    }
  }, [clearActiveDiagnosis, loadReadiness]);

  useEffect(() => {
    const saved = window.sessionStorage.getItem("geoActiveReportKey") || "";
    if (saved) setReportKey(saved);
    else void loadReadiness();
  }, [loadReadiness]);

  useEffect(() => {
    if (!reportKey) {
      const timer = window.setInterval(() => void loadReadiness(), 5000);
      return () => window.clearInterval(timer);
    }
    void loadTask(reportKey);
    const timer = window.setInterval(() => {
      if (!document.hidden) void loadTask(reportKey);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadReadiness, loadTask, reportKey]);

  async function createDiagnosis(values: { brand_name: string; product_name: string; question: string }) {
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch(`${apiBase()}/api/diagnosis`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "诊断提交失败");
      const key = String(next.report_key || "");
      if (!/^[a-f0-9]{32}$/.test(key)) throw new Error("服务器未返回有效报告密钥");
      window.sessionStorage.setItem("geoActiveReportKey", key);
      setTask(next.task);
      setLastPolledAt(Date.now());
      setReportKey(key);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "诊断提交失败");
    } finally {
      setSubmitting(false);
    }
  }

  async function submit() {
    await createDiagnosis({ brand_name: brand, product_name: product, question });
  }

  async function retryFailedTask() {
    const values = {
      brand_name: String(task?.brand_name || ""),
      product_name: String(task?.product_name || ""),
      question: String(task?.questions?.[0] || ""),
    };
    if (!values.brand_name || !values.product_name || !values.question) {
      setError("原任务信息不完整，请返回重新填写");
      return;
    }
    await createDiagnosis(values);
  }

  function resetFailedTask() {
    setBrand(String(task?.brand_name || ""));
    setProduct(String(task?.product_name || ""));
    setQuestion(String(task?.questions?.[0] || ""));
    clearActiveDiagnosis();
    setError("");
  }

  const progress = task?.total_steps ? Math.round(task.completed_steps * 100 / task.total_steps) : 0;
  const active = task?.status === "queued" || task?.status === "running";
  const unsuccessful = task?.status === "failed" || task?.status === "cancelled";
  const queueAhead = Number(readiness?.queue?.ahead_if_submitted || 0);
  const activeQueueAhead = task?.status === "queued" ? Number(task.queue_position || 0) : 0;
  const recovering = task?.status === "queued" && Number(task.retry_count || 0) > 0;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setClockTick((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  const heartbeatFresh = active && lastPolledAt > 0 && Date.now() - lastPolledAt < 10000;
  const queueLabel = readiness?.online === false
    ? "采集服务恢复中"
    : task?.status === "running"
      ? "当前任务执行中"
      : recovering
        ? `当前任务自动恢复中 · 已保存 ${task?.completed_steps || 0}/${task?.total_steps || 0}`
      : task?.status === "queued"
        ? activeQueueAhead > 0 ? `当前任务排队中 · 前方 ${activeQueueAhead} 个` : "当前任务即将执行"
    : queueAhead > 0
      ? `当前需排队 · 前方 ${queueAhead} 个`
      : "企业队列空闲";

  return (
    <main className="diagnosis-start-shell">
      <header className="diagnosis-start-header">
        <a className="diagnosis-start-brand" href={`${monitorBasePath()}/`} aria-label="品牌推荐诊断首页"><EnterpriseBrandLockup /></a>
        <div className="diagnosis-header-actions"><span className={readiness?.online === false || task?.status === "queued" || (!active && queueAhead > 0) ? "" : "ready"}><i />{queueLabel}</span><a href={`${monitorBasePath()}/admin`}>管理员中心</a></div>
      </header>
      <section className="diagnosis-start-main">
        {error && <div className="diagnosis-error">{error}</div>}

        {!reportKey ? (
          <section className="diagnosis-workspace">
            <aside className="diagnosis-workspace-intro">
              <div className="diagnosis-intro-index"><span>01</span><i /></div>
              <div>
                <small>AI RECOMMENDATION AUDIT</small>
                <h1>你的产品，<br />会被大模型主动推荐吗？</h1>
                <p>从真实消费者问题出发，审计主流 AI 平台的品牌推荐、排序位置与引用来源。</p>
              </div>
              <ul>
                <li><i />独立平台抽样</li>
                <li><i />回答与信源核验</li>
                <li><i />生成专属报告</li>
              </ul>
              <div className="diagnosis-intro-foot"><span>覆盖 6 个主流 AI 平台</span><b>GEO / 01</b></div>
            </aside>
            <section className="diagnosis-form-card diagnosis-start-form">
              <header className="diagnosis-form-heading"><div><small>NEW DIAGNOSIS</small><h2>创建品牌推荐诊断</h2><p>填写完整信息后，系统将自动进入企业队列。</p></div><span><i />安全任务通道</span></header>
              <div className="diagnosis-readiness ready">
                <div><b>{queueAhead > 0 ? "企业诊断队列繁忙" : "采集资源状态"}</b><span>{queueAhead > 0 ? `当前 ${readiness?.queue?.running || 0} 个任务执行中、${readiness?.queue?.waiting || 0} 个任务等待；现在提交，前方共 ${queueAhead} 个任务` : readiness?.ready ? "队列空闲，提交后将立即进入诊断" : "任务可以提交，资源恢复后将按顺序自动执行"}</span></div>
                <div className="diagnosis-readiness-models">
                  {Object.keys(MODEL_NAMES).map((model) => <span className="ready" key={model}><i />{MODEL_NAMES[model]}</span>)}
                </div>
              </div>
              <div className="diagnosis-form-grid">
                <label><span>品牌名称 <em>*</em></span><input value={brand} onChange={(event) => setBrand(event.target.value)} placeholder="请输入品牌全称" maxLength={100} /></label>
                <label><span>诊断产品 <em>*</em></span><input value={product} onChange={(event) => setProduct(event.target.value)} placeholder="请输入产品完整名称" maxLength={120} /></label>
              </div>
              <label><span>消费者会向大模型提出的问题 <em>*</em></span><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：推荐一款大量出汗后适合喝的电解质饮料" maxLength={500} /></label>
              <div className="diagnosis-submit-row">
                <div className="diagnosis-form-assurance"><span><i />多平台抽样</span><span><i />信源可追溯</span><span><i />报告独立访问</span>{quota?.limited && <span className={quota.remaining > 0 ? "" : "quota-empty"}><i />今日剩余 {quota.remaining}/{quota.limit} 次</span>}</div>
                <button disabled={submitting || (quota?.limited === true && quota.remaining === 0) || !brand.trim() || !product.trim() || !question.trim()} onClick={submit}><span>{submitting ? "正在创建诊断" : quota?.limited === true && quota.remaining === 0 ? "今日额度已用完" : "提交诊断"}</span><b aria-hidden="true">↗</b></button>
              </div>
            </section>
          </section>
        ) : (
          <section className="diagnosis-progress-card diagnosis-start-progress">
            <header><div><small>{unsuccessful ? "本次诊断未完成" : recovering ? "断点自动恢复" : task?.status === "queued" ? "任务正在排队" : "本次诊断正在执行"}</small><h3>{task?.message || "任务已创建，即将开始"}</h3><div className="diagnosis-live-status"><span className={heartbeatFresh ? "healthy" : ""}><i />{heartbeatFresh ? "采集服务持续响应" : "正在连接采集服务"}</span><span>已运行 {elapsedLabel(task?.created_at)}</span></div></div><b>{progress}%</b></header>
            {task?.status === "queued" && <div className="diagnosis-live-queue"><div><span>{recovering ? "已保存采集结果" : "当前排队位置"}</span><strong>{recovering ? `${task.completed_steps}/${task.total_steps} 项` : task.queue_position ? `前方 ${task.queue_position} 个任务` : "下一位执行"}</strong></div><p>{recovering ? "异常平台正在退避后自动补采；已完成数据不会丢失，也不会重复计数。" : "系统每 3 秒更新位置；前序任务完成后会自动开始，无需重复提交。"}</p></div>}
            <div className={`diagnosis-progress ${active ? "is-live" : ""}`}><i style={{ width: `${progress}%` }} /></div>
            <div className="diagnosis-progress-meta"><span>{active ? `已完成 ${task?.completed_steps || 0}/${task?.total_steps || 0} 项 · 各平台并行采集` : "多平台抽样分析"}</span><span>{active ? "通常约 2–5 分钟，完成后自动进入报告" : task?.status === "failed" ? "诊断暂未完成" : task?.status === "cancelled" ? "诊断已停止" : "正在生成报告"}</span></div>
            {task?.error && <p className="diagnosis-error-text">诊断暂未完成，管理员正在处理，请稍后再试。</p>}
            {unsuccessful && <div className="diagnosis-failure-actions"><button disabled={submitting} onClick={retryFailedTask}>{submitting ? "正在重新创建任务…" : "重新运行本次诊断"}</button><button className="secondary" onClick={resetFailedTask}>返回重新填写</button><span>旧失败记录可在任务管理中心删除</span></div>}
            <div className="diagnosis-customer-steps">
              <div className="done"><i />任务已提交</div>
              <div className={task?.status !== "queued" ? "done" : "active"}><i />{recovering ? "断点续跑中" : task?.status === "queued" ? `等待执行${task.queue_position ? ` · 前方 ${task.queue_position} 个任务` : ""}` : "已开始诊断"}</div>
              <div className={task?.completed_steps ? "active" : ""}><i />六模型数据采集中</div>
              <div className={task?.status === "completed" ? "done" : ""}><i />生成诊断报告</div>
            </div>
          </section>
        )}

        <section className="diagnosis-start-models">
          {MODEL_SHOWCASE.map((model) => {
            const modelProgress = model.progressModel ? task?.model_progress?.[model.progressModel] : undefined;
            return <article key={model.id}><ModelIcon model={model.id} /><div><b>{model.name}</b><span>{modelProgress ? modelProgress.total > 0 && modelProgress.completed >= modelProgress.total ? `${modelProgress.completed}/${modelProgress.total} · 采集完成` : `${modelProgress.completed}/${modelProgress.total} · 采集中` : model.description}</span></div><i className={modelProgress && modelProgress.completed < modelProgress.total ? "working" : ""} /></article>;
          })}
        </section>
      </section>
    </main>
  );
}
