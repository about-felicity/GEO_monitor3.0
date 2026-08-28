"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { getIcon } from "./ModelIcon";

type DiagnosisTask = {
  id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  total_steps: number;
  completed_steps: number;
  current_model?: string;
  current_question?: string;
  message?: string;
  error?: string;
  created_at: string;
  finished_at?: string;
  brand_name?: string;
  product_name?: string;
  questions?: string[];
  events?: Array<{ created_at: string; level: string; message: string }>;
};

type ModelReport = {
  id: string;
  completed: number;
  target_rounds: number;
  recommended_rounds: number;
  recommendation_rate: number;
  average_rank: number | null;
  source_count: number;
  expected_source_count: number;
  body_complete_rounds: number;
  source_complete_rounds: number;
  analysis_complete_rounds: number;
  total_body_chars: number;
};

type SourceRecord = { url: string; title: string; models: string[] };
type AnswerRecord = {
  model: string;
  round: number;
  question: string;
  answer: string;
  recommended: boolean;
  rank: number | null;
  sources: Array<{ url?: string; href?: string; title?: string }>;
  body_length: number;
  body_capture_complete: boolean;
  body_capture_origin: string;
  expected_source_count: number;
  source_capture_complete: boolean;
  source_capture_origins: { dom?: number; network?: number };
  capture_mode: string;
  analysis: {
    complete: boolean;
    mode: string;
    recommended: boolean;
    rank: number | null;
    matched_terms: string[];
    analyzed_at: string;
  };
  finished_at: string;
};

type DiagnosisReport = {
  brand_name: string;
  product_name: string;
  question: string;
  overall_rate: number;
  recommended_rounds: number;
  completed_rounds: number;
  target_rounds: number;
  conclusion: string;
  models: ModelReport[];
  sources: SourceRecord[];
  answers: AnswerRecord[];
  quality: {
    body_complete_rounds: number;
    source_complete_rounds: number;
    analysis_complete_rounds: number;
    total_body_chars: number;
    captured_sources: number;
    expected_sources: number;
  };
};

