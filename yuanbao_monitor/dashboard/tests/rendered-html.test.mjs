import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = new URL("../app/", import.meta.url);

test("统一面板由后端模型目录驱动并提供完整分析工作视图", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /\/api\/models/);
  assert.match(source, /\/api\/analytics/);
  assert.match(source, /模型总览/);
  assert.match(source, /问题对比/);
  assert.match(source, /信源洞察/);
  assert.match(source, /品牌与产品/);
  assert.match(source, /回答审计/);
  assert.match(source, /采集控制/);
  assert.doesNotMatch(source, /modelRegistry/);
  assert.match(source, /const visibleModelCatalog = analytics\.model_catalog/);
  assert.match(source, /\(item\) => !model \|\| item\.id === model/);
  assert.doesNotMatch(source, /item\.ingest_only \|\| item\.id === model \|\| item\.runs > 0/);
  assert.match(source, /const participatingModelCount = selectedModels\.filter/);
  assert.match(source, /有数据模型 \/ 设备/);
});

test("采集控制包含问题计划、账号校验及启停能力", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /每行一个，启动前自动保存/);
  assert.match(source, /仅保存问题/);
  assert.match(source, /检查网页登录/);
  assert.match(source, /account-check/);
  assert.match(source, /api\/control\/\$\{modelId\}/);
  assert.doesNotMatch(source, /实时回传日志/);
  assert.match(source, /启动请求已接收/);
  assert.match(source, /正在启动…/);
  assert.match(source, /Chrome 启动、账号校验完成后会自动运行采集脚本/);
  assert.match(source, /状态每 2 秒从采集日志同步/);
  assert.match(source, /无头网页直采/);
  assert.match(source, /打开远程任务中心/);
  assert.match(source, /window\.location\.port === "3000"/);
  assert.match(source, /Production Nginx exposes the frontend and \/api on one HTTPS origin/);
});

test("信源分析区显示每日 Top 25、关键词和五模型自有链接交集", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /高频文章 Top 25/);
  assert.match(source, /高频视频 Top 25/);
  assert.match(source, /文章文案关键词/);
  assert.match(source, /视频文案关键词/);
  assert.match(source, /自有品牌/);
  assert.match(source, /文章标题关键词每日变化/);
  assert.match(source, /正文产品提及率与每日名次/);
  assert.match(source, /不等待产品 AI 解析/);
  assert.match(source, /待解析不计为未提及/);
  assert.match(source, /个已分析唯一信源提及/);
  assert.match(source, /个待正文分析/);
  assert.match(source, /五模型共同提取的自有信源链接/);
  assert.match(source, /未同时被五个模型提取的链接不会显示/);
  assert.match(source, /恰好被两个模型共同提取的自有信源链接/);
  assert.match(source, /已进入五模型交集的链接不重复展示/);
  assert.match(source, /选择对象/);
  assert.match(source, /竞品（文章正文命中）/);
  assert.match(source, /高\/中质量文章正文/);
  assert.match(source, /严格服从当前问题和日期口径/);
  assert.match(source, /common_competitor_sources/);
  assert.match(source, /two_model_competitor_sources/);
  assert.match(source, /全部非自有竞品/);
  assert.match(source, /allCompetitors/);
  assert.match(source, /api\/analytics\/source-intersections/);
  assert.match(source, /模型交集均按当前全部日期完整统计/);
  assert.match(source, /sourceIntersectionsMatch/);
});

test("总览提供每日自有产品跨模型上榜看板并区分缺数与负样本", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /我的产品每日上榜情况/);
  assert.match(source, /任意1轮回答正文确认推荐，即记为上榜/);
  assert.match(source, /已上榜/);
  assert.match(source, /未上榜/);
  assert.match(source, /待复核/);
  assert.match(source, /未采集/);
  assert.match(source, /owned_product_daily/);
  assert.match(source, /models\.map\(\(item\) => <th key=\{item\.id\}>\{item\.name\}<\/th>\)/);
  assert.match(source, /const display = stateText\(row\.models\[item\.id\]\)/);
});

test("页面元数据使用通用多模型产品名称", async () => {
  const layout = await readFile(new URL("layout.tsx", app), "utf8");
  assert.match(layout, /模型情报台/);
  assert.doesNotMatch(layout, /Starter Project|codex-preview|豆包 × 元宝/);
});

