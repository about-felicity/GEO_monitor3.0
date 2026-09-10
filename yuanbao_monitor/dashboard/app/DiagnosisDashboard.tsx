"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { getIcon } from "./ModelIcon";
import { EnterpriseBrandMark } from "./EnterpriseBrand";

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
  retry_count?: number;
};

type ModelReport = {
  id: string;
  independent?: boolean;
  data_source?: "direct" | "deepseek_mirror";
  completed: number;
  target_rounds: number;
  recommended_rounds: number;
  recommendation_rate: number;
  raw_recommendation_rate?: number;
  baseline_recommendation_rate?: number;
  calibration_penalty?: number;
  average_rank: number | null;
  first_share?: number;
  top3_share?: number;
  top5_share?: number;
  source_count: number;
  expected_source_count: number;
  body_complete_rounds: number;
  source_complete_rounds: number;
  analysis_complete_rounds: number;
  total_body_chars: number;
};

type SourceRecord = { url: string; title: string; models: string[]; citation_count?: number; domain?: string };
type CompetitorRecord = { name: string; mention_rounds: number; models: string[]; mention_models?: string[]; model_count: number; effective_model_count?: number; recommended_model_count?: number; visibility_score?: number; model_visibility?: Record<string, number>; model_mentions?: Record<string, number>; model_recommended_mentions?: Record<string, number>; recommended_mentions: number; products: string[] };
type AnswerRecord = {
  model: string;
  round: number;
  question: string;
  answer: string;
  recommended: boolean;
  mentioned?: boolean;
  evidence_state?: "recommended" | "mentioned" | "absent";
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
    brands?: string[];
    products?: Array<{ name?: string; brand?: string; recommended?: boolean; rank?: number | null }>;
    summary?: string;
    sentiment?: string;
    analyzed_at: string;
  };
  finished_at: string;
};

