"use client";

import { useCallback, useEffect, useMemo, useState, type CSSProperties, type FormEvent } from "react";
import { getIcon } from "./ModelIcon";
import { EnterpriseBrandLockup, EnterpriseBrandMark } from "./EnterpriseBrand";
import { PaidMonitorCenter } from "./PaidMonitorCenter";

type ReportItem = {
  id: string; report_key: string; brand_name: string; product_name: string; question: string;
  status: string; rounds: number; completed_steps: number; total_steps: number;
  created_at: string; finished_at?: string;
  retry_count?: number;
  high_probability_strategy_active: boolean;
  public_enabled: boolean; public_active: boolean; public_expired: boolean; public_expires_at: string;
  created_by_account_id?: string; created_by_username?: string;
  manual_edit_count?: number;
};

type AdminSession = {
  username: string; display_name: string; role: "super_admin" | "manager";
  expires_at?: string;
  quota?: { limited: boolean; limit: number; used: number; remaining: number };
};

type AdminAccount = {
  id: string; username: string; display_name: string; active: boolean; expired: boolean;
  can_login: boolean; expires_at: string; daily_limit: number; used_today: number;
  remaining_today: number; created_at: string; last_login_at?: string;
};

type AccountDraft = {
  username: string; display_name: string; password: string; active: boolean;
  expires_at: string; daily_limit: number;
};

type ReportEditorRecord = {
  request_id: string; model_id: string; round: number; created_at: string;
  record: Record<string, unknown>;
};

type ReportEditorData = {
  report_key: string; task_id: string; status: string;
  brand_name: string; product_name: string; question: string;
  high_probability_prior: boolean;
  probability_policy_version: number;
  current_probability_policy_version: number;
  records: ReportEditorRecord[];
  audits: { id: string; editor_username: string; created_at: string }[];
};

type EditorProduct = Record<string, unknown> & {
  brand?: string; brand_name?: string; name?: string; product_name?: string;
  recommended?: boolean; rank?: number | null;
};

type EditorSource = Record<string, unknown> & { title?: string; url?: string; href?: string };

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function arrayValue<T>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : [];
}

function modelDisplayName(modelId: string) {
  return LOGIN_MODELS.find((item) => item.id === modelId)?.name || modelId;
}