test("客户从根页面创建诊断并在完成后进入随机密钥报告页", async () => {
  const home = await readFile(new URL("page.tsx", app), "utf8");
  const start = await readFile(new URL("DiagnosisStart.tsx", app), "utf8");
  const source = await readFile(new URL("DiagnosisDashboard.tsx", app), "utf8");
  const route = await readFile(new URL("[customer]/page.tsx", app), "utf8");
  const missingRoute = await readFile(new URL("[customer]/[...missing]/page.tsx", app), "utf8");
  const admin = await readFile(new URL("admin/page.tsx", app), "utf8");
  assert.match(home, /DiagnosisStart/);
  assert.doesNotMatch(home, /<Dashboard/);
  assert.match(start, /\/api\/diagnosis/);
  assert.match(start, /report_key/);
  assert.match(start, /geoActiveReportKey/);
  assert.match(start, /response\.status === 404/);
  assert.match(start, /clearActiveDiagnosis/);
  assert.match(start, /sessionStorage\.removeItem\("geoActiveReportKey"\)/);
  assert.match(start, /window\.location\.pathname\.startsWith\("\/geo\/"\)/);
  assert.match(start, /采集服务连接失败，正在自动重试/);
  assert.match(start, /window\.location\.assign/);
  assert.match(start, /需要诊断的具体产品/);
  assert.match(start, /提交并排队/);
  assert.match(start, /重新运行本次诊断/);
  assert.match(start, /返回重新填写/);
  assert.match(start, /retryFailedTask/);
  assert.match(start, /slice\(0, unsuccessful \? 5 : 12\)/);
  assert.match(source, /三模型 · 每模型 8 轮 · 共 24 轮/);
  assert.match(source, /综合推荐率/);
  assert.match(source, /逐轮回答正文与全部信源/);
  assert.match(source, /展开全部正文/);
  assert.match(source, /本轮全部信源/);
  assert.doesNotMatch(source, /report\.sources\.slice\(0, 30\)/);
  assert.match(source, /实时诊断日志/);
  assert.match(source, /api\/diagnosis\/\$\{customerSlug\}/);
  assert.match(source, /response\.status === 404/);
  assert.match(source, /!next\.task \|\| next\.task\.status !== "completed"/);
  assert.match(source, /sessionStorage\.removeItem\("geoActiveReportKey"\)/);
  assert.match(source, /window\.location\.replace/);
  assert.doesNotMatch(source, /startDiagnosis/);
  assert.match(route, /DiagnosisDashboard/);
  assert.match(route, /\^\[a-f0-9\]\{32\}\$/);
  assert.match(route, /redirect\(rootPath\)/);
  assert.match(route, /MONITOR_PUBLIC_ORIGIN/);
  assert.match(route, /api\/diagnosis\/\$\{reportKey\}/);
  assert.match(route, /payload\?\.task\?\.status !== "completed"/);
  assert.match(route, /dynamic = "force-dynamic"/);
  assert.match(route, /revalidate = 0/);
  assert.match(missingRoute, /redirect\(/);
  assert.match(missingRoute, /MONITOR_PUBLIC_ORIGIN/);
  assert.match(admin, /Dashboard/);
});

test("生产构建默认把前端资源挂载在 /geo 下", async () => {
  const config = await readFile(new URL("../vite.config.ts", import.meta.url), "utf8");
  const packageJson = await readFile(new URL("../package.json", import.meta.url), "utf8");
  const verifier = await readFile(new URL("../scripts/verify-production-assets.mjs", import.meta.url), "utf8");
  assert.match(config, /process\.env\.NODE_ENV === "production" \? "\/geo"/);
  assert.match(config, /configuredBasePath\.replace/);
  assert.match(packageJson, /verify:production-assets/);
  assert.match(verifier, /refusing to publish an unstyled dashboard/);
  assert.match(verifier, /Production CSS or JavaScript assets are missing/);
});

test("日期筛选首屏按北京时间一次性初始化", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /const \[date, setDate\] = useState\(\(\) => new Intl\.DateTimeFormat/);
  assert.doesNotMatch(source, /localStorage\.getItem\("monitorSelectedDate"\)/);
  assert.match(source, /localStorage\.setItem\("monitorSelectedDate", date\)/);
  assert.match(source, /timeZone: "Asia\/Shanghai"/);
  assert.match(source, /const filtersReady = true/);
  assert.match(source, /if \(!filtersReady\) return/);
  assert.doesNotMatch(source, /setFiltersReady/);
});

test("分析刷新不做日期预取并使用低开销条件轮询", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.doesNotMatch(source, /analyticsPrefetches|datesToWarm/);
  assert.doesNotMatch(source, /targetView of \["brands", "compare", "sources"\]/);
  assert.match(source, /silent && analyticsRequest\.current/);
  assert.match(source, /setInterval\(refreshLiveData, 15000\)/);
  assert.match(source, /analyticsResponseEtag/);
  assert.match(source, /cache: "no-cache"/);
  assert.match(source, /A filter label must never be paired with the previous filter's rows/);
  assert.doesNotMatch(source, /analyticsPayload\.questions\.includes\(question\).*setQuestion/);
  assert.doesNotMatch(source, /analyticsPayload\.dates\.includes\(date\).*setDate/);
  assert.match(source, /selectors must remain controlled by the user's last action/);
  assert.match(source, /filters\?\.view \|\| ""/);
  assert.match(source, /全部日期汇总已加载/);
  assert.match(source, /全部日期完整统计/);
  assert.match(source, /if \(view === "control"\)/);
  assert.match(source, /view !== "control" && \(/);
});
