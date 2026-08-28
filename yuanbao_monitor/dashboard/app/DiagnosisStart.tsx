"use client";

import { useCallback, useEffect, useState } from "react";
import { getIcon } from "./ModelIcon";

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
  events?: Array<{ created_at: string; level: string; message: string }>;
};

type Readiness = {
  ready: boolean;
  online: boolean;
  message: string;
  models: Record<string, { ready: boolean; message: string }>;
};

const MODEL_NAMES: Record<string, string> = {
  doubao: "豆包",
  yuanbao: "腾讯元宝",
  wenxin: "文心一言",
};

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

export function DiagnosisStart() {
  const [brand, setBrand] = useState("");
  const [product, setProduct] = useState("");
  const [question, setQuestion] = useState("");
  const [reportKey, setReportKey] = useState("");
  const [task, setTask] = useState<Task | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const clearActiveDiagnosis = useCallback(() => {
    window.sessionStorage.removeItem("geoActiveReportKey");
    setReportKey("");
    setTask(null);
  }, []);

  const loadReadiness = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase()}/api/diagnosis?_=${Date.now()}`, { cache: "no-store" });
      const next = await response.json();
      if (response.ok && next.ok) setReadiness(next.readiness);
    } catch (_) {
      setReadiness({
        ready: false,
        online: false,
        message: "采集服务连接失败，正在自动重试…",
        models: Object.fromEntries(Object.keys(MODEL_NAMES).map((model) => [model, { ready: false, message: "等待连接" }])),
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

  return (
    <main className="diagnosis-start-shell">
      <header className="diagnosis-start-header">
        <div className="diagnosis-logo">GEO</div>
        <div><small>GEO PRODUCT RECOMMENDATION</small><b>大模型推荐诊断</b></div>
        <span className={readiness?.ready ? "ready" : "waiting"}>{readiness?.ready ? "三模型在线" : "任务可排队"}</span>
      </header>
      <section className="diagnosis-start-main">
        <section className="diagnosis-hero diagnosis-start-hero">
          <div>
            <span>豆包 · 腾讯元宝 · 文心一言</span>
            <h1>你的产品，会被大模型主动推荐吗？</h1>
            <p>输入需要诊断的品牌、具体产品和消费者问题。系统会执行 24 轮真实网页提问，完成后自动生成一个独立、安全的报告地址。</p>
          </div>
        </section>

        {error && <div className="diagnosis-error">{error}</div>}

        {!reportKey ? (
          <section className="diagnosis-form-card diagnosis-start-form">
            <div className={`diagnosis-readiness ${readiness?.ready ? "ready" : "blocked"}`}>
              <div><b>本机采集环境</b><span>{readiness?.message || "正在检查三模型采集器…"}</span></div>
              <div className="diagnosis-readiness-models">
                {Object.keys(MODEL_NAMES).map((model) => <span className={readiness?.models?.[model]?.ready ? "ready" : "blocked"} key={model}><i />{MODEL_NAMES[model]}{readiness?.models?.[model]?.ready ? "已就绪" : "等待连接"}</span>)}
              </div>
            </div>
            <div className="diagnosis-form-grid">
              <label><span>品牌名称 *</span><input value={brand} onChange={(event) => setBrand(event.target.value)} placeholder="请输入品牌全称" maxLength={100} /></label>
              <label><span>需要诊断的具体产品 *</span><input value={product} onChange={(event) => setProduct(event.target.value)} placeholder="请输入产品完整名称" maxLength={120} /></label>
            </div>
            <label><span>消费者会向大模型提出的问题 *</span><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：推荐一款大量出汗后适合喝的电解质饮料" maxLength={500} /></label>
            <div className="diagnosis-submit-row">
              <div><b>24</b><span>总提问轮次</span></div><div><b>3</b><span>覆盖模型</span></div><div><b>8</b><span>每模型轮次</span></div>
              <button disabled={submitting || !brand.trim() || !product.trim() || !question.trim()} onClick={submit}>{submitting ? "正在创建安全诊断…" : readiness?.ready ? "开始诊断" : "提交并排队"}</button>
            </div>
          </section>
        ) : (
          <section className="diagnosis-progress-card diagnosis-start-progress">
            <header><div><small>{unsuccessful ? "本次诊断未完成" : "本次诊断正在执行"}</small><h3>{task?.message || "任务已创建，等待本机插件领取"}</h3></div><b>{progress}%</b></header>
            <div className="diagnosis-progress"><i style={{ width: `${progress}%` }} /></div>
            <div className="diagnosis-progress-meta"><span>{task?.completed_steps || 0}/{task?.total_steps || 24} 轮完成</span><span>{active ? "完成后自动进入安全报告页" : task?.status === "failed" ? "诊断执行失败" : task?.status === "cancelled" ? "诊断已取消" : "正在生成报告"}</span></div>
            {task?.error && <p className="diagnosis-error-text">{task.error}</p>}
            {unsuccessful && <div className="diagnosis-failure-actions"><button disabled={submitting} onClick={retryFailedTask}>{submitting ? "正在重新创建任务…" : "重新运行本次诊断"}</button><button className="secondary" onClick={resetFailedTask}>返回重新填写</button><span>旧失败记录可在任务管理中心删除</span></div>}
            <div className="diagnosis-timeline">
              <h4>实时诊断日志</h4>
              {(task?.events || []).slice(0, unsuccessful ? 5 : 12).map((event, index) => <div className={event.level === "error" ? "error" : ""} key={`${event.created_at}-${index}`}><time>{event.created_at.slice(11, 19)}</time><i /><span>{event.message}</span></div>)}
            </div>
          </section>
        )}

        <section className="diagnosis-start-models">
          {Object.keys(MODEL_NAMES).map((model) => {
            const modelProgress = task?.model_progress?.[model];
            return <article key={model}><ModelIcon model={model} /><div><b>{MODEL_NAMES[model]}</b><span>{modelProgress ? `${modelProgress.completed}/${modelProgress.total} 轮 · 正文与全部信源` : "并行采集正文与全部信源"}</span></div></article>;
          })}
        </section>
      </section>
    </main>
  );
}