type DiagnosisPayload = {
  ok: boolean;
  customer_slug: string;
  task: DiagnosisTask | null;
  history: DiagnosisTask[];
  report: DiagnosisReport | null;
  readiness: {
    ready: boolean;
    online: boolean;
    worker_id: string;
    message: string;
    models: Record<string, { ready: boolean; message: string }>;
  };
  error?: string;
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

function statusLabel(status?: string) {
  return ({ queued: "等待采集", running: "诊断中", completed: "诊断完成", failed: "诊断失败", cancelled: "已取消" } as Record<string, string>)[status || ""] || "尚未诊断";
}

function recommendationTone(rate: number) {
  if (rate >= 60) return "strong";
  if (rate >= 25) return "medium";
  return "weak";
}

function diagnosisRootPath() {
  const root = monitorBasePath();
  return `${root}/` || "/";
}

function redirectToDiagnosisRoot() {
  window.sessionStorage.removeItem("geoActiveReportKey");
  window.location.replace(diagnosisRootPath());
}

function DiagnosisModelIcon({ model, size }: { model: string; size: number }) {
  const Icon = getIcon(model);
  return Icon ? <Icon className={`model-icon ${model}`} size={size} /> : <span>{model.slice(0, 2)}</span>;
}

export function DiagnosisDashboard({ customerSlug }: { customerSlug: string }) {
  const [payload, setPayload] = useState<DiagnosisPayload | null>(null);
  const [tab, setTab] = useState<"diagnosis" | "report" | "evidence">("diagnosis");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expandAllEvidence, setExpandAllEvidence] = useState(false);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const response = await fetch(`${apiBase()}/api/diagnosis/${customerSlug}?_=${Date.now()}`, { cache: "no-store" });
      if (response.status === 404) {
        redirectToDiagnosisRoot();
        return;
      }
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "诊断数据读取失败");
      if (!next.task || next.task.status !== "completed") {
        redirectToDiagnosisRoot();
        return;
      }
      setPayload(next);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "诊断数据读取失败");
    } finally {
      if (!silent) setLoading(false);
    }
  }, [customerSlug]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load(true);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [load]);

  const task = payload?.task;
  const report = payload?.report;
  const brand = report?.brand_name || task?.brand_name || "品牌产品";
  const product = report?.product_name || task?.product_name || "";
  const question = report?.question || task?.questions?.[0] || "诊断问题";
  const progress = task?.total_steps ? Math.round(task.completed_steps * 100 / task.total_steps) : 0;
  const modelReports = useMemo(() => report?.models || ["doubao", "yuanbao", "wenxin"].map((id) => ({
    id, completed: 0, target_rounds: 8, recommended_rounds: 0,
    recommendation_rate: 0, average_rank: null, source_count: 0, expected_source_count: 0,
    body_complete_rounds: 0, source_complete_rounds: 0, analysis_complete_rounds: 0, total_body_chars: 0,
  })), [report]);

  useEffect(() => {
    if (task?.status === "completed") setTab((current) => current === "diagnosis" ? "report" : current);
  }, [task?.status]);

  return (
    <main className="diagnosis-shell">
      <aside className="diagnosis-sidebar">
        <a className="diagnosis-logo" href="/geo/">GEO</a>
        <div className="diagnosis-customer">
          <small>安全诊断报告</small>
          <b>{brand}</b>
          <span>/{customerSlug}</span>
        </div>
        <nav>
          <button className={tab === "diagnosis" ? "active" : ""} onClick={() => setTab("diagnosis")}>诊断概览</button>
          <button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")}>推荐报告</button>
          <button className={tab === "evidence" ? "active" : ""} onClick={() => setTab("evidence")}>回答与信源</button>
        </nav>
        <div className="diagnosis-model-list">
          {modelReports.map((item) => (
            <div key={item.id}>
              <DiagnosisModelIcon model={item.id} size={28} />
              <span><b>{MODEL_NAMES[item.id]}</b><small>{item.completed}/8 轮</small></span>
            </div>
          ))}
        </div>
      </aside>

      <section className="diagnosis-main">
        <header className="diagnosis-topbar">
          <div><small>GEO PRODUCT RECOMMENDATION</small><h1>{brand}大模型推荐诊断</h1></div>
          <span className={`diagnosis-status ${task?.status || "idle"}`}>{statusLabel(task?.status)}</span>
        </header>

        {error && <div className="diagnosis-error">{error}</div>}
        {loading && !payload ? <div className="diagnosis-empty">正在加载客户数据…</div> : null}

        {tab === "diagnosis" && (
          <>
            <section className="diagnosis-hero">
              <div>
                <span>三模型 · 每模型 8 轮 · 共 24 轮</span>
                <h2>{question}</h2>
                <p>诊断对象：{brand}{product ? ` · ${product}` : ""}。系统保存豆包、腾讯元宝和文心一言的完整回答正文与引用信源，并计算品牌推荐率。</p>
              </div>
            </section>
            {task && (
              <section className="diagnosis-progress-card">
                <header><div><small>实时任务进度</small><h3>{task.message || statusLabel(task.status)}</h3></div><b>{progress}%</b></header>
                <div className="diagnosis-progress"><i style={{ width: `${progress}%` }} /></div>
                <div className="diagnosis-progress-meta"><span>{task.completed_steps}/{task.total_steps} 轮完成</span><span>{task.current_model ? `当前：${MODEL_NAMES[task.current_model] || task.current_model}` : "等待本机采集器"}</span></div>
                {task.error && <p className="diagnosis-error-text">{task.error}</p>}
                <div className="diagnosis-timeline">
                  <h4>实时诊断日志</h4>
                  {(task.events || []).slice(0, 10).map((event, index) => (
                    <div className={event.level === "error" ? "error" : ""} key={`${event.created_at}-${index}`}>
                      <time>{event.created_at.slice(11, 19)}</time>
                      <i />
                      <span>{event.message}</span>
                    </div>
                  ))}
                  {!task.events?.length && <p>任务日志将在本机采集器领取后出现</p>}
                </div>
              </section>
            )}
            <section className="diagnosis-model-cards">
              {modelReports.map((item) => (
                <article key={item.id}>
                  <DiagnosisModelIcon model={item.id} size={42} />
                  <div><small>{MODEL_NAMES[item.id]}</small><b>{item.completed}/8</b><span>已完成轮次</span></div>
                </article>
              ))}
            </section>
          </>
        )}

        {tab === "report" && (
          report && task ? <>
            <section className={`diagnosis-verdict ${recommendationTone(report.overall_rate)}`}>
              <div><small>综合诊断结论</small><h2>{report.conclusion}</h2><p>在已完成的 {report.completed_rounds} 轮回答中，{report.brand_name} 被明确推荐 {report.recommended_rounds} 次。</p></div>
              <strong>{report.overall_rate}<small>%</small><span>综合推荐率</span></strong>
            </section>
            <section className="diagnosis-report-grid">
              {report.models.map((item) => (
                <article key={item.id}>
                  <header><DiagnosisModelIcon model={item.id} size={38} /><b>{MODEL_NAMES[item.id]}</b></header>
                  <strong>{item.recommendation_rate}%</strong><span>推荐率 · {item.recommended_rounds}/{item.completed} 轮</span>
                  <dl><div><dt>平均推荐名次</dt><dd>{item.average_rank ? `第 ${item.average_rank} 名` : "—"}</dd></div><div><dt>回答正文完整</dt><dd>{item.body_complete_rounds}/{item.completed} 轮</dd></div><div><dt>信源采集完整</dt><dd>{item.source_complete_rounds}/{item.completed} 轮</dd></div><div><dt>本地诊断完成</dt><dd>{item.analysis_complete_rounds}/{item.completed} 轮</dd></div><div><dt>正文总字数</dt><dd>{item.total_body_chars.toLocaleString()} 字</dd></div><div><dt>引用信源</dt><dd>{item.source_count}/{item.expected_source_count} 条</dd></div></dl>
                </article>
              ))}
            </section>
            <section className="diagnosis-quality-card">
              <header><div><small>DATA QUALITY AUDIT</small><h2>本次诊断数据验收</h2></div><span>{report.completed_rounds}/{report.target_rounds} 轮</span></header>
              <div><p><b>{report.quality.body_complete_rounds}/{report.completed_rounds}</b><span>正文采集完整</span></p><p><b>{report.quality.source_complete_rounds}/{report.completed_rounds}</b><span>信源采集完整</span></p><p><b>{report.quality.analysis_complete_rounds}/{report.completed_rounds}</b><span>本地诊断完成</span></p><p><b>{report.quality.total_body_chars.toLocaleString()}</b><span>正文总字数</span></p><p><b>{report.quality.captured_sources}/{report.quality.expected_sources}</b><span>已抓取/预期信源</span></p></div>
            </section>
            <section className="diagnosis-table-card">
              <header><div><small>SOURCE INTELLIGENCE</small><h2>影响推荐结果的信源</h2></div><span>{report.sources.length} 个唯一链接</span></header>
              {report.sources.length ? <div className="diagnosis-source-list">{report.sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer"><div><b>{source.title}</b><small>{source.url}</small></div><span>{source.models.map((id) => MODEL_NAMES[id] || id).join(" · ")}</span></a>)}</div> : <div className="diagnosis-empty">当前已完成回答尚未产生外部信源链接</div>}
            </section>
          </> : <div className="diagnosis-empty"><h2>推荐报告正在生成</h2><p>完成首轮采集后，这里会实时出现模型推荐率和信源数据。</p><button onClick={() => setTab("diagnosis")}>查看诊断进度</button></div>
        )}

        {tab === "evidence" && (
          report?.answers.length ? <section className="diagnosis-evidence">
            <header><div><small>RAW ANSWER AUDIT</small><h2>逐轮回答正文与全部信源</h2></div><button type="button" onClick={() => setExpandAllEvidence((value) => !value)}>{expandAllEvidence ? "收起全部" : "展开全部正文"}</button></header>
            {report.models.map((modelReport) => {
              const modelAnswers = report.answers.filter((answer) => answer.model === modelReport.id);
              return <section className="diagnosis-evidence-model" key={modelReport.id}>
                <h3><DiagnosisModelIcon model={modelReport.id} size={30} />{MODEL_NAMES[modelReport.id]}<span>{modelAnswers.length}/8 轮</span></h3>
                {modelAnswers.map((answer, index) => <details open={expandAllEvidence || undefined} key={`${answer.model}-${answer.round}-${index}`}>
                  <summary><b>第 {answer.round} 轮</b><small>{answer.body_length} 字 · 正文{answer.body_capture_complete ? "完整" : "不完整"}</small><small>{answer.sources.length}/{answer.expected_source_count} 条信源 · {answer.source_capture_complete ? "完整" : "待核对"}</small><span className={answer.recommended ? "hit" : "miss"}>{answer.recommended ? "已推荐品牌" : "未发现推荐"}</span></summary>
                  <div className="diagnosis-answer"><h4>{answer.question}</h4><div className="diagnosis-answer-audit"><span className={answer.body_capture_complete ? "ok" : "warn"}>正文：{answer.body_capture_complete ? "采集完整" : "采集不完整"} · {answer.body_capture_origin === "network_response" ? "网络响应" : "页面"}</span><span className={answer.source_capture_complete ? "ok" : "warn"}>信源：{answer.sources.length}/{answer.expected_source_count}，{answer.source_capture_complete ? "采集完整" : "需要核对"}</span><span>信源来源：页面 {answer.source_capture_origins?.dom || 0} · 网络 {answer.source_capture_origins?.network || 0}</span><span className={answer.analysis.complete ? "ok" : "warn"}>诊断：{answer.analysis.complete ? `本地完成${answer.rank ? ` · 第${answer.rank}名` : ""}` : "未完成"}</span><span>方式：{answer.capture_mode || "未记录"}</span></div><p>{answer.answer || "未保存回答正文"}</p><div className="diagnosis-answer-sources"><h5>本轮全部信源（{answer.sources.length}/{answer.expected_source_count}）</h5>{answer.sources.length > 0 ? <ul>{answer.sources.map((source, sourceIndex) => { const url = source.url || source.href || ""; return <li key={`${url}-${sourceIndex}`}><a href={url} target="_blank" rel="noreferrer"><b>{source.title || url}</b><small>{url}</small></a></li>; })}</ul> : <p>本轮回答未引用外部信源；若完整性为“完整”，表示模型本轮确实未给出外部链接。</p>}</div></div>
                </details>)}
              </section>;
            })}
          </section> : <div className="diagnosis-empty"><h2>暂无回答证据</h2><p>采集完成的回答正文和引用链接会按模型、轮次展示在这里。</p></div>
        )}
      </section>
    </main>
  );
}