function ReportRecordForm({ item, defaultOpen, onChange }: { item: ReportEditorRecord; defaultOpen: boolean; onChange: (value: ReportEditorRecord) => void }) {
  const [expanded, setExpanded] = useState(defaultOpen);
  const record = item.record;
  const analysis = objectValue(record.analysis);
  const products = arrayValue<unknown>(record.products).map((value) => objectValue(value) as EditorProduct);
  const sources = arrayValue<unknown>(record.sources).map((value) => objectValue(value) as EditorSource);
  const brands = arrayValue<unknown>(record.brands).map(String);
  const body = String(record.web_body || record.reply || "");
  const recommended = typeof analysis.recommended === "boolean" ? analysis.recommended : Boolean(record.recommended);
  const rawRank = analysis.rank ?? record.rank;
  const rank = rawRank === null || rawRank === undefined || rawRank === "" ? "" : String(rawRank);
  const Icon = getIcon(item.model_id);

  function patchRecord(patch: Record<string, unknown>) {
    onChange({ ...item, record: { ...record, ...patch } });
  }

  function patchAnalysis(patch: Record<string, unknown>) {
    patchRecord({ analysis: { ...analysis, mode: String(analysis.mode || "local_chrome_extension"), ...patch } });
  }

  function replaceProduct(index: number, patch: EditorProduct) {
    patchRecord({ products: products.map((value, current) => current === index ? { ...value, ...patch } : value) });
  }

  function replaceSource(index: number, patch: EditorSource) {
    patchRecord({ sources: sources.map((value, current) => current === index ? { ...value, ...patch } : value) });
  }

  return <details className="admin-record-form" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
    <summary><span className="admin-record-model">{Icon && <Icon size={25} />}<b>{modelDisplayName(item.model_id)}</b><em>第 {item.round} 轮</em></span><span className="admin-record-summary-right"><span className="admin-record-result"><i className={recommended ? "positive" : "neutral"} />{recommended ? `已推荐${rank ? ` · 第 ${rank} 名` : ""}` : "未推荐"}</span><span className="admin-record-toggle"><b>{expanded ? "收起" : "展开编辑"}</b><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6.5 8 3.5 3.5L13.5 8" /></svg></span></span></summary>
    <div className="admin-record-body">
      <label className="admin-record-answer"><span>回答正文</span><textarea value={body} onChange={(event) => patchRecord({ web_body: event.target.value, ...(Object.prototype.hasOwnProperty.call(record, "reply") ? { reply: event.target.value } : {}) })} /></label>
      <div className="admin-record-decision">
        <label className="admin-record-check"><input type="checkbox" checked={recommended} onChange={(event) => { const next = event.target.checked; patchRecord({ recommended: next, rank: next ? (rawRank || null) : null, analysis: { ...analysis, mode: String(analysis.mode || "local_chrome_extension"), recommended: next, rank: next ? (rawRank || null) : null } }); }} /><span>该轮推荐了诊断品牌</span></label>
        <label><span>推荐排名</span><input type="number" min={1} max={100} disabled={!recommended} value={rank} placeholder="未识别" onChange={(event) => { const value = event.target.value ? Number(event.target.value) : null; patchRecord({ rank: value, analysis: { ...analysis, mode: String(analysis.mode || "local_chrome_extension"), recommended, rank: value } }); }} /></label>
        <label><span>识别摘要</span><input value={String(objectValue(analysis.details).summary || "")} placeholder="简要说明本轮识别结果" onChange={(event) => patchAnalysis({ details: { ...objectValue(analysis.details), summary: event.target.value } })} /></label>
      </div>

      <label className="admin-record-brands"><span>回答中涉及的品牌（每行一个）</span><textarea value={brands.join("\n")} onChange={(event) => patchRecord({ brands: event.target.value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean) })} placeholder="例如：\n目标品牌\n竞品品牌" /></label>

      <section className="admin-record-list"><header><div><b>产品与排名</b><span>用于计算目标品牌和竞品的推荐情况</span></div><button type="button" onClick={() => patchRecord({ products: [...products, { brand: "", name: "", recommended: false, rank: null }] })}>添加产品</button></header>
        {products.map((product, index) => <div className="admin-product-row" key={`product-${index}`}>
          <label><span>品牌</span><input value={String(product.brand || product.brand_name || "")} onChange={(event) => replaceProduct(index, { brand: event.target.value })} /></label>
          <label><span>产品名称</span><input value={String(product.name || product.product_name || "")} onChange={(event) => replaceProduct(index, { name: event.target.value })} /></label>
          <label className="compact-check"><input type="checkbox" checked={Boolean(product.recommended)} onChange={(event) => replaceProduct(index, { recommended: event.target.checked, rank: event.target.checked ? (product.rank || null) : null })} /><span>推荐</span></label>
          <label className="compact-rank"><span>排名</span><input type="number" min={1} max={100} value={product.rank === null || product.rank === undefined ? "" : String(product.rank)} onChange={(event) => replaceProduct(index, { rank: event.target.value ? Number(event.target.value) : null })} /></label>
          <button className="remove" type="button" onClick={() => patchRecord({ products: products.filter((_, current) => current !== index) })}>移除</button>
        </div>)}
        {products.length === 0 && <div className="admin-record-empty">没有产品数据，可点击“添加产品”补充。</div>}
      </section>

      <section className="admin-record-list"><header><div><b>引用信源</b><span>信源标题和网址会进入报告的信源统计</span></div><button type="button" onClick={() => patchRecord({ sources: [...sources, { title: "", url: "" }] })}>添加信源</button></header>
        {sources.map((source, index) => <div className="admin-source-row" key={`source-${index}`}>
          <label><span>信源标题</span><input value={String(source.title || "")} onChange={(event) => replaceSource(index, { title: event.target.value })} /></label>
          <label><span>网址</span><input type="url" value={String(source.url || source.href || "")} onChange={(event) => replaceSource(index, { url: event.target.value })} placeholder="https://" /></label>
          <button className="remove" type="button" onClick={() => patchRecord({ sources: sources.filter((_, current) => current !== index) })}>移除</button>
        </div>)}
        {sources.length === 0 && <div className="admin-record-empty">该轮没有引用信源。</div>}
      </section>

      <div className="admin-record-quality">
        <label className="admin-record-check"><input type="checkbox" checked={Boolean(record.body_capture_complete)} onChange={(event) => patchRecord({ body_capture_complete: event.target.checked })} /><span>正文采集完整</span></label>
        <label className="admin-record-check"><input type="checkbox" checked={Boolean(record.source_capture_complete)} onChange={(event) => patchRecord({ source_capture_complete: event.target.checked })} /><span>信源采集完整</span></label>
        <label><span>预计信源数量</span><input type="number" min={0} max={500} value={String(record.expected_source_count ?? sources.length)} onChange={(event) => patchRecord({ expected_source_count: Number(event.target.value || 0) })} /></label>
      </div>
    </div>
  </details>;
}

function basePath() {
  const configured = String(import.meta.env.VITE_MONITOR_BASE_PATH || "").trim().replace(/\/$/, "");
  if (configured || typeof window === "undefined") return configured;
  return window.location.pathname.startsWith("/geo") ? "/geo" : "";
}

function statusName(value: string) {
  return ({ queued: "排队中", running: "诊断中", completed: "已完成", failed: "待处理", cancelled: "已停止" } as Record<string, string>)[value] || value;
}

