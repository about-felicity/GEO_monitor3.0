"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getIcon } from "./ModelIcon";

type Source = {
  title: string;
  url: string;
  canonical_url: string;
  media: string;
  type: string;
  count?: number;
  brand_mentions?: string[];
  owned_brands?: string[];
  own_products?: string[];
  own_brand?: boolean;
  brand_match_scope?: string;
  content_analysis_status?: "title_only" | "complete" | "pending" | "failed";
  body_analysis_ready?: boolean;
};
type CommonOwnedSource = Source & {
  date: string;
  question: string;
  competitor_brand?: string;
  competitor_brands?: string[];
  body_match_scope?: string;
  model_counts: Record<string, number>;
  matched_models: string[];
  total_count: number;
};
type CountItem = { name: string; count: number };
type Keyword = { term: string; count: number };
type Run = {
  run_id: string;
  sequence: number;
  question: string;
  finished_at: string;
  serial: string;
  answer: string;
  status: string;
  sources: Source[];
};
type MentionItem = { name: string; mentions: number; mention_rate: number; rank: number; rank_change?: number | null; average_position?: number | null };
type MentionDay = { date: string; runs: number; total_runs?: number; pending_runs?: number; items: MentionItem[] };
type BrandSourceItem = { name: string; mentions: number; mention_rate: number; eligible_sources?: number; pending_sources?: number; failed_sources?: number };
type BrandSourceDay = { date: string; sources: number; items: BrandSourceItem[] };
type SourceBrandDay = { date: string; runs: number; sources: number; body_ready_sources?: number; body_pending_sources?: number; body_failed_sources?: number; branded_eligible_sources?: number; branded_sources: number; branded_source_rate: number; title_eligible_sources?: number; title_branded_sources: number; title_branded_source_rate: number; owned_eligible_sources?: number; owned_sources: number; owned_source_rate: number; article_keywords: Keyword[]; video_keywords: Keyword[] };
type OwnedVideoCategoryRow = {
  category: string;
  all_unique_links: number;
  video_unique_links: number;
  owned_video_unique_links: number;
  owned_video_refs: number;
  owned_video_link_share: number;
  owned_within_video_link_share: number;
  owned_brands: string[];
};
type OwnedProductModelStatus = {
  state: "listed" | "not_listed" | "pending" | "not_collected";
  eligible_runs: number;
  reviewed_runs: number;
  pending_runs: number;
  recommendation_runs: number;
  body_match_runs: number;
  structured_match_runs: number;
  recommendation_rate: number;
};
type OwnedProductDailyRow = {
  date: string;
  question: string;
  product: string;
  brand: string;
  models: Record<string, OwnedProductModelStatus>;
  listed_model_count: number;
};
type Model = {
  id: string;
  name: string;
  short_name: string;
  tone: string;
  runs: number;
  sources: number;
  unique_sources: number;
  question_count: number;
  device_count: number;
  analysis_ready_runs?: number;
  analysis_pending_runs?: number;
  source_types: CountItem[];
  media: CountItem[];
  daily: {
    date: string;
    runs: number;
    sources: number;
    unique_sources: number;
  }[];
  questions: {
    question: string;
    runs: number;
    sources: number;
    unique_sources: number;
    avg_sources: number;
  }[];
  top_articles: Source[];
  top_videos: Source[];
  owned_sources?: Source[];
  article_keywords: Keyword[];
  video_keywords: Keyword[];
  brand_daily: MentionDay[];
  product_daily: MentionDay[];
  brand_trend_daily: MentionDay[];
  product_trend_daily: MentionDay[];
  brand_source_daily: BrandSourceDay[];
  source_brand_daily: SourceBrandDay[];
  daily_source_top: { date: string; top_articles: Source[]; top_videos: Source[] }[];
  owned_source_count: number;
  owned_source_eligible_count?: number;
  branded_source_count: number;
  branded_source_eligible_count?: number;
  source_body_ready_count?: number;
  source_body_pending_count?: number;
  source_body_failed_count?: number;
  recent_runs?: Run[];
};
type CatalogModel = {
  id: string;
  name: string;
  short_name: string;
  tone: string;
  supports_control: boolean;
  execution: "local";
  ingest_only?: boolean;
};
type Analytics = {
  generated_at: string;
  filters?: { model?: string; question?: string; date?: string; view?: string };
  detail_scope?: {
    kind: "latest_day";
    dates: string[];
    date_from: string;
    date_to: string;
  };
  models: Model[];
  model_catalog: CatalogModel[];
  questions: string[];
  dates: string[];
  common_owned_sources?: CommonOwnedSource[];
  two_model_owned_sources?: CommonOwnedSource[];
  competitor_brands?: string[];
  common_competitor_sources?: CommonOwnedSource[];
  two_model_competitor_sources?: CommonOwnedSource[];
  common_all_competitor_sources?: CommonOwnedSource[];
  two_model_all_competitor_sources?: CommonOwnedSource[];
  owned_product_daily?: OwnedProductDailyRow[];
  owned_brands?: { name: string; aliases: string[] }[];
  doubao_owned_video_category_share?: {
    rows: OwnedVideoCategoryRow[];
    first_date: string;
    last_date: string;
    definitions?: Record<string, string>;
  };
  analysis_method?: Record<string, string | number>;
};
type ControlState = {
  running: boolean;
  starting?: boolean;
  phase?: string;
  startup_error?: string;
  ready: boolean;
  pid?: number;
  started_at?: string;
  last_exit_code?: number;
  log?: string;
};
type AccountState = {
  ok: boolean;
  status: string;
  message: string;
  web?: { masked?: string };
  location?: string;
};
type BrandSourceLink = Source & { match_scope?: string };
type BrandSourceLinkDay = { date: string; sources: number; eligible_sources?: number; pending_sources?: number; failed_sources?: number; mentions: number; mention_rate: number; links: BrandSourceLink[] };
type View = "overview" | "compare" | "sources" | "brands" | "runs" | "control";
type QuestionMode = "interleaved" | "sequential";

const emptyAnalytics: Analytics = {
  generated_at: "",
  models: [],
  model_catalog: [],
  questions: [],
  dates: [],
};
const views: { id: View; label: string; hint: string; symbol: string }[] = [
  { id: "overview", label: "模型总览", hint: "先看规模与质量", symbol: "◈" },
  { id: "compare", label: "问题对比", hint: "同一问题横向比较", symbol: "⇄" },
  { id: "sources", label: "信源洞察", hint: "每日链接 Top 25 与关键词", symbol: "◎" },
  { id: "brands", label: "品牌与产品", hint: "提及率、排名与自有信源", symbol: "◇" },
  { id: "runs", label: "回答审计", hint: "逐轮查看原始证据", symbol: "≡" },
  { id: "control", label: "采集控制", hint: "问题、账号与任务", symbol: "▶" },
];