type DiagnosisReport = {
  brand_name: string;
  product_name: string;
  question: string;
  overall_rate: number;
  raw_overall_rate?: number;
  first_share?: number;
  top3_share?: number;
  top5_share?: number;
  probability_adjustment?: {
    high_probability_prior_applied?: boolean;
    ordinary_brand_ceiling?: number | null;
  };
  policy_comparison?: {
    same_sample: boolean;
    active_policy: "ordinary" | "high_probability";
    ordinary_rate: number;
    high_probability_rate: number;
    difference: number;
  };
  recommended_rounds: number;
  completed_rounds: number;
  target_rounds: number;
  conclusion: string;
  models: ModelReport[];
  sources: SourceRecord[];
  competitors: CompetitorRecord[];
  source_analysis: {
    total_citations: number;
    unique_links: number;
    unique_domains: number;
    cross_model_links: number;
    top_domains: Array<{ domain: string; citations: number }>;
    by_model: Array<{ model: string; citations: number }>;
  };
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
  quark: "千问",
  deepseek: "DeepSeek",
  kimi: "Kimi",
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

function formatPercent(value: number) {
  const rounded = Math.round(Number(value || 0) * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function answerParagraphs(value: string) {
  const lines = String(value || "")
    .replace(/\r/g, "\n")
    .replace(/[\u200B-\u200D\uFEFF]/g, "")
    .split(/\n+/)
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .filter((line) => line && !/^(?:\d{1,2}|[·•\-–—。；;，,、:：])$/.test(line));
  const output: string[] = [];
  for (const line of lines) {
    const previous = output.at(-1);
    if (previous && (/[或和与、，,:：]$/.test(previous) || /^[，。；;、）)]/.test(line))) {
      output[output.length - 1] = `${previous}${/^[\u4e00-\u9fff]/.test(line) ? "" : " "}${line}`;
    } else if (line !== previous) {
      output.push(line);
    }
  }
  return output.length ? output : ["未保存回答正文"];
}

function MetricIcon({ kind }: { kind: "eye" | "pin" | "medal" | "bars" | "pie" }) {
  const paths = {
    eye: <><path d="M2.5 12s3.4-6 9.5-6 9.5 6 9.5 6-3.4 6-9.5 6S2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="2.7" /></>,
    pin: <><path d="M12 21s6-5.1 6-11a6 6 0 1 0-12 0c0 5.9 6 11 6 11Z" /><circle cx="12" cy="10" r="2" /></>,
    medal: <><path d="m8 3 4 6 4-6" /><circle cx="12" cy="14" r="5" /><path d="M12 11v6m-3-3h6" /></>,
    bars: <><path d="M4 20V10h4v10M10 20V4h4v16M16 20v-7h4v7" /></>,
    pie: <><path d="M11 3a9 9 0 1 0 9 9h-9V3Z" /><path d="M14 3.5V9h5.5A9 9 0 0 0 14 3.5Z" /></>,
  };
  return <span className={`diagnosis-metric-icon ${kind}`} aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[kind]}</svg></span>;
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
  const [tab, setTab] = useState<"diagnosis" | "report">("diagnosis");
  const [competitorModel, setCompetitorModel] = useState("all");
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
  const modelReports = useMemo(() => report?.models || ["doubao", "yuanbao", "wenxin", "quark", "deepseek", "kimi"].map((id) => ({
    id, completed: 0, target_rounds: ["deepseek", "kimi"].includes(id) ? 2 : 3, recommended_rounds: 0,
    recommendation_rate: 0, average_rank: null, source_count: 0, expected_source_count: 0,
    body_complete_rounds: 0, source_complete_rounds: 0, analysis_complete_rounds: 0, total_body_chars: 0,
  })), [report]);
  const rankedAnswers = useMemo(
    () => (report?.answers || []).filter((answer) => typeof answer.rank === "number" && answer.rank > 0),
    [report],
  );
  const averageRank = rankedAnswers.length
    ? rankedAnswers.reduce((sum, answer) => sum + Number(answer.rank), 0) / rankedAnswers.length
    : null;
  const rankShare = (limit: number) => {
    const calibrated = limit === 1 ? report?.first_share : limit === 3 ? report?.top3_share : report?.top5_share;
    if (typeof calibrated === "number") return calibrated;
    return report?.completed_rounds
      ? Math.round(rankedAnswers.filter((answer) => Number(answer.rank) <= limit).length * 500 / report.completed_rounds) / 10
      : 0;
  };
  const competitionRows = useMemo(() => {
    if (!report) return [];
    const selectedModel = competitorModel === "all" ? null : report.models.find((item) => item.id === competitorModel);
    const denominator = selectedModel?.completed || report.completed_rounds;
    const competitors = (report.competitors || []).filter((competitor) => (
      competitorModel === "all" || (competitor.mention_models || competitor.models).includes(competitorModel)
    )).map((competitor) => ({
      current: false,
      name: competitor.name,
      visibility: competitorModel === "all"
        ? competitor.visibility_score ?? (denominator ? Math.round(competitor.mention_rounds * 1000 / denominator) / 10 : 0)
        : competitor.model_visibility?.[competitorModel] ?? (denominator ? Math.round((competitor.model_mentions?.[competitorModel] || 0) * 1000 / denominator) / 10 : 0),
      recommended: competitorModel === "all" ? competitor.recommended_mentions : competitor.model_recommended_mentions?.[competitorModel] ?? 0,
      platforms: competitor.effective_model_count ?? competitor.model_count,
      models: competitor.mention_models || competitor.models,
      products: competitor.products.slice(0, 2).join("、") || "回答中提及厂商，未识别具体产品",
    }));
    return [{
      current: true,
      name: report.brand_name,
      visibility: selectedModel?.recommendation_rate ?? report.overall_rate,
      recommended: selectedModel?.recommended_rounds ?? report.recommended_rounds,
      platforms: selectedModel ? (selectedModel.recommended_rounds > 0 ? 1 : 0) : report.models.filter((item) => item.recommended_rounds > 0).length,
      models: selectedModel ? [selectedModel.id] : report.models.filter((item) => item.recommended_rounds > 0).map((item) => item.id),
      products: report.product_name || "—",
    }, ...competitors].sort((left, right) => right.visibility - left.visibility || right.platforms - left.platforms || left.name.localeCompare(right.name, "zh-CN"));
  }, [report, competitorModel]);

  useEffect(() => {
    if (task?.status === "completed") setTab((current) => current === "diagnosis" ? "report" : current);
  }, [task?.status]);

  return (
    <main className={`diagnosis-shell diagnosis-shell-${tab}`}>
      <aside className="diagnosis-sidebar">
        <a className="diagnosis-logo diagnosis-logo-enterprise" href="/geo/" aria-label="返回品牌诊断首页"><EnterpriseBrandMark size={44} /></a>
        <span className="diagnosis-report-label">品牌可见度</span>
        <div className="diagnosis-customer">
          <small>安全诊断报告</small>
          <b>{brand}</b>
          <span>/{customerSlug}</span>
        </div>
        <nav>
          <button className={tab === "diagnosis" ? "active" : ""} onClick={() => setTab("diagnosis")}>诊断概览</button>
          <button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")}>推荐报告</button>
        </nav>
        <a className="diagnosis-admin-link" href="/geo/admin">管理员中心</a>
        <div className="diagnosis-model-list">
          {modelReports.map((item) => (
            <div key={item.id}>
              <DiagnosisModelIcon model={item.id} size={28} />
              <span><b>{MODEL_NAMES[item.id]}</b><small>{item.completed > 0 ? "数据已采集" : "等待数据"}</small></span>
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
                <span>六大 AI 平台 · 独立会话采集 · 回答与信源双重核验</span>
                <h2>{question}</h2>
                <p>诊断对象：{brand}{product ? ` · ${product}` : ""}。系统保存豆包、腾讯元宝、文心一言、千问、DeepSeek 和 Kimi 的完整回答正文与引用信源，并计算品牌推荐率。</p>
              </div>
            </section>
            {task && (
              <section className="diagnosis-progress-card">
                <header><div><small>诊断任务进度</small><h3>{task.message || (task.status === "queued" && Number(task.retry_count || 0) > 0 ? `异常平台自动补采中 · 已保存 ${task.completed_steps}/${task.total_steps}` : task.status === "queued" ? "任务已进入队列" : task.status === "running" ? "六模型正在完成诊断" : statusLabel(task.status))}</h3></div><b>{progress}%</b></header>
                <div className={`diagnosis-progress ${task.status === "running" || task.status === "queued" ? "is-live" : ""}`}><i style={{ width: `${progress}%` }} /></div>
                <div className="diagnosis-progress-meta"><span>{task.status === "completed" ? "数据采集与分析已完成" : "当前步骤处理中，完成入库后更新百分比"}</span><span>{task.status === "completed" ? "报告已生成" : "数据完成后自动生成报告"}</span></div>
                {task.error && <p className="diagnosis-error-text">诊断暂未完成，管理员正在处理。</p>}
                <div className="diagnosis-customer-steps">
                  <div className="done"><i />任务已提交</div>
                  <div className={task.status !== "queued" ? "done" : "active"}><i />{task.status === "queued" && Number(task.retry_count || 0) > 0 ? "断点续跑中" : task.status === "queued" ? "等待执行" : "已开始诊断"}</div>
                  <div className={task.completed_steps ? "active" : ""}><i />六模型数据采集中</div>
                  <div className={task.status === "completed" ? "done" : ""}><i />报告已生成</div>
                </div>
              </section>
            )}
            <section className="diagnosis-model-cards">
              {modelReports.map((item) => (
                <article key={item.id}>
                  <DiagnosisModelIcon model={item.id} size={42} />
                  <div><small>{MODEL_NAMES[item.id]}</small><b>{item.completed > 0 ? "已完成" : "待采集"}</b><span>平台数据状态</span></div>
                </article>
              ))}
            </section>
          </>
        )}

        {tab === "report" && (
          report && task ? <>
            <section className="diagnosis-overview">
              <header><div><small>BRAND OVERVIEW</small><h2>品牌概览</h2><p>品牌在 AI 回答中的明确推荐率、平均推荐位置及排名占位</p><div className="diagnosis-overview-tags"><span>独立平台等权</span><span>小样本置信校准</span><span>推荐名次加权</span><span>原始证据可追溯</span></div></div></header>
              <div className="diagnosis-metric-grid">
                <article className={`primary ${recommendationTone(report.overall_rate)}`}><div className="diagnosis-metric-title"><span>平均可见度</span><MetricIcon kind="eye" /></div><strong>{formatPercent(report.overall_rate)}<small>%</small></strong><p>{report.conclusion}</p></article>
                <article><div className="diagnosis-metric-title"><span>平均位置</span><MetricIcon kind="pin" /></div><strong>{averageRank ? averageRank.toFixed(1) : "—"}</strong><p>基于回答中的明确推荐位置</p></article>
                <article><div className="diagnosis-metric-title"><span>首推占比</span><MetricIcon kind="medal" /></div><strong>{formatPercent(rankShare(1))}<small>%</small></strong><p>排名第 1 的保守占比</p></article>
                <article><div className="diagnosis-metric-title"><span>前 3 占比</span><MetricIcon kind="bars" /></div><strong>{formatPercent(rankShare(3))}<small>%</small></strong><p>进入推荐前三的保守占比</p></article>
                <article><div className="diagnosis-metric-title"><span>前 5 占比</span><MetricIcon kind="pie" /></div><strong>{formatPercent(rankShare(5))}<small>%</small></strong><p>进入推荐前五的保守占比</p></article>
              </div>
              <div className="diagnosis-model-probability-grid">
                {report.models.map((item) => <article className={`model-${item.id}`} key={item.id}>
                  <div><DiagnosisModelIcon model={item.id} size={38} /><span>{MODEL_NAMES[item.id] || item.id}</span></div>
                  <strong>{formatPercent(item.recommendation_rate)}<small>%</small></strong>
                  <p>{`校准评分 · 原始命中 ${formatPercent(item.raw_recommendation_rate || 0)}%`}</p>
                </article>)}
              </div>
            </section>
            <section className="diagnosis-platform-card diagnosis-platform-card-priority">
              <header><div><small>AI PLATFORM DETAILS</small><h2>AI 平台维度详情</h2><p>按平台查看经小样本置信度、推荐名次和证据完整度校准后的概率</p></div><span>6 个 AI 平台</span></header>
              <div className="diagnosis-data-table"><table><thead><tr><th>AI 平台</th><th>推荐概率</th><th>平均推荐位置</th><th>首推占比</th><th>前 3 占比</th><th>前 5 占比</th></tr></thead><tbody>
                {report.models.map((item) => { const mirrored = item.data_source === "deepseek_mirror"; const platformAnswers = report.answers.filter((answer) => answer.model === item.id); const platformRanks = platformAnswers.filter((answer) => typeof answer.rank === "number" && answer.rank > 0); const share = (limit: number) => item.completed ? Math.round(platformRanks.filter((answer) => Number(answer.rank) <= limit).length * 500 / item.completed) / 10 : 0; return <tr key={item.id}><td data-label="AI 平台"><span className="diagnosis-platform-name"><DiagnosisModelIcon model={item.id} size={28} /><strong>{MODEL_NAMES[item.id]}</strong></span></td><td data-label="推荐概率"><span className="diagnosis-rate-cell"><i><b style={{ width: `${item.recommendation_rate}%` }} /></i><strong>{formatPercent(item.recommendation_rate)}%</strong></span></td><td data-label="平均位置">{mirrored ? "—" : item.average_rank ? item.average_rank.toFixed(1) : "—"}</td><td data-label="首推占比">{mirrored ? "—" : `${formatPercent(item.first_share ?? share(1))}%`}</td><td data-label="前 3 占比">{mirrored ? "—" : `${formatPercent(item.top3_share ?? share(3))}%`}</td><td data-label="前 5 占比">{mirrored ? "—" : `${formatPercent(item.top5_share ?? share(5))}%`}</td></tr>; })}
              </tbody></table></div>
            </section>
            <section className="diagnosis-ranking-card">
              <header><div><small>COMPETITIVE LANDSCAPE</small><h2>竞争格局</h2><p>综合品牌提及、明确推荐、出现频次与推荐位置计算竞争可见度</p></div><span>{report.competitors?.length || 0} 个有效竞品品牌</span></header>
              <div className="diagnosis-competitor-filters" aria-label="按 AI 模型查看竞品">
                <button className={competitorModel === "all" ? "active" : ""} onClick={() => setCompetitorModel("all")}>全部模型</button>
                {report.models.map((model) => <button className={competitorModel === model.id ? "active" : ""} key={model.id} onClick={() => setCompetitorModel(model.id)}>{MODEL_NAMES[model.id] || model.id}</button>)}
              </div>
              <p className="diagnosis-competition-method">计算口径：明确推荐权重高于普通品牌提及；出现轮次越多、推荐位置越靠前，可见度越高。工艺、设备类型和店铺名称不计作竞品品牌。</p>
              <p className="diagnosis-competitor-scroll-hint">列表区域可上下滑动，查看全部 {competitionRows.length} 个品牌</p>
              <div className="diagnosis-data-table diagnosis-competitor-scroll"><table><thead><tr><th>排名</th><th>品牌</th><th>竞争可见度</th><th>证据类型</th><th>涉及平台</th><th>代表产品</th></tr></thead><tbody>
                {competitionRows.map((row, index) => <tr className={row.current ? "current" : ""} key={row.name}><td data-label="排名"><b>{index + 1}</b></td><td data-label="品牌"><strong>{row.name}</strong>{row.current ? <small>当前品牌</small> : null}</td><td data-label="竞争可见度">{row.current ? <b>{formatPercent(row.visibility)}%</b> : `${formatPercent(row.visibility)}%`}</td><td data-label="证据类型"><span className={`diagnosis-competitor-evidence ${row.recommended > 0 ? "recommended" : "mentioned"}`}>{row.current ? row.recommended > 0 ? "目标被推荐" : "目标未被推荐" : row.recommended > 0 ? "明确推荐" : "品牌提及"}</span></td><td data-label="涉及平台"><div className="diagnosis-model-tags">{row.models.map((model) => <span key={model}>{MODEL_NAMES[model] || model}</span>)}</div></td><td data-label="代表产品">{row.products}</td></tr>)}
              </tbody></table></div>
            </section>
            <section className="diagnosis-source-landscape">
              <header><div><small>SOURCE LANDSCAPE</small><h2>信源来源结构</h2><p>本次诊断引用信源的集中度与主要域名</p></div><span>{report.source_analysis?.unique_domains || 0} 个域名</span></header>
              <div className="diagnosis-source-layout"><div className="diagnosis-source-metrics"><p><b>{report.source_analysis?.total_citations || 0}</b><span>总引用次数</span></p><p><b>{report.source_analysis?.unique_links || 0}</b><span>唯一链接</span></p><p><b>{report.source_analysis?.cross_model_links || 0}</b><span>跨模型共同信源</span></p></div><div className="diagnosis-domain-list">{report.source_analysis?.top_domains?.slice(0, 12).map((item, index) => <div key={item.domain}><span>{index + 1}. {item.domain}</span><b>{item.citations} 次</b></div>)}</div></div>
            </section>
            <section id="diagnosis-evidence-card" className="diagnosis-evidence diagnosis-evidence-embedded">
              <header><div><small>SAMPLED ANSWER AUDIT</small><h2>抽样回答与信源</h2><p>以下为本次诊断的抽样证据；点击任意记录即可展开正文和引用链接</p></div><button type="button" onClick={() => setExpandAllEvidence((value) => !value)}>{expandAllEvidence ? "收起全部" : "展开全部样本"}</button></header>
              <div className="diagnosis-evidence-model-list">
                {report.models.map((modelReport) => {
                  const modelAnswers = report.answers.filter((answer) => answer.model === modelReport.id);
                  return <section className="diagnosis-evidence-model" key={modelReport.id}>
                    <h3><DiagnosisModelIcon model={modelReport.id} size={30} />{MODEL_NAMES[modelReport.id]}<span>{modelAnswers.length ? "抽样证据已归档" : "暂无有效数据"}</span></h3>
                    {modelAnswers.map((answer, index) => <details open={expandAllEvidence || undefined} key={`${answer.model}-${answer.round}-${index}`}>
                      <summary><b>抽样记录 {String.fromCharCode(65 + index)}</b><small>正文{answer.body_capture_complete ? "完整" : "待重新采集"}</small><small>引用信源{answer.source_capture_complete ? "完整" : "待核对"}</small></summary>
                      <div className="diagnosis-answer"><h4>{answer.question}</h4><div className="diagnosis-answer-audit"><span className={answer.body_capture_complete ? "ok" : "warn"}>回答正文：{answer.body_capture_complete ? "完整" : "仅保存到部分内容"}</span><span className={answer.source_capture_complete ? "ok" : "warn"}>引用信源：{answer.source_capture_complete ? "完整" : "待核对"}</span><span className={answer.analysis.complete ? "ok" : "warn"}>证据判定：{answer.analysis.complete ? answer.recommended ? `明确推荐${answer.rank ? ` · 第${answer.rank}名` : ""}` : answer.mentioned ? "仅提及，不计入推荐" : "未提及" : "待完成"}</span></div>{answer.analysis.summary && <div className="diagnosis-answer-summary"><b>回答分析摘要</b><p>{answer.analysis.summary}</p>{answer.analysis.brands?.length ? <span>涉及品牌：{answer.analysis.brands.join("、")}</span> : null}</div>}<div className="diagnosis-answer-body">{answerParagraphs(answer.answer).map((paragraph, paragraphIndex) => <p key={paragraphIndex}>{paragraph}</p>)}</div><div className="diagnosis-answer-sources"><h5>本次抽样回答信源</h5>{answer.sources.length > 0 ? <ul>{answer.sources.map((source, sourceIndex) => { const url = source.url || source.href || ""; return <li key={`${url}-${sourceIndex}`}><a href={url} target="_blank" rel="noreferrer"><b>{source.title || url}</b><small>{url}</small></a></li>; })}</ul> : <p>本次抽样回答未引用外部信源。</p>}</div></div>
                    </details>)}
                    {modelAnswers.length ? <div className="diagnosis-sample-end" aria-label="抽样展示结束"><b>•••</b><span>抽样展示结束</span></div> : null}
                  </section>;
                })}
              </div>
            </section>
          </> : <div className="diagnosis-empty"><h2>推荐报告正在生成</h2><p>完成数据采集后，这里会实时出现模型推荐率和信源数据。</p><button onClick={() => setTab("diagnosis")}>查看诊断进度</button></div>
        )}
      </section>
    </main>
  );
}