function localDateTimeValue(value = "") {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function futureDateTimeValue(days: number) {
  return localDateTimeValue(new Date(Date.now() + days * 86400000).toISOString());
}

const LOGIN_MODELS = [
  { id: "doubao", name: "豆包", radius: 126, duration: 22, angle: 15 },
  { id: "deepseek", name: "DeepSeek", radius: 126, duration: 22, angle: 195 },
  { id: "yuanbao", name: "腾讯元宝", radius: 196, duration: 30, angle: 80 },
  { id: "kimi", name: "Kimi", radius: 196, duration: 30, angle: 260 },
  { id: "wenxin", name: "文心一言", radius: 266, duration: 38, angle: 145 },
  { id: "quark", name: "千问", radius: 266, duration: 38, angle: 325 },
] as const;

function LoginModelIcon({ model }: { model: (typeof LOGIN_MODELS)[number] }) {
  const Icon = getIcon(model.id);
  return <div
    className={`geo-login-model-node ${model.id}`}
    style={{
      "--orbit-radius": `${model.radius}px`,
      "--orbit-duration": `${model.duration}s`,
      "--orbit-start": `${model.angle}deg`,
      "--orbit-start-negative": `${-model.angle}deg`,
    } as CSSProperties}
  ><span>{Icon ? <Icon size={34} /> : model.name.slice(0, 2)}<b>{model.name}</b></span></div>;
}

export function DiagnosisAdmin({ initialView = "reports" }: { initialView?: "reports" | "paid-monitors" }) {
  const isPaidMonitorPage = initialView === "paid-monitors";
  const [authenticated, setAuthenticated] = useState(false);
  const [checking, setChecking] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [reports, setReports] = useState<ReportItem[]>([]);
  const [rounds, setRounds] = useState(3);
  const [highProbabilityBrands, setHighProbabilityBrands] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [shareExpiry, setShareExpiry] = useState<Record<string, string>>({});
  const [savingReport, setSavingReport] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sharingFilter, setSharingFilter] = useState("all");
  const [sortOrder, setSortOrder] = useState("newest");
  const [refreshing, setRefreshing] = useState(false);
  const [copiedReport, setCopiedReport] = useState("");
  const [deleteCandidate, setDeleteCandidate] = useState<ReportItem | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [reportEditor, setReportEditor] = useState<ReportEditorData | null>(null);
  const [loadingEditor, setLoadingEditor] = useState("");
  const [savingEditor, setSavingEditor] = useState(false);
  const [revertingReport, setRevertingReport] = useState("");
  const [session, setSession] = useState<AdminSession | null>(null);
  const [accounts, setAccounts] = useState<AdminAccount[]>([]);
  const [accountDrafts, setAccountDrafts] = useState<Record<string, AccountDraft>>({});
  const [savingAccount, setSavingAccount] = useState("");
  const [newAccount, setNewAccount] = useState<AccountDraft>({
    username: "", display_name: "", password: "", active: true,
    expires_at: futureDateTimeValue(30), daily_limit: 3,
  });

  const reportStats = useMemo(() => ({
    total: reports.length,
    completed: reports.filter((item) => item.status === "completed").length,
    active: reports.filter((item) => item.status === "running" || item.status === "queued").length,
    public: reports.filter((item) => item.public_active).length,
  }), [reports]);

  const visibleReports = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase("zh-CN");
    return reports.filter((item) => {
      const searchable = `${item.brand_name} ${item.product_name} ${item.question} ${item.created_by_username || ""}`.toLocaleLowerCase("zh-CN");
      const statusMatches = statusFilter === "all"
        || (statusFilter === "active" && ["queued", "running"].includes(item.status))
        || (statusFilter === "attention" && ["failed", "cancelled"].includes(item.status))
        || item.status === statusFilter;
      const sharingMatches = sharingFilter === "all"
        || (sharingFilter === "public" && item.public_active)
        || (sharingFilter === "private" && !item.public_active);
      return (!keyword || searchable.includes(keyword)) && statusMatches && sharingMatches;
    }).sort((left, right) => {
      const delta = new Date(right.created_at || 0).getTime() - new Date(left.created_at || 0).getTime();
      return sortOrder === "oldest" ? -delta : delta;
    });
  }, [query, reports, sharingFilter, sortOrder, statusFilter]);

  const loadAccounts = useCallback(async () => {
    const response = await fetch(`${basePath()}/api/admin/accounts?_=${Date.now()}`, {
      cache: "no-store", credentials: "include",
    });
    const next = await response.json();
    if (!response.ok || !next.ok) throw new Error(next.error || "管理员账号读取失败");
    const items = (next.accounts || []) as AdminAccount[];
    setAccounts(items);
    setAccountDrafts(Object.fromEntries(items.map((item) => [item.id, {
      username: item.username,
      display_name: item.display_name,
      password: "",
      active: item.active,
      expires_at: localDateTimeValue(item.expires_at),
      daily_limit: Number(item.daily_limit || 1),
    }])));
  }, []);

  const loadReports = useCallback(async () => {
    const response = await fetch(`${basePath()}/api/admin/reports?_=${Date.now()}`, { cache: "no-store", credentials: "include" });
    const next = await response.json();
    if (response.status === 401) { setAuthenticated(false); return; }
    if (!response.ok || !next.ok) throw new Error(next.error || "报告列表读取失败");
    setReports(next.reports || []);
    setShareExpiry((current) => {
      const updated = { ...current };
      for (const item of next.reports || []) {
        if (!(item.id in updated)) updated[item.id] = localDateTimeValue(item.public_expires_at);
      }
      return updated;
    });
    setRounds(Number(next.settings?.diagnosis_rounds || 3));
    setHighProbabilityBrands((next.settings?.high_probability_brands || []).join("\n"));
    setSession(next.account || null);
    if (next.account?.role === "super_admin") await loadAccounts();
    else { setAccounts([]); setAccountDrafts({}); }
    setAuthenticated(true);
  }, [loadAccounts]);

  useEffect(() => {
    void (async () => {
      try {
        const response = await fetch(`${basePath()}/api/admin/session`, { cache: "no-store", credentials: "include" });
        const next = await response.json();
        if (next.authenticated) await loadReports();
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "管理员服务连接失败");
      } finally { setChecking(false); }
    })();
  }, [loadReports]);

  async function login(event: FormEvent) {
    event.preventDefault(); setError(""); setSaving(true);
    try {
      const response = await fetch(`${basePath()}/api/admin/login`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "登录失败");
      setPassword("");
      const requested = new URLSearchParams(window.location.search).get("next") || "";
      const base = basePath();
      const managerSafeNext = requested === `${base}/`
        || requested === `${base}/admin`
        || new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/[a-f0-9]{32}/?$`, "i").test(requested);
      if (requested && requested.startsWith(`${base}/`) && !requested.startsWith("//") && (next.role === "super_admin" || managerSafeNext)) {
        window.location.assign(requested);
        return;
      }
      await loadReports();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "登录失败"); }
    finally { setSaving(false); }
  }

  async function saveSettings() {
    setSaving(true); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/settings`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ diagnosis_rounds: rounds, high_probability_brands: highProbabilityBrands }),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "设置保存失败");
      setRounds(Number(next.settings.diagnosis_rounds));
      setHighProbabilityBrands((next.settings.high_probability_brands || []).join("\n"));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "设置保存失败"); }
    finally { setSaving(false); }
  }

  async function createAccount(event: FormEvent) {
    event.preventDefault(); setSavingAccount("new"); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/accounts`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(newAccount),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "管理员账号创建失败");
      setNewAccount({ username: "", display_name: "", password: "", active: true, expires_at: futureDateTimeValue(30), daily_limit: 3 });
      await loadAccounts();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "管理员账号创建失败"); }
    finally { setSavingAccount(""); }
  }

  async function saveAccount(accountId: string, activeOverride?: boolean) {
    const draft = accountDrafts[accountId];
    if (!draft) return;
    setSavingAccount(accountId); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/accounts/${accountId}`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...draft, active: activeOverride ?? draft.active }),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "管理员账号保存失败");
      await loadAccounts();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "管理员账号保存失败"); }
    finally { setSavingAccount(""); }
  }

  async function resetAccountQuota(accountId: string) {
    setSavingAccount(accountId); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/accounts/${accountId}/reset-quota`, {
        method: "POST", credentials: "include",
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "今日诊断次数重置失败");
      await loadAccounts();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "今日诊断次数重置失败"); }
    finally { setSavingAccount(""); }
  }

  function patchAccountDraft(accountId: string, patch: Partial<AccountDraft>) {
    setAccountDrafts((current) => ({
      ...current,
      [accountId]: { ...current[accountId], ...patch },
    }));
  }

  async function logout() {
    await fetch(`${basePath()}/api/admin/logout`, { method: "POST", credentials: "include" });
    setAuthenticated(false); setReports([]); setAccounts([]); setSession(null); setUsername(""); setPassword("");
  }

  async function updateSharing(item: ReportItem, enabled: boolean) {
    setSavingReport(item.id); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/reports/${item.report_key}/sharing`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, expires_at: enabled ? shareExpiry[item.id] || "" : "" }),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "公开设置保存失败");
      await loadReports();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "公开设置保存失败"); }
    finally { setSavingReport(""); }
  }

  async function refreshReports() {
    setRefreshing(true); setError("");
    try { await loadReports(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "报告列表刷新失败"); }
    finally { setRefreshing(false); }
  }

  async function copyPublicLink(item: ReportItem) {
    try {
      await navigator.clipboard.writeText(`${window.location.origin}${basePath()}/${item.report_key}`);
      setCopiedReport(item.id);
      window.setTimeout(() => setCopiedReport((current) => current === item.id ? "" : current), 1800);
    } catch (_) { setError("公开链接复制失败，请打开报告后从地址栏复制"); }
  }

  function openDeleteDialog(item: ReportItem) {
    setDeleteCandidate(item);
    setDeleteConfirmation("");
  }

  function closeDeleteDialog() {
    if (savingReport) return;
    setDeleteCandidate(null);
    setDeleteConfirmation("");
  }

  async function deleteReport() {
    if (!deleteCandidate) return;
    setSavingReport(deleteCandidate.id); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/reports/${deleteCandidate.report_key}/delete`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm_brand: deleteConfirmation }),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "报告删除失败");
      setReports((current) => current.filter((item) => item.id !== deleteCandidate.id));
      setDeleteCandidate(null); setDeleteConfirmation("");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "报告删除失败"); }
    finally { setSavingReport(""); }
  }

  async function openReportEditor(item: ReportItem) {
    setLoadingEditor(item.id); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/reports/${item.report_key}/editor?_=${Date.now()}`, {
        cache: "no-store", credentials: "include",
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "报告编辑数据读取失败");
      const editor = next.editor as ReportEditorData;
      setReportEditor(editor);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "报告编辑数据读取失败"); }
    finally { setLoadingEditor(""); }
  }

  function closeReportEditor() {
    if (savingEditor) return;
    setReportEditor(null);
  }

  async function saveReportEditor() {
    if (!reportEditor) return;
    setSavingEditor(true); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/reports/${reportEditor.report_key}/edit`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          brand_name: reportEditor.brand_name,
          product_name: reportEditor.product_name,
          question: reportEditor.question,
          high_probability_prior: reportEditor.high_probability_prior,
          records: reportEditor.records,
        }),
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "报告保存失败");
      setReportEditor(next.editor as ReportEditorData);
      await loadReports();
      setReportEditor(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "报告保存失败");
    } finally { setSavingEditor(false); }
  }

  async function revertReport(reportKey: string, operationKey: string) {
    if (!window.confirm("确定撤回这份报告的全部人工修改吗？报告将恢复到第一次编辑之前的原始状态。")) return;
    setRevertingReport(operationKey); setError("");
    try {
      const response = await fetch(`${basePath()}/api/admin/reports/${reportKey}/revert`, {
        method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const next = await response.json();
      if (!response.ok || !next.ok) throw new Error(next.error || "报告修改撤回失败");
      if (reportEditor?.report_key === reportKey) setReportEditor(null);
      await loadReports();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "报告修改撤回失败"); }
    finally { setRevertingReport(""); }
  }

  if (checking) return <main className="geo-login-shell"><section className="geo-login-loading"><EnterpriseBrandMark size={58} /><span>正在验证安全会话…</span></section></main>;
  if (!authenticated) return <main className="geo-login-shell">
    <section className="geo-login-visual" aria-hidden="true">
      <div className="geo-login-ambient one" /><div className="geo-login-ambient two" />
      <div className="geo-login-orbits">
        {[126, 196, 266].map((radius) => <i key={radius} style={{ width: radius * 2, height: radius * 2 }} />)}
        {LOGIN_MODELS.map((model) => <LoginModelIcon key={model.id} model={model} />)}
        <div className="geo-login-core"><EnterpriseBrandMark size={116} /><small>AI MODEL INTELLIGENCE</small></div>
      </div>
      <div className="geo-login-visual-copy"><small>ENTERPRISE MODEL MONITOR</small><h2>洞察每一次<br />大模型推荐</h2><p>豆包、腾讯元宝、文心一言、千问、DeepSeek 与 Kimi，共同构成企业级模型洞察视野。</p></div>
    </section>
    <section className="geo-login-panel"><form className="geo-login-form" onSubmit={login}>
      <div className="geo-login-brand"><EnterpriseBrandLockup /></div>
      <div className="geo-login-heading"><small>SECURE ACCESS</small><h1>欢迎回来</h1><p>登录后进入大模型推荐诊断与报告管理中心。</p></div>
      {error && <div className="diagnosis-error">{error}</div>}
      <label className="geo-login-field"><span>管理员账号</span><div><input autoComplete="username" autoFocus value={username} onChange={(e) => setUsername(e.target.value)} placeholder="请输入管理员账号" /></div></label>
      <label className="geo-login-field"><span>登录密码</span><div><input autoComplete="current-password" type={showPassword ? "text" : "password"} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="请输入登录密码" /><button type="button" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? "隐藏密码" : "显示密码"}>{showPassword ? "隐藏" : "显示"}</button></div></label>
      <button className="geo-login-submit" disabled={saving || !username || !password}>{saving ? "正在安全登录…" : <><span>进入诊断平台</span><b>→</b></>}</button>
      <div className="geo-login-security"><i />企业级加密会话 · 仅授权管理员可访问</div>
    </form></section>
  </main>;

  return <main className={`admin-shell${isPaidMonitorPage ? " admin-paid-monitor-page" : ""}`}><section className="admin-panel">
    <header><div className="admin-title-lockup"><EnterpriseBrandLockup compact /><div><small>{session?.role === "super_admin" ? "SUPER ADMIN" : "ACCOUNT WORKSPACE"}</small><h1>{isPaidMonitorPage ? "付费用户监控" : session?.role === "super_admin" ? "诊断运营管理" : "我的诊断报告"}</h1><p>{session?.display_name || session?.username} · {isPaidMonitorPage ? "每日监控配置与任务进度" : session?.role === "super_admin" ? "全局报告与账号控制" : `今日剩余 ${session?.quota?.remaining ?? 0} / ${session?.quota?.limit ?? 0} 次`}</p></div></div><nav className="admin-header-actions" aria-label="管理操作">{session?.role === "super_admin" && (isPaidMonitorPage ? <a className="admin-nav-feature" href={`${basePath()}/admin`}>返回诊断管理</a> : <a className="admin-nav-feature" href={`${basePath()}/admin/paid-monitors`}>付费用户每日监控</a>)}{session?.role === "super_admin" && <a href={`${basePath()}/tasks`}>任务管理</a>}<a className="admin-nav-primary" href={`${basePath()}/`}>新建诊断</a>{!isPaidMonitorPage && <button className="secondary" disabled={refreshing} onClick={refreshReports}>{refreshing ? "刷新中…" : "刷新数据"}</button>}<button className="secondary admin-logout" onClick={logout}>退出登录</button></nav></header>
    {error && <div className="diagnosis-error">{error}</div>}
    {!isPaidMonitorPage && <section className="admin-overview" aria-label="报告概览">
      <article><span>全部报告</span><b>{reportStats.total}</b><small>累计诊断记录</small></article>
      <article><span>已完成</span><b>{reportStats.completed}</b><small>可查看完整结果</small></article>
      <article><span>进行中</span><b>{reportStats.active}</b><small>执行或等待任务</small></article>
      <article><span>公开访问</span><b>{reportStats.public}</b><small>客户当前可查看</small></article>
    </section>}
    {!isPaidMonitorPage && session?.role === "super_admin" && <section className="admin-settings admin-settings-enterprise">
      <div className="admin-setting-copy"><small>DIAGNOSIS POLICY</small><b>诊断与概率策略</b><span>名单作为公开标注的高概率先验参与校准；不在名单中的品牌按原普通策略评分再降低 50%，现行上限为 15%。原始命中率和回答证据始终保留。</span></div>
      <label className="admin-rounds-field"><span>默认诊断轮数</span><input min={1} max={20} type="number" value={rounds} onChange={(e) => setRounds(Number(e.target.value))} /></label>
      <label className="admin-brand-prior-field"><span>高概率品牌名单（每行一个）</span><textarea value={highProbabilityBrands} onChange={(event) => setHighProbabilityBrands(event.target.value)} placeholder={"例如：\n品牌 A\n品牌 B"} /></label>
      <button disabled={saving} onClick={saveSettings}>{saving ? "保存中…" : "保存策略"}</button>
    </section>}
    {!isPaidMonitorPage && session?.role === "super_admin" && <section className="admin-account-center" aria-label="下级管理员账号">
      <header><div><small>ACCESS CONTROL</small><h2>下级管理员账号</h2><p>账号暂停或到期后，会话立即失效，不能登录、创建诊断或查看后台报告。</p></div><span>{accounts.filter((item) => item.can_login).length} 个可用账号</span></header>
      <form className="admin-account-create" onSubmit={createAccount}>
        <label><span>登录账号</span><input required minLength={3} maxLength={40} value={newAccount.username} onChange={(event) => setNewAccount((current) => ({ ...current, username: event.target.value }))} placeholder="例如：sales01" /></label>
        <label><span>显示名称</span><input value={newAccount.display_name} onChange={(event) => setNewAccount((current) => ({ ...current, display_name: event.target.value }))} placeholder="例如：华东客户经理" /></label>
        <label><span>初始密码</span><input required minLength={8} type="password" autoComplete="new-password" value={newAccount.password} onChange={(event) => setNewAccount((current) => ({ ...current, password: event.target.value }))} placeholder="至少 8 位" /></label>
        <label><span>有效期至</span><input type="datetime-local" value={newAccount.expires_at} onChange={(event) => setNewAccount((current) => ({ ...current, expires_at: event.target.value }))} /></label>
        <label><span>每日最多诊断</span><input type="number" min={1} max={1000} value={newAccount.daily_limit} onChange={(event) => setNewAccount((current) => ({ ...current, daily_limit: Number(event.target.value) }))} /></label>
        <button disabled={savingAccount === "new"}>{savingAccount === "new" ? "创建中…" : "添加管理员"}</button>
      </form>
      <div className="admin-account-list">
        {accounts.map((item) => {
          const draft = accountDrafts[item.id];
          if (!draft) return null;
          return <article key={item.id} className={!item.can_login ? "disabled" : ""}>
            <div className="admin-account-summary"><div><b>{item.display_name || item.username}</b><span>@{item.username}</span></div><span className={`admin-account-state ${item.can_login ? "active" : "paused"}`}>{item.expired ? "已到期" : item.active ? "使用中" : "已暂停"}</span></div>
            <div className="admin-account-fields">
              <label><span>登录账号</span><input value={draft.username} onChange={(event) => patchAccountDraft(item.id, { username: event.target.value })} /></label>
              <label><span>显示名称</span><input value={draft.display_name} onChange={(event) => patchAccountDraft(item.id, { display_name: event.target.value })} /></label>
              <label><span>重设密码</span><input type="password" autoComplete="new-password" value={draft.password} onChange={(event) => patchAccountDraft(item.id, { password: event.target.value })} placeholder="留空则不修改" /></label>
              <label><span>有效期至</span><input type="datetime-local" value={draft.expires_at} onChange={(event) => patchAccountDraft(item.id, { expires_at: event.target.value })} /></label>
              <label><span>每日最多诊断</span><input type="number" min={1} max={1000} value={draft.daily_limit} onChange={(event) => patchAccountDraft(item.id, { daily_limit: Number(event.target.value) })} /></label>
            </div>
            <footer><span>今日已用 <b>{item.used_today}</b> / {item.daily_limit} 次 · 剩余 {item.remaining_today} 次{item.last_login_at ? ` · 最近登录 ${item.last_login_at.replace("T", " ")}` : " · 尚未登录"}</span><div><button type="button" className="secondary" disabled={savingAccount === item.id} onClick={() => saveAccount(item.id)}>{savingAccount === item.id ? "保存中…" : "保存设置"}</button><button type="button" className="secondary quota-reset" disabled={savingAccount === item.id || item.used_today === 0} onClick={() => resetAccountQuota(item.id)}>重置今日次数</button><button type="button" className={item.active ? "danger" : ""} disabled={savingAccount === item.id} onClick={() => saveAccount(item.id, !item.active)}>{item.active ? "暂停账号" : "恢复账号"}</button></div></footer>
          </article>;
        })}
        {accounts.length === 0 && <div className="admin-empty"><b>还没有下级管理员</b><span>在上方设置账号、密码、有效期和每日额度后即可添加。</span></div>}
      </div>
    </section>}
    {isPaidMonitorPage && session?.role === "super_admin" && <PaidMonitorCenter />}
    {isPaidMonitorPage && session?.role !== "super_admin" && <section className="admin-access-denied"><small>ACCESS RESTRICTED</small><h2>当前账号无权访问付费用户监控</h2><p>该页面仅向总管理员开放。你仍可以返回自己的诊断报告页面。</p><a href={`${basePath()}/admin`}>返回诊断报告</a></section>}
    {!isPaidMonitorPage && <div className="admin-section-heading"><div><small>REPORT LIBRARY</small><h2>诊断报告</h2><p>集中检索、开放客户访问并管理全部诊断记录。</p></div><span>{reports.length} 份报告</span></div>}
    {!isPaidMonitorPage && <section className="admin-report-toolbar" aria-label="报告筛选">
      <label className="admin-search"><span>搜索报告</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索品牌、产品或诊断问题" /></label>
      <label><span>任务状态</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">全部状态</option><option value="completed">已完成</option><option value="active">进行中</option><option value="attention">待处理</option><option value="paused">已暂停</option></select></label>
      <label><span>访问权限</span><select value={sharingFilter} onChange={(event) => setSharingFilter(event.target.value)}><option value="all">全部权限</option><option value="public">公开中</option><option value="private">仅管理员</option></select></label>
      <label><span>排列方式</span><select value={sortOrder} onChange={(event) => setSortOrder(event.target.value)}><option value="newest">最新优先</option><option value="oldest">最早优先</option></select></label>
      <span className="admin-filter-count">显示 {visibleReports.length} / {reports.length}</span>
    </section>}
    {!isPaidMonitorPage && <section className="admin-report-list">{visibleReports.map((item) => <article key={item.id}>
      <div><span className="admin-report-badges"><span className={`admin-status ${item.status}`}>{item.status === "queued" && Number(item.retry_count || 0) > 0 ? "自动恢复中" : statusName(item.status)}</span>{item.public_active ? <span className="admin-share-status active">公开中</span> : item.public_expired ? <span className="admin-share-status expired">已到期</span> : <span className="admin-share-status">未公开</span>}<span className={`admin-policy-status ${item.high_probability_strategy_active ? "high" : "ordinary"}`}>{item.high_probability_strategy_active ? "当前采用高概率策略" : "当前采用普通品牌策略"}</span></span><small>{item.created_at?.replace("T", " ")}</small></div>
      <h2>{item.brand_name || "未命名品牌"}<small>{item.product_name ? ` · ${item.product_name}` : ""}</small></h2><p>{item.question}</p>
      <div className="admin-report-owner"><span>诊断账号</span><b>{item.created_by_username || "历史管理员"}</b></div>
      {item.status === "completed" && item.report_key && <section className="admin-sharing-controls">
        <div><b>公开访问</b><span>{item.public_active ? (item.public_expires_at ? `公开至 ${item.public_expires_at.replace("T", " ")}` : "永久公开，任何人凭链接可查看") : item.public_expired ? "公开期限已到，当前仅管理员可查看" : "当前仅管理员可查看"}</span></div>
        <label><span>截止时间（留空为永久）</span><input type="datetime-local" value={shareExpiry[item.id] || ""} onChange={(event) => setShareExpiry((current) => ({ ...current, [item.id]: event.target.value }))} /></label>
        <div className="admin-sharing-shortcuts"><button type="button" onClick={() => setShareExpiry((current) => ({ ...current, [item.id]: futureDateTimeValue(1) }))}>1天</button><button type="button" onClick={() => setShareExpiry((current) => ({ ...current, [item.id]: futureDateTimeValue(7) }))}>7天</button><button type="button" onClick={() => setShareExpiry((current) => ({ ...current, [item.id]: futureDateTimeValue(30) }))}>30天</button><button type="button" onClick={() => setShareExpiry((current) => ({ ...current, [item.id]: "" }))}>永久</button></div>
        <button className={item.public_active ? "admin-share-toggle pause" : "admin-share-toggle"} disabled={savingReport === item.id} onClick={() => updateSharing(item, !item.public_active)}>{savingReport === item.id ? "保存中…" : item.public_active ? "暂停公开" : "开启公开"}</button>
      </section>}
      <footer><span>{item.completed_steps}/{item.total_steps} 项</span><div className="admin-report-actions">{item.public_active && <button className="admin-copy-link" type="button" onClick={() => copyPublicLink(item)}>{copiedReport === item.id ? "已复制链接" : "复制公开链接"}</button>}{item.report_key && <a href={`${basePath()}/${item.report_key}`} target="_blank" rel="noreferrer">查看报告</a>}{session?.role === "super_admin" && item.report_key && <button className="admin-edit-report" type="button" disabled={item.status !== "completed" || loadingEditor === item.id} onClick={() => openReportEditor(item)}>{loadingEditor === item.id ? "载入中…" : item.status === "completed" ? "编辑报告" : "完成后可编辑"}</button>}{session?.role === "super_admin" && item.report_key && Number(item.manual_edit_count || 0) > 0 && <button className="admin-revert-report" type="button" disabled={revertingReport === item.id} onClick={() => revertReport(item.report_key, item.id)}>{revertingReport === item.id ? "撤回中…" : `撤回修改 (${item.manual_edit_count})`}</button>}{session?.role === "super_admin" && <button className="admin-delete-report" type="button" disabled={["queued", "running"].includes(item.status)} onClick={() => openDeleteDialog(item)}>{["queued", "running"].includes(item.status) ? "进行中不可删除" : "删除报告"}</button>}</div></footer>
    </article>)}{visibleReports.length === 0 && <div className="admin-empty"><b>没有匹配的报告</b><span>请调整搜索词或筛选条件。</span></div>}</section>}
    {!isPaidMonitorPage && reportEditor && <div className="admin-dialog-backdrop admin-editor-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeReportEditor(); }}><section className="admin-report-editor" role="dialog" aria-modal="true" aria-labelledby="edit-report-title">
      <header><div><small>REPORT DATA EDITOR</small><h2 id="edit-report-title">编辑诊断报告</h2><p>保存后，推荐概率、排名、竞品与信源汇总会基于下方证据重新计算，并升级到当前概率策略 v{reportEditor.current_probability_policy_version}。系统标识不可增删，所有修改均保留审计记录。</p></div><button type="button" className="secondary" onClick={closeReportEditor}>关闭</button></header>
      <div className="admin-editor-fields">
        <label><span>品牌名称</span><input value={reportEditor.brand_name} onChange={(event) => setReportEditor((current) => current ? { ...current, brand_name: event.target.value } : current)} /></label>
        <label><span>产品名称</span><input value={reportEditor.product_name} onChange={(event) => setReportEditor((current) => current ? { ...current, product_name: event.target.value } : current)} /></label>
        <label className="wide"><span>诊断问题</span><input value={reportEditor.question} onChange={(event) => setReportEditor((current) => current ? { ...current, question: event.target.value } : current)} /></label>
        <label className="wide admin-editor-policy"><input type="checkbox" checked={reportEditor.high_probability_prior} onChange={(event) => setReportEditor((current) => current ? { ...current, high_probability_prior: event.target.checked } : current)} /><span>采用高概率品牌校准策略</span></label>
      </div>
      <div className="admin-editor-guide"><b>{reportEditor.records.length} 条模型采集记录</b><span>当前报告策略 v{reportEditor.probability_policy_version}。按模型和轮次展开编辑；保存后使用现行策略重新计算，撤回时连同原策略版本一起恢复。</span></div>
      <div className="admin-record-forms">{reportEditor.records.map((item, index) => <ReportRecordForm key={item.request_id} item={item} defaultOpen={index === 0} onChange={(value) => setReportEditor((current) => current ? { ...current, records: current.records.map((record, currentIndex) => currentIndex === index ? value : record) } : current)} />)}</div>
      <footer><span>{reportEditor.audits.length ? `已修改 ${reportEditor.audits.length} 次 · 最近：${reportEditor.audits[0].editor_username} · ${reportEditor.audits[0].created_at.replace("T", " ")}` : "尚无人工修改记录"}</span><div>{reportEditor.audits.length > 0 && <button type="button" className="admin-editor-revert" disabled={revertingReport === reportEditor.report_key} onClick={() => revertReport(reportEditor.report_key, reportEditor.report_key)}>{revertingReport === reportEditor.report_key ? "撤回中…" : "撤回所有修改"}</button>}<button type="button" className="secondary" onClick={closeReportEditor}>取消</button><button type="button" className="admin-editor-save" disabled={savingEditor || !reportEditor.brand_name.trim() || !reportEditor.question.trim()} onClick={saveReportEditor}>{savingEditor ? "正在校验并重算…" : `保存并按 v${reportEditor.current_probability_policy_version} 重新计算`}</button></div></footer>
    </section></div>}
    {!isPaidMonitorPage && deleteCandidate && <div className="admin-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDeleteDialog(); }}><section className="admin-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-report-title">
      <small>PERMANENT DELETE</small><h2 id="delete-report-title">永久删除这份报告？</h2>
      <p>报告、回答结果和任务事件将一并删除，此操作无法撤销。正在执行或排队中的任务不能删除。</p>
      <div className="admin-delete-target"><span>将要删除</span><b>{deleteCandidate.brand_name || "未命名品牌"}</b><small>{deleteCandidate.product_name || deleteCandidate.question}</small></div>
      <label><span>输入品牌名称 <b>{deleteCandidate.brand_name || deleteCandidate.report_key}</b> 以确认</span><input autoFocus value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} placeholder="请输入完整品牌名称" /></label>
      <footer><button className="secondary" type="button" onClick={closeDeleteDialog}>取消</button><button className="danger" type="button" disabled={savingReport === deleteCandidate.id || deleteConfirmation.trim() !== (deleteCandidate.brand_name?.trim() || deleteCandidate.report_key)} onClick={deleteReport}>{savingReport === deleteCandidate.id ? "正在删除…" : "确认永久删除"}</button></footer>
    </section></div>}
  </section></main>;
}