function apiBase() {
  const localApiPort = import.meta.env.VITE_MONITOR_API_PORT || "8765";
  const configuredBasePath = String(import.meta.env.VITE_MONITOR_BASE_PATH || "")
    .trim()
    .replace(/\/$/, "");
  if (typeof window === "undefined") return `http://127.0.0.1:${localApiPort}`;
  if (window.location.port === localApiPort) return "";
  if (window.location.port === "3000") {
    return `${window.location.protocol}//${window.location.hostname}:${localApiPort}`;
  }
  // Production Nginx exposes the frontend and /api on one HTTPS origin.
  return configuredBasePath;
}
function fmt(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}
function pct(value: number, total: number) {
  return total ? `${((value * 100) / total).toFixed(1)}%` : "0%";
}
function timeText(value?: string) {
  if (!value) return "—";
  const date = new Date(value.replace(" ", "T"));
  return Number.isNaN(date.getTime())
    ? value.slice(0, 16)
    : date.toLocaleString("zh-CN", {
        hour12: false,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function beijingTime(value?: string) {
  if (!value) return "等待接收";
  const date = new Date(value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value.slice(5, 16).replace("T", " ");
  return date
    .toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai",
      hour12: false,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    })
    .replaceAll("/", "-");
}

function Empty({ children }: { children: string }) {
  return (
    <div className="empty">
      <b>◇</b>
      <span>{children}</span>
    </div>
  );
}
function SectionTitle({
  eyebrow,
  title,
  note,
}: {
  eyebrow?: string;
  title: string;
  note?: string;
}) {
  return (
    <div className="section-title">
      <div>
        {eyebrow && <span>{eyebrow}</span>}
        <h2>{title}</h2>
      </div>
      {note && <small>{note}</small>}
    </div>
  );
}
function Bars({ items, total }: { items: CountItem[]; total: number }) {
  const max = Math.max(1, ...items.map((item) => item.count));
  if (!items.length) return <Empty>当前范围暂无数据</Empty>;
  return (
    <div className="bars">
      {items.slice(0, 9).map((item) => (
        <div className="bar" key={item.name}>
          <div>
            <b>{item.name}</b>
            <span>
              {fmt(item.count)} · {pct(item.count, total)}
            </span>
          </div>
          <i>
            <em style={{ width: `${(item.count * 100) / max}%` }} />
          </i>
        </div>
      ))}
    </div>
  );
}
function OwnedVideoCategoryShare({ data }: { data: NonNullable<Analytics["doubao_owned_video_category_share"]> }) {
  const rows = data.rows || [];
  const period = data.first_date && data.last_date
    ? `${data.first_date} 至 ${data.last_date}`
    : "当前筛选范围";
  return (
    <section className="panel owned-video-category-panel">
      <SectionTitle
        eyebrow="DOUBAO OWNED VIDEO SHARE"
        title="豆包各产品品类 · 自有品牌视频信源链接占比"
        note={`${period} · 唯一链接口径`}
      />
      <p className="owned-video-category-note">
        主占比＝命中自有品牌或自有产品的视频唯一链接 ÷ 该品类全部唯一信源链接；同一链接跨轮重复抓取只计一次。
      </p>
      <div className="compare-table owned-video-category-table">
        <table>
          <thead>
            <tr>
              <th>产品品类</th><th>自有品牌</th><th>全部唯一链接</th><th>全部视频链接</th>
              <th>自有品牌视频链接</th><th>占品类全部链接</th><th>占品类视频链接</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.category}>
                <td><b>{row.category}</b></td>
                <td>{row.owned_brands?.join("、") || "—"}</td>
                <td>{fmt(row.all_unique_links)}</td>
                <td>{fmt(row.video_unique_links)}</td>
                <td><strong>{fmt(row.owned_video_unique_links)}</strong><small>{fmt(row.owned_video_refs)} 次引用</small></td>
                <td><span className="owned-video-share-value">{row.owned_video_link_share.toFixed(2)}%</span><small>{row.owned_video_unique_links}/{row.all_unique_links}</small></td>
                <td>{row.owned_within_video_link_share.toFixed(2)}%<small>{row.owned_video_unique_links}/{row.video_unique_links}</small></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DailyOwnedProductBoard({
  rows,
  models,
}: {
  rows: OwnedProductDailyRow[];
  models: Model[];
}) {
  const groups = Array.from(
    rows.reduce((map, row) => {
      const group = map.get(row.date) || [];
      group.push(row);
      map.set(row.date, group);
      return map;
    }, new Map<string, OwnedProductDailyRow[]>()).entries(),
  );
  const stateText = (status?: OwnedProductModelStatus) => {
    if (!status || status.state === "not_collected") return { label: "未采集", detail: "没有对应问题", className: "not-collected" };
    if (status.state === "listed") return {
      label: "已上榜",
      detail: `${status.recommendation_runs}/${status.eligible_runs} 轮推荐 · 正文明确命中${status.body_match_runs || 0}轮${status.pending_runs ? ` · ${status.pending_runs}轮待复核` : ""}`,
      className: "listed",
    };
    if (status.state === "pending") return {
      label: "待复核",
      detail: `${status.reviewed_runs}/${status.eligible_runs} 轮已完成`,
      className: "pending",
    };
    return { label: "未上榜", detail: `0/${status.eligible_runs} 轮推荐`, className: "not-listed" };
  };
  return (
    <section className="panel owned-product-board">
      <SectionTitle
        eyebrow="DAILY OWNED PRODUCT BOARD"
        title="我的产品每日上榜情况"
        note="任意1轮回答正文确认推荐，即记为上榜"
      />
      <p className="owned-product-board-note">
        上榜以对应问题正文中“自有品牌名＋产品名”的明确命名为直接证据，并与已复核产品结果合并；没跑对应问题显示“未采集”，无命中但分析未完成显示“待复核”，不会误判成未上榜。
      </p>
      {groups.length ? groups.map(([day, dayRows]) => (
        <div className="owned-product-day" key={day}>
          <h3><time>{day}</time><span>{dayRows.filter((row) => row.listed_model_count > 0).length}/{dayRows.length} 个产品至少在一个模型上榜</span></h3>
          <div className="owned-product-table">
            <table>
              <thead><tr><th>我的产品</th><th>对应问题</th>{models.map((item) => <th key={item.id}>{item.name}</th>)}</tr></thead>
              <tbody>{dayRows.map((row) => (
                <tr key={`${row.date}-${row.question}-${row.product}`}>
                  <td><b>{row.product}</b><small>{row.brand}</small></td>
                  <td>{row.question}</td>
                  {models.map((item) => {
                    const display = stateText(row.models[item.id]);
                    return <td key={item.id}><span className={`owned-product-state ${display.className}`}><b>{display.label}</b><small>{display.detail}</small></span></td>;
                  })}
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      )) : <Empty>当前日期还没有与自有产品对应的采集问题</Empty>}
    </section>
  );
}
function SourceTop({
  title,
  items,
  tone,
}: {
  title: string;
  items: Source[];
  tone: string;
}) {
  const [visibleItems, setVisibleItems] = useState(25);
  const shownItems = items.slice(0, visibleItems);
  return (
    <article className="source-block">
      <div className="source-block-title">
        <span className={`source-kind ${tone}`}>
          {tone === "video" ? "视频" : "文章"}
        </span>
        <h3>{title}</h3>
        <small>每轮同链接只计一次</small>
      </div>
      {items.length ? (
        <ol>
          {shownItems.map((item, index) => (
            <li key={`${item.canonical_url || item.url}-${index}`}>
              <span>{index + 1}</span>
              <a href={item.url} target="_blank" rel="noreferrer">
                <b>{item.title || item.url}</b>
                <small>
                  {item.media} · 出现 {item.count || 1} 轮
                </small>
                {item.own_brand && (
                  <em className="owned-source">自有品牌 · {item.owned_brands?.join("、") || item.own_products?.join("、")} · {item.brand_match_scope}</em>
                )}
                {!item.own_brand && tone !== "video" && item.content_analysis_status === "pending" && (
                  <em className="source-analysis-pending">正文待核验 · 暂不判定</em>
                )}
              </a>
            </li>
          ))}
        </ol>
      ) : (
        <Empty>暂无对应信源</Empty>
      )}
      {visibleItems < items.length && <button type="button" className="trend-more" onClick={() => setVisibleItems((value) => value + 50)}>继续显示（剩余 {items.length - visibleItems} 条）</button>}
    </article>
  );
}
function CommonSourceIntersections({
  items,
  mode,
  targetKind,
  targetLabel,
}: {
  items: CommonOwnedSource[];
  mode: "all" | "two";
  targetKind: "owned" | "competitor";
  targetLabel: string;
}) {
  const isAll = mode === "all";
  const isCompetitor = targetKind === "competitor";
  const heading = isCompetitor
    ? isAll
      ? `五模型共同提取且正文命中「${targetLabel}」的信源链接`
      : `恰好被两个模型共同提取且正文命中「${targetLabel}」的信源链接`
    : isAll
      ? "五模型共同提取的自有信源链接"
      : "恰好被两个模型共同提取的自有信源链接";
  const groups = Array.from(
    items.reduce((map, item) => {
      const key = `${item.date}\u0000${item.question}`;
      const group = map.get(key) || { date: item.date, question: item.question, links: [] as CommonOwnedSource[] };
      group.links.push(item);
      map.set(key, group);
      return map;
    }, new Map<string, { date: string; question: string; links: CommonOwnedSource[] }>()).values(),
  );
  return (
    <section className={`panel common-owned-panel ${isAll ? "all-model-panel" : "two-model-panel"} ${isCompetitor ? "competitor-panel" : ""}`}>
      <div className="common-owned-heading">
        <div>
          <span>{isAll ? "五模型交集" : "双模型交集"}</span>
          <h2>{heading}</h2>
        </div>
        <b>{items.length} 条</b>
      </div>
      <p className="common-owned-note">
        {isCompetitor
          ? isAll
            ? "仅使用高/中质量文章正文解析结果；严格按自然日、产品问题、竞品和规范化链接匹配，标题命中不计入。"
            : "仅使用高/中质量文章正文解析结果；显示恰好被两个模型引用的链接，五模型交集不重复展示。"
          : isAll
            ? "严格按自然日、产品问题和规范化链接匹配；每轮同一链接只计一次，未同时被五个模型提取的链接不会显示。"
            : "仅显示恰好命中两个模型的链接，已进入五模型交集的链接不重复展示；每轮同一链接只计一次。"}
      </p>
      {groups.length ? groups.map((group) => (
        <article className="common-owned-group" key={`${group.date}-${group.question}`}>
          <h3><time>{group.date}</time><span>{group.question}</span></h3>
          <div className="common-owned-table">
            {group.links.map((item) => (
              <div className="common-owned-row" key={`${group.date}-${group.question}-${item.competitor_brand || "owned"}-${item.canonical_url}`}>
                <a href={item.url} target="_blank" rel="noreferrer">
                  <b>{item.title || item.url}</b>
                  <small>
                    {item.media || item.canonical_url} · {isCompetitor
                      ? `${item.competitor_brands?.join("、") || item.competitor_brand || targetLabel} · 文章正文命中`
                      : item.owned_brands?.join("、") || item.own_products?.join("、") || "自有产品"}
                  </small>
                </a>
                <div className="common-model-counts">
                  {!!item.model_counts.doubao && <span className="doubao">豆包 <b>{item.model_counts.doubao}</b></span>}
                  {!!item.model_counts.yuanbao && <span className="yuanbao">元宝 <b>{item.model_counts.yuanbao}</b></span>}
                  {!!item.model_counts.wenxin && <span className="wenxin">文心 <b>{item.model_counts.wenxin}</b></span>}
                  {!!item.model_counts.deepseek && <span className="deepseek">DeepSeek <b>{item.model_counts.deepseek}</b></span>}
                  {!!item.model_counts.quark && <span className="quark">夸克 <b>{item.model_counts.quark}</b></span>}
                  <em>合计 {item.total_count}</em>
                </div>
              </div>
            ))}
          </div>
        </article>
      )) : <Empty>{isCompetitor
        ? `当前问题和日期范围内，暂无${isAll ? "五个模型" : "恰好两个模型"}共同提取且正文命中「${targetLabel}」的链接`
        : isAll
          ? "当前问题和日期范围内，暂无同时被五个模型提取的自有信源链接"
          : "当前问题和日期范围内，暂无恰好被两个模型提取的自有信源链接"}</Empty>}
    </section>
  );
}
function Keywords({
  title,
  items,
  tone,
}: {
  title: string;
  items: Keyword[];
  tone: string;
}) {
  return (
    <div className="keyword-box">
      <div>
        <b>{title}</b>
        <small>按信源标题文案的跨链接出现次数</small>
      </div>
      {items.length ? (
        <div className="keyword-cloud">
          {items.map((item, index) => (
            <span
              className={`${tone} size-${Math.min(3, Math.floor(index / 5))}`}
              key={item.term}
            >
              {item.term}
              <i>{item.count}</i>
            </span>
          ))}
        </div>
      ) : (
        <Empty>样本不足，至少需两个标题共同出现</Empty>
      )}
    </div>
  );
}

function MentionTrend({ title, days, kind }: { title: string; days: MentionDay[]; kind: "brand" | "product" }) {
  const rows = days.slice(-7).reverse().flatMap((day) => day.items.map((item) => ({ ...item, date: day.date, runs: day.runs, pendingRuns: day.pending_runs || 0 })));
  const pendingRuns = kind === "product" ? days.slice(-7).reduce((sum, day) => sum + (day.pending_runs || 0), 0) : 0;
  const [visibleRows, setVisibleRows] = useState(25);
  const shownRows = rows.slice(0, visibleRows);
  return (
    <article className="trend-block">
      <h3>{title}</h3>
      <small>{kind === "brand" ? "品牌提及率 = 正文明确提及轮次 ÷ 当日全部有效回答轮次；不等待产品 AI 解析" : `产品提及率 = 提及轮次 ÷ 已完成产品解析轮次；待解析不计为未提及${pendingRuns ? `（近 7 日仍有 ${pendingRuns} 轮待解析）` : ""}`}</small>
      {rows.length ? <div className="trend-table"><table><thead><tr><th>日期</th><th>名称</th><th>提及</th><th>提及率</th><th>名次</th><th>变化</th><th>正文位次</th></tr></thead><tbody>
        {shownRows.map((row) => <tr key={`${row.date}-${row.name}`}><td>{row.date}</td><td><b>{row.name}</b></td><td>{row.mentions}/{row.runs}{kind === "product" && row.pendingRuns ? <small> · {row.pendingRuns}待解析</small> : null}</td><td>{row.mention_rate.toFixed(1)}%</td><td>{kind === "product" && row.pendingRuns ? "暂 " : ""}#{row.rank}</td><td className={(row.rank_change || 0) > 0 ? "rise" : (row.rank_change || 0) < 0 ? "fall" : ""}>{row.rank_change == null ? "新" : row.rank_change > 0 ? `↑${row.rank_change}` : row.rank_change < 0 ? `↓${Math.abs(row.rank_change)}` : "—"}</td><td>{row.average_position ? row.average_position.toFixed(1) : "—"}</td></tr>)}
      </tbody></table>{visibleRows < rows.length && <button type="button" className="trend-more" onClick={() => setVisibleRows((value) => value + 50)}>继续显示（剩余 {rows.length - visibleRows} 条）</button>}</div> : <Empty>当前范围暂无正文品牌或产品结果</Empty>}
    </article>
  );
}

function SourceBrandTrend({ days }: { days: SourceBrandDay[] }) {
  return <article className="trend-block"><h3>品牌信源与自有品牌信源变化</h3><small>比例只以已核验信源为分母；待分析与抓取失败单列，不会误算为未提及</small>
    {days.length ? <div className="trend-table"><table><thead><tr><th>日期</th><th>观测信源</th><th>正文状态</th><th>标题含品牌</th><th>标题/正文含品牌</th><th>自有品牌</th></tr></thead><tbody>
      {days.slice(-14).reverse().map((row) => <tr key={row.date}><td>{row.date}</td><td>{row.sources}</td><td>{row.body_ready_sources ?? row.sources}/{row.sources} 已核验{row.body_pending_sources ? <small> · {row.body_pending_sources}待</small> : null}{row.body_failed_sources ? <small> · {row.body_failed_sources}失败</small> : null}</td><td>{row.title_branded_sources}/{row.title_eligible_sources ?? row.sources} · {row.title_branded_source_rate.toFixed(1)}%</td><td>{row.branded_sources}/{row.branded_eligible_sources ?? row.sources} · {row.branded_source_rate.toFixed(1)}%</td><td>{row.owned_sources}/{row.owned_eligible_sources ?? row.sources} · {row.owned_source_rate.toFixed(1)}%</td></tr>)}
    </tbody></table></div> : <Empty>当前范围暂无信源品牌数据</Empty>}
  </article>;
}

type RatePoint = { date: string; value: number; mentions: number; total: number; observedTotal?: number; pending?: number; failed?: number };
type RateSeries = { label: string; color: string; values: RatePoint[] };

function RateLineChart({ dates, series, selectedDate, onSelectDate }: { dates: string[]; series: RateSeries[]; selectedDate?: string; onSelectDate?: (date: string) => void }) {
  const width = 820, height = 250, left = 48, right = 20, top = 20, bottom = 48;
  const innerWidth = width - left - right, innerHeight = height - top - bottom;
  const x = (index: number) => left + (dates.length <= 1 ? innerWidth / 2 : index * innerWidth / (dates.length - 1));
  const y = (value: number) => top + (100 - Math.max(0, Math.min(100, value))) * innerHeight / 100;
  const valueMaps = series.map((item) => new Map(item.values.map((point) => [point.date, point])));
  return (
    <div className="rate-chart-wrap">
      <div className="rate-chart-legend">{series.map((item) => <span key={item.label}><i style={{ background: item.color }} />{item.label}</span>)}</div>
      <svg className="rate-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="每日提及率折线图">
        {[0, 25, 50, 75, 100].map((tick) => <g key={tick}><line x1={left} y1={y(tick)} x2={width - right} y2={y(tick)} className="chart-grid" /><text x={left - 8} y={y(tick) + 4} textAnchor="end">{tick}%</text></g>)}
        {selectedDate && dates.includes(selectedDate) && <rect className="chart-date-focus" x={x(dates.indexOf(selectedDate)) - 17} y={top} width={34} height={innerHeight} rx={8} />}
        {series.map((item, seriesIndex) => {
          const points = dates.map((date, index) => `${x(index)},${y(valueMaps[seriesIndex].get(date)?.value || 0)}`).join(" ");
          return <g key={item.label}><polyline points={points} fill="none" stroke={item.color} strokeWidth="3" strokeLinejoin="round" strokeLinecap="round" />{dates.map((date, index) => {
            const point = valueMaps[seriesIndex].get(date) || { date, value: 0, mentions: 0, total: 0 };
            return <circle key={date} cx={x(index)} cy={y(point.value)} r={selectedDate === date ? 6 : 4} fill={item.color} className="chart-point" onClick={() => onSelectDate?.(date)}><title>{date} · {item.label} {point.value.toFixed(1)}%（{point.mentions}/${point.total}{point.pending ? `，另有 ${point.pending} 待分析` : ""}${point.failed ? `，${point.failed} 抓取失败` : ""}）</title></circle>;
          })}</g>;
        })}
        {dates.map((date, index) => <text key={date} x={x(index)} y={height - 18} textAnchor="middle" className="chart-date" onClick={() => onSelectDate?.(date)}>{date.slice(5)}</text>)}
      </svg>
    </div>
  );
}

function BrandProductTrendExplorer({ model, question, focusDate, ownedBrands, loadHistory }: { model: Model; question: string; focusDate: string; ownedBrands: { name: string; aliases: string[] }[]; loadHistory: boolean }) {
  const [historicalModel, setHistoricalModel] = useState<Model | null>(null);
  const [historyRequested, setHistoryRequested] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const trendModel = historicalModel || model;
  const ownedBrandNames = useMemo(() => new Set(ownedBrands.flatMap((item) => [item.name, ...(item.aliases || [])])), [ownedBrands]);
  const brandOptions = useMemo(() => Array.from(new Set([
    ...ownedBrands.map((item) => item.name),
    ...(trendModel.brand_trend_daily || []).flatMap((day) => day.items.map((item) => item.name)),
    ...(trendModel.brand_source_daily || []).flatMap((day) => day.items.map((item) => item.name)),
  ])).sort((a, b) => Number(ownedBrandNames.has(b)) - Number(ownedBrandNames.has(a)) || a.localeCompare(b, "zh-CN")), [trendModel.brand_trend_daily, trendModel.brand_source_daily, ownedBrands, ownedBrandNames]);
  const defaultBrand = useMemo(() => {
    for (const item of ownedBrands) {
      const match = [item.name, ...(item.aliases || [])].find((name) => brandOptions.includes(name));
      if (match) return match;
    }
    return brandOptions[0] || "";
  }, [brandOptions, ownedBrands]);
  const [brand, setBrand] = useState(defaultBrand);
  const [linkDays, setLinkDays] = useState<BrandSourceLinkDay[]>([]);
  const [linkLoading, setLinkLoading] = useState(false);
  const allDates = useMemo(() => Array.from(new Set([
    ...(trendModel.brand_trend_daily || []).map((day) => day.date),
    ...(trendModel.brand_source_daily || []).map((day) => day.date),
    ...(trendModel.product_trend_daily || []).map((day) => day.date),
  ])).sort().slice(-14), [trendModel.brand_trend_daily, trendModel.brand_source_daily, trendModel.product_trend_daily]);
  const [selectedDate, setSelectedDate] = useState(focusDate && allDates.includes(focusDate) ? focusDate : allDates.at(-1) || "");
  const activeBrand = brandOptions.includes(brand) ? brand : defaultBrand;
  const activeDate = allDates.includes(selectedDate)
    ? selectedDate
    : focusDate && allDates.includes(focusDate)
      ? focusDate
      : allDates.at(-1) || "";

  useEffect(() => {
    if (!loadHistory || !historyRequested || !focusDate) return;
    const controller = new AbortController();
    const params = new URLSearchParams({
      view: "brand-trends", model: model.id, date: focusDate,
    });
    if (question) params.set("question", question);
    fetch(`${apiBase()}/api/analytics?${params}`, {
      cache: "no-cache", signal: controller.signal,
    })
      .then((response) => response.ok ? response.json() : Promise.reject(new Error("趋势读取失败")))
      .then((payload) => {
        const next = (payload.models || []).find((item: Model) => item.id === model.id);
        if (next) setHistoricalModel(next);
      })
      .catch((reason) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) void reason;
      })
      .finally(() => { if (!controller.signal.aborted) setHistoryLoading(false); });
    return () => controller.abort();
  }, [loadHistory, historyRequested, model.id, question, focusDate]);

  useEffect(() => {
    if (!activeBrand) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ model: model.id, brand: activeBrand });
    if (question) params.set("question", question);
    if (activeDate || focusDate) params.set("date", activeDate || focusDate);
    queueMicrotask(() => {
      if (!controller.signal.aborted) setLinkLoading(true);
    });
    fetch(`${apiBase()}/api/analytics/brand-sources?${params}`, { cache: "no-cache", signal: controller.signal })
      .then((response) => response.json())
      .then((payload) => { if (payload.ok) setLinkDays(payload.days || []); })
      .catch((reason) => { if (!(reason instanceof DOMException && reason.name === "AbortError")) setLinkDays([]); })
      .finally(() => { if (!controller.signal.aborted) setLinkLoading(false); });
    return () => controller.abort();
  }, [activeBrand, model.id, question, focusDate, activeDate]);

  const brandAnswer: RatePoint[] = (trendModel.brand_trend_daily || []).map((day) => {
    const item = day.items.find((row) => row.name === activeBrand);
    return { date: day.date, value: item?.mention_rate || 0, mentions: item?.mentions || 0, total: day.runs };
  });
  const brandSources: RatePoint[] = (trendModel.brand_source_daily || []).map((day) => {
    const item = day.items.find((row) => row.name === activeBrand);
    const eligible = item?.eligible_sources ?? day.sources;
    return { date: day.date, value: item?.mention_rate || 0, mentions: item?.mentions || 0, total: eligible, observedTotal: day.sources, pending: item?.pending_sources ?? Math.max(0, day.sources - eligible - (item?.failed_sources || 0)), failed: item?.failed_sources || 0 };
  });
  const brandProducts = Array.from(new Set((trendModel.product_trend_daily || []).flatMap((day) => day.items.map((item) => item.name)).filter((name) => name === activeBrand || name.startsWith(`${activeBrand} `))));
  const productSeries: RateSeries[] = brandProducts.slice(0, 6).map((name, index) => ({
    label: name.startsWith(`${activeBrand} `) ? name.slice(activeBrand.length + 1) : name,
    color: ["#6f5bd3", "#2979d6", "#d6588d", "#8a6b28", "#59646f", "#c15d37"][index],
    values: (trendModel.product_trend_daily || []).map((day) => {
      const item = day.items.find((row) => row.name === name);
      return { date: day.date, value: item?.mention_rate || 0, mentions: item?.mentions || 0, total: day.runs };
    }),
  }));
  const selectedLinks = linkDays.find((day) => day.date === activeDate);
  const selectedSourcePoint = brandSources.find((point) => point.date === activeDate);
  const selectedSourceMentions = selectedLinks?.mentions ?? selectedSourcePoint?.mentions ?? 0;
  const selectedSourceEligible = selectedLinks?.eligible_sources ?? selectedSourcePoint?.total ?? 0;
  const selectedSourceObserved = selectedLinks?.sources ?? selectedSourcePoint?.observedTotal ?? selectedSourceEligible;
  const selectedSourcePending = selectedLinks?.pending_sources ?? selectedSourcePoint?.pending ?? Math.max(0, selectedSourceObserved - selectedSourceEligible);
  const selectedSourceFailed = selectedLinks?.failed_sources ?? selectedSourcePoint?.failed ?? 0;
  const selectedSourceRate = selectedLinks?.mention_rate ?? selectedSourcePoint?.value ?? 0;

  return <section className="brand-trend-explorer">
    <div className="trend-explorer-head"><div><span>INTERACTIVE TREND</span><h3>品牌与信源提及率</h3><small>{historyLoading ? "当天数据已显示；最近 14 个自然日趋势正在后台补齐…" : allDates.length > 1 ? "只需选择品牌；折线展示当前问题最近 14 个自然日，点击日期可查看对应竞品信源" : loadHistory ? "当天数据已显示；需要时再加载最近 14 个自然日趋势，不阻塞模型切换" : "综合比较先展示所选日期；选择单个模型可查看最近 14 个自然日趋势"}</small></div>{loadHistory && !historicalModel && <button type="button" className="trend-more" disabled={historyLoading} onClick={() => { setHistoryLoading(true); setHistoryRequested(true); }}>{historyLoading ? "趋势加载中…" : "加载14日趋势"}</button>}</div>
    <div className="trend-selectors brand-only"><label>选择品牌（含竞品）<select value={activeBrand} onChange={(event) => setBrand(event.target.value)}>{brandOptions.map((name) => <option key={name}>{name}</option>)}</select></label></div>
    {activeBrand ? <div className="interactive-chart-card"><h4>{activeBrand} · 回答与信源变化</h4><RateLineChart dates={allDates} selectedDate={activeDate} onSelectDate={setSelectedDate} series={[{ label: "回答提及率", color: "#17a77b", values: brandAnswer }, { label: "信源提及率", color: "#f59e42", values: brandSources }]} /></div> : <Empty>暂无品牌趋势</Empty>}
    {!!productSeries.length && <div className="interactive-chart-card"><h4>{activeBrand} · 各产品回答提及率</h4><RateLineChart dates={allDates} series={productSeries} /></div>}
    <div className="competitor-links"><div className="competitor-links-head"><div><b>{activeBrand || "竞品"}信源链接</b><small>{activeDate || "请选择折线日期"} · {selectedSourceEligible ? `${selectedSourceMentions}/${selectedSourceEligible} 个已分析唯一信源提及（${selectedSourceRate.toFixed(1)}%） · 共观测 ${selectedSourceObserved} 个${selectedSourcePending ? `，${selectedSourcePending} 个待正文分析` : ""}${selectedSourceFailed ? `，${selectedSourceFailed} 个抓取失败` : ""}${selectedLinks ? ` · ${selectedLinks.links.length} 条可查看链接` : ""}` : selectedSourceObserved ? `共观测 ${selectedSourceObserved} 个${selectedSourcePending ? `，${selectedSourcePending} 个待正文分析` : ""}${selectedSourceFailed ? `，${selectedSourceFailed} 个抓取失败` : ""}` : "该日无信源"}</small></div>{linkLoading && <em>读取中…</em>}</div>
      {!!linkDays.length && <div className="link-date-tabs" aria-label="选择竞品信源日期">{linkDays.map((day) => <button key={day.date} type="button" className={activeDate === day.date ? "active" : ""} onClick={() => setSelectedDate(day.date)}><span>{day.date.slice(5)}</span><b>{day.mentions}</b></button>)}</div>}
      {selectedLinks?.links?.length ? <ol>{selectedLinks.links.map((link, index) => <li key={link.canonical_url || link.url}><span>{index + 1}</span><a href={link.url} target="_blank" rel="noreferrer"><b>{link.title || link.url}</b><small>{link.media} · {link.type} · 命中{link.match_scope || "已识别"}</small></a></li>)}</ol> : <Empty>所选日期没有该品牌信源</Empty>}
    </div>
  </section>;
}

function KeywordDailyTrend({ title, days, field }: { title: string; days: SourceBrandDay[]; field: "article_keywords" | "video_keywords" }) {
  const rows = days.slice(-7).reverse().flatMap((day) => day[field].slice(0, 10).map((item, index) => ({ ...item, date: day.date, rank: index + 1 })));
  return <article className="trend-block"><h3>{title}</h3><small>标题本地分词；显示每日 Top 10，便于观察主题升降</small>
    {rows.length ? <div className="trend-table keyword-trend"><table><thead><tr><th>日期</th><th>关键词</th><th>标题数</th><th>当日名次</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.date}-${row.term}`}><td>{row.date}</td><td><b>{row.term}</b></td><td>{row.count}</td><td>#{row.rank}</td></tr>)}</tbody></table></div> : <Empty>当前范围标题样本不足</Empty>}
  </article>;
}

export function Dashboard() {
  const [view, setView] = useState<View>("overview");
  const [model, setModel] = useState("");
  const [question, setQuestion] = useState("");
  // The server and browser must render the same first frame. Browser-only
  // preferences are restored after hydration to avoid a React mismatch.
  const [date, setDate] = useState(() => new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date()));
  const filtersReady = true;
  const [intersectionTarget, setIntersectionTarget] = useState("owned");
  const [loadedAnalytics, setAnalytics] = useState<Analytics>(emptyAnalytics);
  const [sourceIntersections, setSourceIntersections] = useState<Analytics | null>(null);
  const [sourceIntersectionLoading, setSourceIntersectionLoading] = useState(false);
  const [control, setControl] = useState<Record<string, ControlState>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [accounts, setAccounts] = useState<Record<string, AccountState>>({});
  const [startingModels, setStartingModels] = useState<Record<string, boolean>>({});
  const [rounds, setRounds] = useState<Record<string, number>>({});
  const [questionModes, setQuestionModes] = useState<
    Record<string, QuestionMode>
  >({});
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [lastRefresh, setLastRefresh] = useState("");
  const draftsInitialized = useRef(false);
  const analyticsCache = useRef(new Map<string, Analytics>());
  const analyticsRequest = useRef<AbortController | null>(null);
  const analyticsRequestSequence = useRef(0);
  const analyticsResponseEtag = useRef("");
  const sourceIntersectionRequest = useRef<AbortController | null>(null);

  // A filter label must never be paired with the previous filter's rows. Derive
  // an empty result immediately from the selected filter while retaining the
  // selector catalogs. This avoids both stale rows and a second effect render.
  const analytics = useMemo<Analytics>(() => {
    const filters = loadedAnalytics.filters;
    if (
      (filters?.model || "") === model &&
      (filters?.question || "") === question &&
      (filters?.date || "") === date &&
      (filters?.view || "") === view
    ) return loadedAnalytics;
    return {
      ...emptyAnalytics,
      filters: { model, question, date, view },
      model_catalog: loadedAnalytics.model_catalog,
      questions: loadedAnalytics.questions,
      dates: loadedAnalytics.dates,
      owned_brands: loadedAnalytics.owned_brands,
    };
  }, [loadedAnalytics, model, question, date, view]);

  useEffect(() => {
    // A persisted date made a newly opened dashboard silently show an old
    // natural day (for example 08-19 on 08-22). Always start on Beijing today;
    // users can still switch to historical dates during the current session.
    window.localStorage.setItem("monitorSelectedDate", date);
  }, [date]);

  const loadAnalytics = useCallback(async (force = false, silent = false) => {
    if (!filtersReady) return;
    if (view === "control") {
      setLoading(false);
      return;
    }
    // Let an active refresh finish. Aborting the browser request does not stop
    // its server-side aggregation and used to leave many expensive jobs alive.
    if (silent && analyticsRequest.current) return;
    const includeRuns = view === "runs";
    const cacheKey = JSON.stringify([model, question, date, view]);
    const cached = analyticsCache.current.get(cacheKey);
    if (cached && !force) {
      setAnalytics(cached);
      setLoading(false);
      return;
    }
    if (!silent) {
      setLoading(true);
    }
    analyticsRequest.current?.abort();
    const request = new AbortController();
    const requestSequence = ++analyticsRequestSequence.current;
    analyticsRequest.current = request;
    try {
      const base = apiBase();
      const params = new URLSearchParams();
      if (model) params.set("model", model);
      if (question) params.set("question", question);
      if (date) params.set("date", date);
      params.set("view", view);
      if (includeRuns) params.set("include_runs", "1");
      const response = await fetch(`${base}/api/analytics?${params}`, {
        cache: "no-cache",
        signal: request.signal,
      });
      if (!response.ok) throw new Error("本地分析服务暂不可用");
      const responseEtag = response.headers.get("ETag") || "";
      if (silent && responseEtag && responseEtag === analyticsResponseEtag.current) {
        setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
        return;
      }
      const analyticsPayload = await response.json();
      if (
        analyticsRequest.current !== request ||
        analyticsRequestSequence.current !== requestSequence
      ) return;
      analyticsResponseEtag.current = responseEtag;
      analyticsCache.current.set(cacheKey, analyticsPayload);
      while (analyticsCache.current.size > 40) {
        const oldestKey = analyticsCache.current.keys().next().value;
        if (oldestKey === undefined) break;
        analyticsCache.current.delete(oldestKey);
      }
      setAnalytics(analyticsPayload);
      // Never rewrite a filter as a side effect of an analytics response. When
      // users change question and date in quick succession, a response started
      // for the earlier combination can otherwise reset the newer selection to
      // "全部日期/全部问题". The current request key already guards the rows;
      // selectors must remain controlled by the user's last action.
      setError("");
      setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      if (!draftsInitialized.current && analyticsPayload.model_catalog?.length) {
        draftsInitialized.current = true;
        const questionRows = await Promise.all(
          analyticsPayload.model_catalog.map(async (item: CatalogModel) => {
            const response = await fetch(
              `${base}/api/models/${item.id}/questions`,
              { cache: "no-store" },
            );
            const payload = response.ok
              ? await response.json()
              : { questions: [], question_mode: "interleaved" };
            return {
              id: item.id,
              questions: (payload.questions || []).join("\n"),
              questionMode:
                payload.question_mode === "sequential"
                  ? "sequential"
                  : "interleaved",
            };
          }),
        );
        setDrafts(
          Object.fromEntries(
            questionRows.map((row) => [row.id, row.questions]),
          ),
        );
        setQuestionModes(
          Object.fromEntries(
            questionRows.map((row) => [row.id, row.questionMode]),
          ),
        );
        setRounds(
          Object.fromEntries(
            analyticsPayload.model_catalog.map((item: CatalogModel) => [
              item.id,
              10,
            ]),
          ),
        );
      }
    } catch (reason) {
      if (analyticsRequestSequence.current !== requestSequence) return;
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      setError(reason instanceof Error ? reason.message : "连接失败");
    } finally {
      if (analyticsRequest.current === request) {
        analyticsRequest.current = null;
        setLoading(false);
      }
    }
  }, [model, question, date, view, filtersReady]);

  useEffect(() => {
    if (!filtersReady) return;
    // Keep only a very short coalescing window. Server-side filter caches make
    // the common switches cheap, so a long fixed debounce only adds visible lag.
    const initial = window.setTimeout(() => void loadAnalytics(), 80);
    const refreshLiveData = () => {
      if (!document.hidden && view !== "control") void loadAnalytics(true, true);
    };
    // Collector callbacks are committed continuously. Keep the visible
    // dashboard close to that stream instead of leaving production counters
    // stale for up to a minute. Returning to the tab also refreshes at once.
    const timer = window.setInterval(refreshLiveData, 15000);
    window.addEventListener("focus", refreshLiveData);
    document.addEventListener("visibilitychange", refreshLiveData);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshLiveData);
      document.removeEventListener("visibilitychange", refreshLiveData);
      analyticsRequestSequence.current += 1;
      analyticsRequest.current?.abort();
    };
  }, [loadAnalytics, view, filtersReady]);

  useEffect(() => {
    sourceIntersectionRequest.current?.abort();
    if (view !== "sources" || date) {
      return;
    }
    const request = new AbortController();
    sourceIntersectionRequest.current = request;
    const timer = window.setTimeout(async () => {
      setSourceIntersectionLoading(true);
      try {
        const params = new URLSearchParams();
        if (question) params.set("question", question);
        const response = await fetch(
          `${apiBase()}/api/analytics/source-intersections?${params}`,
          { cache: "no-cache", signal: request.signal },
        );
        if (!response.ok) throw new Error("模型交集读取失败");
        const payload = await response.json();
        if (sourceIntersectionRequest.current === request) {
          setSourceIntersections(payload);
        }
      } catch (reason) {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(reason instanceof Error ? reason.message : "模型交集读取失败");
        }
      } finally {
        if (sourceIntersectionRequest.current === request) {
          sourceIntersectionRequest.current = null;
          setSourceIntersectionLoading(false);
        }
      }
    }, 30);
    return () => {
      window.clearTimeout(timer);
      request.abort();
    };
  }, [view, question, date]);

  const loadControl = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase()}/api/control/status?_=${Date.now()}`, {
        cache: "no-store",
      });
      if (response.ok) {
        const payload = await response.json();
        setControl(payload.models || {});
      }
    } catch (reason) {
      void reason;
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void loadControl(), 0);
    const timer = window.setInterval(() => {
      if (!document.hidden) void loadControl();
    }, 5000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [loadControl]);

  const visibleModelCatalog = analytics.model_catalog;
  const selectedModels = analytics.models.filter(
    (item) => !model || item.id === model,
  );
  const participatingModelCount = selectedModels.filter(
    (item) => item.runs > 0 || item.sources > 0,
  ).length;
  const sourceIntersectionsMatch = Boolean(
    sourceIntersections
    && (sourceIntersections.filters?.question || "") === question
    && (sourceIntersections.filters?.date || "") === date
  );
  const intersectionAnalytics = date
    ? analytics
    : sourceIntersectionsMatch
      ? sourceIntersections
      : null;
  const sourceIntersectionPending = view === "sources"
    && !date
    && (!sourceIntersectionsMatch || sourceIntersectionLoading);
  const activeIntersectionTarget = ["owned", "competitors"].includes(intersectionTarget)
    || (intersectionAnalytics?.competitor_brands || []).includes(intersectionTarget)
    ? intersectionTarget
    : "owned";
  const allCompetitors = activeIntersectionTarget === "competitors";
  const selectedCompetitor = ["owned", "competitors"].includes(activeIntersectionTarget) ? "" : activeIntersectionTarget;
  const competitorMode = allCompetitors || !!selectedCompetitor;
  const threeIntersectionItems = competitorMode
    ? allCompetitors
      ? intersectionAnalytics?.common_all_competitor_sources || []
      : (intersectionAnalytics?.common_competitor_sources || []).filter((item) => item.competitor_brand === selectedCompetitor)
    : intersectionAnalytics?.common_owned_sources || [];
  const twoIntersectionItems = competitorMode
    ? allCompetitors
      ? intersectionAnalytics?.two_model_all_competitor_sources || []
      : (intersectionAnalytics?.two_model_competitor_sources || []).filter((item) => item.competitor_brand === selectedCompetitor)
    : intersectionAnalytics?.two_model_owned_sources || [];
  const intersectionTargetLabel = allCompetitors ? "全部非自有竞品" : selectedCompetitor || "自有产品";
  const totals = useMemo(
    () =>
      selectedModels.reduce(
        (sum, item) => ({
          runs: sum.runs + item.runs,
          sources: sum.sources + item.sources,
          unique: sum.unique + item.unique_sources,
          devices: sum.devices + item.device_count,
        }),
        { runs: 0, sources: 0, unique: 0, devices: 0 },
      ),
    [selectedModels],
  );

  async function saveQuestions(modelId: string) {
    const questions = (drafts[modelId] || "")
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
    const response = await fetch(
      `${apiBase()}/api/models/${modelId}/questions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          questions,
          question_mode: questionModes[modelId] || "interleaved",
        }),
      },
    );
    const payload = await response.json();
    if (!response.ok || !payload.ok)
      throw new Error(payload.error || "保存失败");
  }
  async function accountCheck(modelId: string) {
    setMessage("正在检查网页会话；未登录时会打开对应登录页…");
    try {
      const response = await fetch(
        `${apiBase()}/api/models/${modelId}/account-check`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        },
      );
      const payload = await response.json();
      if (!response.ok || !payload.ok)
        throw new Error(payload.error || "校验失败");
      setAccounts((old) => ({ ...old, [modelId]: payload.account }));
      setMessage(payload.account.message);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "校验失败");
    }
  }
  async function controlAction(modelId: string, action: "start" | "stop") {
    if (action === "start") {
      setStartingModels((old) => ({ ...old, [modelId]: true }));
      setControl((old) => ({ ...old, [modelId]: { running: false, ready: true, ...old[modelId], starting: true, phase: "正在保存问题并提交启动请求" } }));
    }
    setMessage(action === "start" ? "启动请求已接收，正在准备浏览器与账号校验…" : "正在停止任务…");
    try {
      if (action === "start") await saveQuestions(modelId);
      const response = await fetch(
        `${apiBase()}/api/control/${modelId}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            rounds: rounds[modelId] || 10,
            question_mode: questionModes[modelId] || "interleaved",
          }),
        },
      );
      const payload = await response.json();
      if (!response.ok || !payload.ok)
        throw new Error(payload.error || "操作失败");
      setMessage(
        `${analytics.model_catalog.find((item) => item.id === modelId)?.name || modelId}${action === "start" ? "已启动" : "已停止"}`,
      );
      await loadControl();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      if (action === "start") setStartingModels((old) => ({ ...old, [modelId]: false }));
    }
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div>MI</div>
          <span>
            <b>GEO大模型监测</b>
            <small>
              <i className={error ? "off" : ""} />
              {error || "数据服务在线"}
            </small>
          </span>
        </div>
        <div className="scope">
          <span>模型范围</span>
          <button
            className={!model ? "active" : ""}
            onClick={() => setModel("")}
          >
            <svg className="model-icon all-models" width={29} height={29} viewBox="0 0 29 29" fill="none">
              <rect width="29" height="29" rx="8" fill="url(#all-grad)" />
              <circle cx="10" cy="10" r="3" fill="white" opacity="0.9" />
              <circle cx="19" cy="10" r="3" fill="white" opacity="0.9" />
              <circle cx="10" cy="19" r="3" fill="white" opacity="0.9" />
              <circle cx="19" cy="19" r="3" fill="white" opacity="0.9" />
              <defs>
                <linearGradient id="all-grad" x1="0" y1="0" x2="29" y2="29" gradientUnits="userSpaceOnUse">
                  <stop stopColor="#34C298" />
                  <stop offset="1" stopColor="#128062" />
                </linearGradient>
              </defs>
            </svg>
            <b>全部模型</b>
            <small>综合比较</small>
          </button>
          {visibleModelCatalog.map((item) => {
            const Icon = getIcon(item.tone);
            return (
              <button
                className={model === item.id ? "active" : ""}
                key={item.id}
                onClick={() => setModel(item.id)}
              >
                {Icon ? <Icon className={`model-icon ${item.tone}`} size={29} /> : <i className={item.tone}>{item.short_name}</i>}
                <b>{item.name}</b>
                <small>{control[item.id]?.running ? "网页采集中" : "已停止"}</small>
              </button>
            );
          })}
        </div>
        <nav>
          {views.map((item) => (
            <button
              key={item.id}
              className={view === item.id ? "active" : ""}
              onClick={() => setView(item.id)}
            >
              <i>{item.symbol}</i>
              <span>
                <b>{item.label}</b>
                <small>{item.hint}</small>
              </span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span>分析 15 秒 · 状态 5 秒</span>
          <b>{lastRefresh || "等待首次读取"}</b>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <span>
              MODEL INTELLIGENCE /{" "}
              {views.find((item) => item.id === view)?.label}
            </span>
            <h1>
              {view === "compare" && question
                ? `“${question}”跨模型表现`
                : views.find((item) => item.id === view)?.label}
            </h1>
            <p>所有统计按北京时间自然日归档；同一轮同一链接仅计一次。</p>
          </div>
          {view !== "control" && (
            <button onClick={() => loadAnalytics(true)} disabled={loading}>
              {loading ? "读取中" : "刷新数据"}
            </button>
          )}
        </header>
        {view !== "control" && (
          <div className="filterbar">
            <label>
              问题
              <select
                data-testid="question-filter"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
              >
                <option value="">全部问题</option>
                {analytics.questions.map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </label>
            <label>
              自然日
              <select
                data-testid="date-filter"
                value={date}
                onChange={(event) => {
                  setDate(event.target.value);
                  window.localStorage.setItem("monitorSelectedDate", event.target.value);
                }}
              >
                <option value="">全部日期</option>
                {analytics.dates.map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </label>
            <div>
              <span>当前口径</span>
              <b>
                {model
                  ? analytics.model_catalog.find((item) => item.id === model)
                      ?.name
                  : "全部模型"}{" "}
                · {question || "全部问题"} · {date || "全部日期"}
              </b>
            </div>
          </div>
        )}
        {view !== "control" && loading && (
          <div className="filter-loading" role="status">
            正在切换数据口径，请稍候…
          </div>
        )}

        <div className="content">
          {view !== "control" && (
            <section className="kpis">
              <article>
                <span>有效回答</span>
                <b>{fmt(totals.runs)}</b>
                <small>当前筛选范围内完成轮次</small>
              </article>
              <article>
                <span>信源引用</span>
                <b>{fmt(totals.sources)}</b>
                <small>去重后 {fmt(totals.unique)} 条链接</small>
              </article>
              <article>
                <span>平均信源 / 回答</span>
                <b>
                  {totals.runs
                    ? (totals.sources / totals.runs).toFixed(1)
                    : "0"}
                </b>
                <small>衡量回答证据密度</small>
              </article>
              <article>
                <span>有数据模型 / 设备</span>
                <b>
                  {participatingModelCount} / {totals.devices}
                </b>
                <small>当前筛选范围实际有数据的网页会话</small>
              </article>
            </section>
          )}

          {view === "overview" && (
            <>
              <DailyOwnedProductBoard rows={analytics.owned_product_daily || []} models={selectedModels} />
              <section className="panel">
                <SectionTitle
                  eyebrow="MODEL HEALTH"
                  title="模型表现一眼看清"
                  note="点击左侧模型可进入单模型视角"
                />
                <div className="model-grid">
                  {selectedModels.map((item) => {
                    const Icon = getIcon(item.tone);
                    return (
                    <article
                      className={`model-card ${item.tone}`}
                      key={item.id}
                    >
                      <header>
                        {Icon ? <Icon className={`model-icon ${item.tone}`} size={38} /> : <i>{item.short_name}</i>}
                        <div>
                          <b>{item.name}</b>
                          <small>{control[item.id]?.running ? "● 正在网页采集" : "配置就绪"}</small>
                        </div>
                      </header>
                      <strong>
                        {fmt(item.runs)}
                        <small>轮有效回答</small>
                      </strong>
                      <dl>
                        <div>
                          <dt>信源引用</dt>
                          <dd>{fmt(item.sources)}</dd>
                        </div>
                        <div>
                          <dt>唯一链接</dt>
                          <dd>{fmt(item.unique_sources)}</dd>
                        </div>
                        <div>
                          <dt>平均信源</dt>
                          <dd>
                            {item.runs
                              ? (item.sources / item.runs).toFixed(1)
                              : "0"}
                          </dd>
                        </div>
                      </dl>
                    </article>
                  );
                  })}
                </div>
              </section>
              {!!analytics.doubao_owned_video_category_share?.rows?.length && (
                <OwnedVideoCategoryShare data={analytics.doubao_owned_video_category_share} />
              )}
              <section className="two-col">
                <article className="panel">
                  <SectionTitle title="按日采集脉搏" note="最近 7 个自然日" />
                  <div className="daily-list">
                    {selectedModels
                      .flatMap((item) =>
                        item.daily
                          .slice(0, 7)
                          .map((row) => ({
                            ...row,
                            model: item.name,
                            tone: item.tone,
                          })),
                      )
                      .sort((a, b) => b.date.localeCompare(a.date))
                      .slice(0, 14)
                      .map((row, index) => (
                        <div key={`${row.model}-${row.date}-${index}`}>
                          <i className={row.tone} />
                          <b>{row.date}</b>
                          <span>{row.model}</span>
                          <strong>
                            {row.runs} 轮 · {row.sources} 信源
                          </strong>
                        </div>
                      ))}
                  </div>
                </article>
                <article className="panel">
                  <SectionTitle
                    title="证据结构"
                    note="文章、视频、社交与电商"
                  />
                  {selectedModels.map((item) => (
                    <div className="model-bars" key={item.id}>
                      <b>{item.name}</b>
                      <Bars items={item.source_types} total={item.sources} />
                    </div>
                  ))}
                </article>
              </section>
            </>
          )}

          {view === "compare" && (
            <>
              {!question ? (
                <section className="panel compare-prompt">
                  <b>先选择一个问题</b>
                  <p>
                    选择问题后，这里会用完全相同的口径横向比较每个模型的回答轮次、信源数量、唯一链接和证据密度。
                  </p>
                  <div>
                    {analytics.questions.map((item) => (
                      <button key={item} onClick={() => setQuestion(item)}>
                        {item}
                      </button>
                    ))}
                  </div>
                </section>
              ) : (
                <>
                  <section className="panel">
                    <SectionTitle
                      eyebrow="QUESTION BENCHMARK"
                      title="同一问题，各模型证据表现"
                      note={date || "全部日期"}
                    />
                    <div className="compare-table">
                      <table>
                        <thead>
                          <tr>
                            <th>模型</th>
                            <th>回答轮次</th>
                            <th>信源引用</th>
                            <th>唯一链接</th>
                            <th>平均信源</th>
                            <th>相对证据量</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedModels.map((item) => {
                            const Icon = getIcon(item.tone);
                            const max = Math.max(
                              1,
                              ...selectedModels.map((row) => row.sources),
                            );
                            return (
                              <tr key={item.id}>
                                <td>
                                  <span className={`model-pill ${item.tone}`}>
                                    {Icon ? <Icon className="model-icon" size={28} /> : item.short_name}
                                  </span>
                                  <b>{item.name}</b>
                                </td>
                                <td>{item.runs}</td>
                                <td>
                                  <strong>{item.sources}</strong>
                                </td>
                                <td>{item.unique_sources}</td>
                                <td>
                                  {item.runs
                                    ? (item.sources / item.runs).toFixed(2)
                                    : "0"}
                                </td>
                                <td>
                                  <i className="evidence">
                                    <em
                                      style={{
                                        width: `${(item.sources * 100) / max}%`,
                                      }}
                                    />
                                  </i>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </section>
                  <section className="model-source-grid">
                    {selectedModels.map((item) => (
                      <article className="panel" key={item.id}>
                        <SectionTitle
                          eyebrow={item.name.toUpperCase()}
                          title="信源构成"
                          note={`${item.sources} 次引用`}
                        />
                        <Bars items={item.source_types} total={item.sources} />
                        <div className="media-chips">
                          {item.media.slice(0, 8).map((row) => (
                            <span key={row.name}>
                              {row.name}
                              <b>{row.count}</b>
                            </span>
                          ))}
                        </div>
                        <div className="compare-source-tops">
                          <SourceTop title="文章链接 Top 25" items={item.top_articles} tone="article" />
                          <SourceTop title="视频链接 Top 25" items={item.top_videos} tone="video" />
                        </div>
                      </article>
                    ))}
                  </section>
                </>
              )}
            </>
          )}

          {view === "sources" && (
            <>
              {!date && analytics.detail_scope?.kind === "latest_day" && (
                <section className="analysis-note panel source-scope-note">
                  <b>全部日期汇总已加载</b>
                  <span>
                    上方回答与信源 KPI、模型交集均按当前全部日期完整统计；各模型 Top 25 与关键词先展示最新有效自然日
                    {analytics.detail_scope.date_from && analytics.detail_scope.date_to
                      ? analytics.detail_scope.date_from === analytics.detail_scope.date_to
                        ? `（${analytics.detail_scope.date_to}）`
                        : `（${analytics.detail_scope.date_from} 至 ${analytics.detail_scope.date_to}）`
                      : ""}，需要核对更早明细时可直接选择对应日期。
                  </span>
                </section>
              )}
              <section className="panel intersection-target-panel">
                <div>
                  <span>链接交集对象</span>
                  <h2>查看自有产品或竞品的跨模型共同信源</h2>
                  <p>{sourceIntersectionPending
                    ? "正在读取当前问题下全部日期的模型交集…"
                    : "模型交集严格服从当前问题和日期口径；竞品只使用结构化产品及高/中质量文章正文，不使用标题推测。"}</p>
                </div>
                <label>
                  <b>选择对象</b>
                  <select aria-label="选择对象" disabled={sourceIntersectionPending} value={activeIntersectionTarget} onChange={(event) => setIntersectionTarget(event.target.value)}>
                    <option value="owned">自有产品</option>
                    <option value="competitors">全部非自有竞品</option>
                    {(intersectionAnalytics?.competitor_brands || []).length > 0 && (
                      <optgroup label="竞品（文章正文命中）">
                        {(intersectionAnalytics?.competitor_brands || []).map((brandName) => (
                          <option value={brandName} key={brandName}>{brandName}</option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                </label>
              </section>
              {sourceIntersectionPending ? (
                <section className="panel filter-loading-panel" role="status">
                  正在汇总全部日期的跨模型共同信源…
                </section>
              ) : (
                <>
                  <CommonSourceIntersections
                    items={threeIntersectionItems}
                    mode="all"
                    targetKind={competitorMode ? "competitor" : "owned"}
                    targetLabel={intersectionTargetLabel}
                  />
                  <CommonSourceIntersections
                    items={twoIntersectionItems}
                    mode="two"
                    targetKind={competitorMode ? "competitor" : "owned"}
                    targetLabel={intersectionTargetLabel}
                  />
                </>
              )}
              {selectedModels.map((item) => (
                <section className="panel source-section" key={item.id}>
                  <SectionTitle
                    eyebrow={item.name.toUpperCase()}
                    title={`${question || "全部问题"} · 信源策略`}
                    note={date || (analytics.detail_scope?.date_from && analytics.detail_scope?.date_to
                      ? `${analytics.detail_scope.date_to} · 最新有效日明细`
                      : "最新有效日明细")}
                  />
                  {(date ? [{ date, top_articles: item.top_articles, top_videos: item.top_videos }] : item.daily_source_top.slice(0, 7)).map((day) => (
                    <div className="daily-source-group" key={`${item.id}-${day.date}`}>
                      <h3>{day.date} · {item.name}</h3>
                      <div className="source-pair">
                        <SourceTop title="高频文章 Top 25" items={day.top_articles} tone="article" />
                        <SourceTop title="高频视频 Top 25" items={day.top_videos} tone="video" />
                      </div>
                    </div>
                  ))}
                  <div className="all-owned-sources">
                    <SourceTop
                      title={`全部自有品牌信源（${(item.owned_sources || []).length}）`}
                      items={item.owned_sources || []}
                      tone="article"
                    />
                  </div>
                  <div className="keyword-pair">
                    <Keywords
                      title="文章文案关键词"
                      items={item.article_keywords}
                      tone="article"
                    />
                    <Keywords
                      title="视频文案关键词"
                      items={item.video_keywords}
                      tone="video"
                    />
                  </div>
                </section>
              ))}
            </>
          )}

          {view === "brands" && (
            <>
              <section className="analysis-note panel">
                <b>准确性与成本口径</b>
                <span>正文优先使用采集端结构化品牌/产品结果；未覆盖项只做跨模型词表精确命中。视频仅检查标题，文章检查标题与已归档正文。本页统计不调用大模型，日常 Token 消耗为 0。</span>
              </section>
              {selectedModels.map((item) => (
                <section className="panel brand-section" key={item.id}>
                  <SectionTitle eyebrow={item.name.toUpperCase()} title={`${question || "全部问题"} · 品牌与产品每日表现`} note={date || (model ? "最近 2 日首屏 · 可按需加载 14 日趋势" : "最近 2 个有效自然日")} />
                  {!!item.analysis_pending_runs && (
                    <div className="analysis-note pending-note">
                      <b>产品解析进度</b>
                      <span>{item.analysis_pending_runs} 轮产品复核尚未完成；品牌榜已按回答正文实时统计，产品榜只以已解析轮次为分母，待解析轮次不会误算为未提及。</span>
                    </div>
                  )}
                  <div className="brand-kpis">
                    <span><b>{item.branded_source_count}/{item.branded_source_eligible_count ?? item.sources}</b> 已分析信源含品牌</span>
                    <span><b>{item.owned_source_count}/{item.owned_source_eligible_count ?? item.sources}</b> 已分析信源属自有品牌</span>
                    <span><b>{(item.owned_source_eligible_count ?? item.sources) ? ((item.owned_source_count * 100) / (item.owned_source_eligible_count ?? item.sources)).toFixed(1) : "0"}%</b> 已分析自有信源率</span>
                    {!!item.source_body_pending_count && <span><b>{item.source_body_pending_count}</b> 条待正文分析</span>}
                    {!!item.source_body_failed_count && <span><b>{item.source_body_failed_count}</b> 条正文抓取失败</span>}
                  </div>
                  <BrandProductTrendExplorer key={`${item.id}-${question}-${date}`} model={item} question={question} focusDate={date || item.source_brand_daily.at(-1)?.date || ""} ownedBrands={analytics.owned_brands || []} loadHistory={Boolean(model)} />
                  <div className="trend-grid">
                    <MentionTrend title="正文品牌提及率与每日名次" days={item.brand_trend_daily} kind="brand" />
                    <MentionTrend title="正文产品提及率与每日名次" days={item.product_trend_daily} kind="product" />
                  </div>
                  <SourceBrandTrend days={item.source_brand_daily} />
                  <div className="trend-grid">
                    <KeywordDailyTrend title="文章标题关键词每日变化" days={item.source_brand_daily} field="article_keywords" />
                    <KeywordDailyTrend title="视频标题关键词每日变化" days={item.source_brand_daily} field="video_keywords" />
                  </div>
                  <div className="keyword-pair">
                    <Keywords title="文章标题关键词（当前范围）" items={item.article_keywords} tone="article" />
                    <Keywords title="视频标题关键词（当前范围）" items={item.video_keywords} tone="video" />
                  </div>
                </section>
              ))}
            </>
          )}

          {view === "runs" && (
            <section className="panel">
              <SectionTitle
                eyebrow="ANSWER AUDIT"
                title="逐轮回答与信源审计"
                note="按完成时间倒序"
              />
              <div className="run-list">
                {selectedModels
                  .flatMap((item) =>
                    (item.recent_runs || []).map((run) => ({ ...run, model: item })),
                  )
                  .sort((a, b) => b.finished_at.localeCompare(a.finished_at))
                  .map((run) => (
                    <article key={`${run.model.id}-${run.run_id}`}>
                      <header>
                        <span className={`model-pill ${run.model.tone}`}>
                          {(() => { const Icon = getIcon(run.model.tone); return Icon ? <Icon className="model-icon" size={28} /> : run.model.short_name; })()}
                        </span>
                        <b>
                          {run.model.name} · 第 {run.sequence} 轮
                        </b>
                        <time>{timeText(run.finished_at)}</time>
                      </header>
                      <h3>{run.question}</h3>
                      <p>
                        {run.sources.length} 条信源 · {run.serial}
                      </p>
                      <details>
                        <summary>查看回答正文与信源</summary>
                        <div className="answer-text">
                          {run.answer || "未保存回答正文"}
                        </div>
                        <ul>
                          {run.sources.map((source) => (
                            <li key={source.canonical_url}>
                              <a
                                href={source.url}
                                target="_blank"
                                rel="noreferrer"
                              >
                                {source.title || source.url}
                              </a>
                              <small>
                                {source.media} · {source.type}
                              </small>
                              {source.own_brand && <em className="owned-source">自有品牌 · {source.owned_brands?.join("、") || source.own_products?.join("、")} · {source.brand_match_scope}</em>}
                            </li>
                          ))}
                        </ul>
                      </details>
                    </article>
                  ))}
              </div>
            </section>
          )}

          {view === "control" && (
            <>
              <section className="control-hero">
                <div>
                  <span>COLLECTION WORKSPACE</span>
                  <h2>每个模型独立配置，互不影响</h2>
                  <p>
                    五个模型均由独立浏览器插件直接在网页提问、等待回答并采集引用；不再依赖模拟器、ADB 或 Appium。
                  </p>
                </div>
                <button type="button" onClick={() => { window.location.href = `${apiBase()}/tasks`; }}>
                  打开远程任务中心
                </button>
                {message && <b>{message}</b>}
              </section>
              <section className="control-grid">
                {visibleModelCatalog.map((item) => {
                  const state = control[item.id];
                  const account = accounts[item.id];
                  const starting = Boolean(state?.starting || startingModels[item.id]);
                  return (
                    <article
                      className={`panel control-card ${state?.running ? "running" : ""} ${starting ? "starting" : ""}`}
                      data-model-id={item.id}
                      key={item.id}
                    >
                      <header>
                        {(() => { const Icon = getIcon(item.tone); return Icon ? <Icon className={`model-icon ${item.tone}`} size={42} /> : <i className={item.tone}>{item.short_name}</i>; })()}
                        <div>
                          <h2>{item.name}</h2>
                          <span>
                            {starting
                                ? `● ${state?.phase || "正在启动"}`
                                : state?.running
                                ? `● 运行中 · PID ${state.pid}`
                                : state?.ready
                                  ? "● 配置就绪"
                                  : "● 缺少配置"}
                          </span>
                        </div>
                      </header>
                      {(starting || state?.startup_error) && (
                        <div className={`startup-feedback ${state?.startup_error ? "failed" : ""}`}>
                          <span className={starting ? "startup-spinner" : ""} />
                          <div>
                            <b>{state?.startup_error || state?.phase || "正在启动"}</b>
                            <small>{state?.startup_error ? "请修正后重新点击启动" : "Chrome 启动、账号校验完成后会自动运行采集脚本"}</small>
                          </div>
                        </div>
                      )}
                      {state?.running && state?.phase && (
                        <div className="runtime-feedback">
                          <span className="runtime-dot" />
                          <div>
                            <b>{state.phase}</b>
                            <small>状态每 2 秒从采集日志同步</small>
                          </div>
                        </div>
                      )}
                      <label>
                        提问问题{" "}
                        <small>每行一个，启动前自动保存</small>
                        <textarea
                          value={drafts[item.id] || ""}
                          onChange={(event) =>
                            setDrafts((old) => ({
                              ...old,
                              [item.id]: event.target.value,
                            }))
                          }
                        />
                      </label>
                      <div className="control-row">
                        <label>
                          每题轮数
                          <input
                            type="number"
                            min="1"
                            max="10000"
                            value={rounds[item.id] || 10}
                            onChange={(event) =>
                              setRounds((old) => ({
                                ...old,
                                [item.id]: Math.max(
                                  1,
                                  Number(event.target.value) || 1,
                                ),
                              }))
                            }
                          />
                        </label>
                        <label>
                          执行顺序
                          <select
                            value={questionModes[item.id] || "interleaved"}
                            onChange={(event) =>
                              setQuestionModes((old) => ({
                                ...old,
                                [item.id]: event.target.value as QuestionMode,
                              }))
                            }
                          >
                            <option value="interleaved">
                              交叉提问（每题一轮）
                            </option>
                            <option value="sequential">
                              顺序提问（单题全部轮次）
                            </option>
                          </select>
                        </label>
                        <button
                          className="account-button"
                          onClick={() => accountCheck(item.id)}
                        >
                          检查网页登录
                        </button>
                      </div>
                      <div
                        className={`account-result ${account?.status || "idle"}`}
                      >
                        <b>
                          {account
                            ? account.message
                            : "首次运行请先检查网页登录状态"}
                        </b>
                        {account?.web && (
                          <small>
                            网页会话 {account.web?.masked || "已就绪"} · 本机独立浏览器配置
                          </small>
                        )}
                      </div>
                      <div className="job-meta">
                        <span>
                          浏览器模式
                          <b>无头网页直采</b>
                        </span>
                        <span>
                          启动时间
                          <b>{timeText(state?.started_at)}</b>
                        </span>
                      </div>
                      <div className="job-actions">
                        <button
                          onClick={() =>
                            saveQuestions(item.id)
                              .then(() => setMessage(`${item.name}问题已保存`))
                              .catch((reason) => setMessage(reason.message))
                          }
                        >
                          仅保存问题
                        </button>
                        <button
                          className="start"
                          disabled={starting || state?.running || !state?.ready}
                          onClick={() => controlAction(item.id, "start")}
                        >
                          {starting ? "正在启动…" : "启动采集"}
                        </button>
                        <button
                          className="stop"
                          disabled={!starting && !state?.running}
                          onClick={() => controlAction(item.id, "stop")}
                        >
                          停止
                        </button>
                      </div>
                      <small className="log">日志：{state?.log || "启动后生成"}</small>
                    </article>
                  );
                })}
              </section>
            </>
          )}
        </div>
      </section>
    </main>
  );
}
