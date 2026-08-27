import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const baseDir = process.cwd();
const outputDir = path.join(baseDir, "outputs", "eyebrow_eyelash_source_mix_20260723");
const payloadPath = path.join(outputDir, "analysis_payload.json");
const outputPath = path.join(outputDir, "眉毛睫毛_自有产品信源文章视频占比与标题关键词分析.xlsx");
const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));

const wb = Workbook.create();
const report = wb.worksheets.add("分析报告");
const daily = wb.worksheets.add("每日占比");
const details = wb.worksheets.add("信源明细");
const keywords = wb.worksheets.add("标题关键词");
const shifts = wb.worksheets.add("日间关键词变化");
const quality = wb.worksheets.add("数据质量");
const method = wb.worksheets.add("方法说明");

const colors = {
  navy: "#16324F",
  teal: "#0F8B7A",
  tealLight: "#DDF3EE",
  blue: "#2F75B5",
  blueLight: "#DCEAF7",
  orange: "#E58A1F",
  orangeLight: "#FCEAD3",
  red: "#C0392B",
  green: "#2E8B57",
  gray900: "#17212B",
  gray700: "#4F5B66",
  gray500: "#7A8793",
  gray300: "#D7DEE4",
  gray100: "#F5F7F8",
  white: "#FFFFFF",
};

function colName(index1) {
  let n = index1;
  let result = "";
  while (n > 0) {
    n -= 1;
    result = String.fromCharCode(65 + (n % 26)) + result;
    n = Math.floor(n / 26);
  }
  return result;
}

function writeObjectTable(sheet, startRow, startCol, rows, headers, options = {}) {
  const endCol = startCol + headers.length - 1;
  const endRow = startRow + rows.length;
  const rangeAddress = `${colName(startCol)}${startRow}:${colName(endCol)}${endRow}`;
  const matrix = [
    headers,
    ...rows.map((row) => headers.map((header) => row[header] ?? null)),
  ];
  sheet.getRange(rangeAddress).values = matrix;
  const headerRange = sheet.getRange(
    `${colName(startCol)}${startRow}:${colName(endCol)}${startRow}`,
  );
  headerRange.format = {
    fill: options.headerFill || colors.navy,
    font: { bold: true, color: colors.white },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: colors.gray300 },
  };
  const bodyRange = sheet.getRange(
    `${colName(startCol)}${startRow + 1}:${colName(endCol)}${endRow}`,
  );
  bodyRange.format = {
    borders: { preset: "all", style: "thin", color: colors.gray300 },
    verticalAlignment: "top",
  };
  if (options.tableName && rows.length > 0) {
    const table = sheet.tables.add(rangeAddress, true, options.tableName);
    table.style = options.tableStyle || "TableStyleMedium2";
    table.showBandedRows = true;
  }
  return { endRow, endCol, rangeAddress };
}

function formatTitle(sheet, title, subtitle, endCol = "P") {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${endCol}2`).merge();
  sheet.getRange("A1").values = [[title]];
  sheet.getRange(`A1:${endCol}2`).format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white, size: 20 },
    verticalAlignment: "center",
    horizontalAlignment: "left",
  };
  sheet.getRange(`A3:${endCol}3`).merge();
  sheet.getRange("A3").values = [[subtitle]];
  sheet.getRange(`A3:${endCol}3`).format = {
    fill: colors.tealLight,
    font: { color: colors.gray700, italic: true, size: 10 },
    verticalAlignment: "center",
  };
  sheet.getRange("A1").format.rowHeight = 28;
  sheet.getRange("A2").format.rowHeight = 18;
  sheet.getRange("A3").format.rowHeight = 24;
}

function styleCard(sheet, rangeAddress, fill) {
  const range = sheet.getRange(rangeAddress);
  range.format = {
    fill,
    borders: { preset: "outside", style: "medium", color: colors.gray300 },
    verticalAlignment: "center",
    wrapText: true,
  };
}

function setWidths(sheet, widths) {
  for (const [col, width] of Object.entries(widths)) {
    sheet.getRange(`${col}:${col}`).format.columnWidth = width;
  }
}

// 信源明细：先写明细，供所有公式审计。
const detailHeaders = [
  "日期", "品类", "自有产品", "问题", "轮次", "信源序号", "信源类型", "原始类型",
  "媒体", "域名", "标题", "链接", "标准化链接", "命中位置", "标题命中", "正文命中",
  "正文状态", "正文质量", "正文抓取时间", "正文错误", "来源识别备注", "当日唯一链接首条",
];
const detailRowsForWorkbook = payload.details.map((row) => ({
  ...row,
  "当日唯一链接首条": row["当日唯一链接首条"] ? 1 : 0,
}));
const detailInfo = writeObjectTable(
  details,
  1,
  1,
  detailRowsForWorkbook,
  detailHeaders,
  { tableName: "SourceDetailTable", tableStyle: "TableStyleMedium2" },
);
details.freezePanes.freezeRows(1);
details.freezePanes.freezeColumns(4);
details.getRange(`A2:A${detailInfo.endRow}`).format.numberFormat = "yyyy-mm-dd";
details.getRange(`E2:F${detailInfo.endRow}`).format.numberFormat = "0";
details.getRange(`O2:P${detailInfo.endRow}`).format.horizontalAlignment = "center";
details.getRange(`V2:V${detailInfo.endRow}`).format.horizontalAlignment = "center";
details.getRange(`K2:U${detailInfo.endRow}`).format.wrapText = true;
setWidths(details, {
  A: 12, B: 13, C: 18, D: 18, E: 9, F: 9, G: 10, H: 10, I: 14, J: 22,
  K: 48, L: 55, M: 55, N: 12, O: 10, P: 10, Q: 14, R: 10, S: 19, T: 32, U: 26, V: 14,
});

// 每日占比：数量字段用 COUNTIFS 从明细表计算，占比与日变动继续用公式。
const sortedDaily = [...payload.daily].sort(
  (a, b) => a["品类"].localeCompare(b["品类"], "zh-CN") || a["日期"].localeCompare(b["日期"]),
);
const dailyHeaders = [
  "日期", "品类", "品类全部信源行", "自有产品命中行（分母）", "文章行", "视频行", "其他行",
  "文章占比", "视频占比", "其他占比", "唯一链接数", "唯一文章链接", "唯一视频链接",
  "唯一文章占比", "唯一视频占比", "文章占比日变动", "视频占比日变动",
  "标题命中行", "正文补充命中行", "标题+正文双命中行",
];
daily.getRange(`A1:T${sortedDaily.length + 1}`).values = [
  dailyHeaders,
  ...sortedDaily.map((row) => [
    row["日期"], row["品类"], row["品类全部信源行"], null, null, null, null,
    null, null, null, null, null, null, null, null, null, null, null, null, null,
  ]),
];
daily.getRange("A1:T1").format = {
  fill: colors.navy,
  font: { bold: true, color: colors.white },
  wrapText: true,
  borders: { preset: "all", style: "thin", color: colors.gray300 },
};
const detailEnd = detailInfo.endRow;
for (let i = 0; i < sortedDaily.length; i += 1) {
  const row = i + 2;
  daily.getRange(`D${row}:T${row}`).formulas = [[
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row})`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$G$2:$G$${detailEnd},"文章")`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$G$2:$G$${detailEnd},"视频")`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$G$2:$G$${detailEnd},"其他")`,
    `=IFERROR(E${row}/D${row},"")`,
    `=IFERROR(F${row}/D${row},"")`,
    `=IFERROR(G${row}/D${row},"")`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$V$2:$V$${detailEnd},1)`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$V$2:$V$${detailEnd},1,'信源明细'!$G$2:$G$${detailEnd},"文章")`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$V$2:$V$${detailEnd},1,'信源明细'!$G$2:$G$${detailEnd},"视频")`,
    `=IFERROR(L${row}/K${row},"")`,
    `=IFERROR(M${row}/K${row},"")`,
    i > 0
      ? `=IF(B${row}=B${row - 1},H${row}-H${row - 1},"")`
      : '=""',
    i > 0
      ? `=IF(B${row}=B${row - 1},I${row}-I${row - 1},"")`
      : '=""',
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$N$2:$N$${detailEnd},"标题")+COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$N$2:$N$${detailEnd},"标题+正文")`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$N$2:$N$${detailEnd},"正文")`,
    `=COUNTIFS('信源明细'!$A$2:$A$${detailEnd},A${row},'信源明细'!$B$2:$B$${detailEnd},B${row},'信源明细'!$N$2:$N$${detailEnd},"标题+正文")`,
  ]];
}
const dailyBody = daily.getRange(`A2:T${sortedDaily.length + 1}`);
dailyBody.format = {
  borders: { preset: "all", style: "thin", color: colors.gray300 },
  verticalAlignment: "center",
};
daily.getRange(`H2:J${sortedDaily.length + 1}`).format.numberFormat = "0.00%";
daily.getRange(`N2:Q${sortedDaily.length + 1}`).format.numberFormat = "0.00%";
daily.getRange(`P2:Q${sortedDaily.length + 1}`).conditionalFormats.add("colorScale", {
  colors: [colors.red, colors.white, colors.green],
  thresholds: ["min", "50%", "max"],
});
daily.tables.add(`A1:T${sortedDaily.length + 1}`, true, "DailyMixTable").style = "TableStyleMedium2";
daily.freezePanes.freezeRows(1);
daily.freezePanes.freezeColumns(2);
setWidths(daily, {
  A: 12, B: 14, C: 15, D: 18, E: 10, F: 10, G: 10, H: 12, I: 12, J: 12,
  K: 13, L: 14, M: 14, N: 14, O: 14, P: 15, Q: 15, R: 13, S: 16, T: 18,
});

// 标题关键词：TF-IDF 与文章/视频主题差异。
formatTitle(
  keywords,
  "标题关键词数学分析",
  "TF-IDF：衡量标题词对当前品类×信源类型的代表性；主题差异用双比例 z 检验与 0.5 连续性修正的对数优势。",
  "V",
);
keywords.getRange("A5:I5").merge();
keywords.getRange("A5").values = [["TF-IDF关键词（重复引用加权）"]];
keywords.getRange("A5:I5").format = {
  fill: colors.teal,
  font: { bold: true, color: colors.white, size: 12 },
};
const keywordHeaders = [
  "品类", "信源类型", "排名", "关键词", "TF-IDF均值", "标题覆盖数", "标题覆盖率", "总词频", "标题样本数",
];
const keywordInfo = writeObjectTable(
  keywords,
  6,
  1,
  payload.tfidf,
  keywordHeaders,
  { tableName: "KeywordTfidfTable", tableStyle: "TableStyleMedium4" },
);
keywords.getRange(`E7:E${keywordInfo.endRow}`).format.numberFormat = "0.0000";
keywords.getRange(`G7:G${keywordInfo.endRow}`).format.numberFormat = "0.00%";

keywords.getRange("K5:V5").merge();
keywords.getRange("K5").values = [["文章 vs 视频：标题主题差异"]];
keywords.getRange("K5:V5").format = {
  fill: colors.blue,
  font: { bold: true, color: colors.white, size: 12 },
};
const themeHeaders = [
  "品类", "主题", "文章命中标题", "文章标题数", "文章命中率", "视频命中标题",
  "视频标题数", "视频命中率", "视频-文章差", "平滑对数优势（视频相对文章）",
  "p值", "显著性",
];
const themeInfo = writeObjectTable(
  keywords,
  6,
  11,
  payload.themes,
  themeHeaders,
  { tableName: "ThemeDifferenceTable", tableStyle: "TableStyleMedium9", headerFill: colors.blue },
);
keywords.getRange(`O7:O${themeInfo.endRow}`).format.numberFormat = "0.00%";
keywords.getRange(`R7:S${themeInfo.endRow}`).format.numberFormat = "0.00%";
keywords.getRange(`T7:U${themeInfo.endRow}`).format.numberFormat = "0.000";
keywords.getRange(`V7:V${themeInfo.endRow}`).conditionalFormats.add("containsText", {
  text: "显著",
  format: { fill: colors.tealLight, font: { color: colors.teal, bold: true } },
});
keywords.freezePanes.freezeRows(6);
setWidths(keywords, {
  A: 14, B: 10, C: 8, D: 20, E: 14, F: 13, G: 13, H: 11, I: 12,
  J: 3, K: 14, L: 16, M: 14, N: 12, O: 13, P: 14, Q: 12, R: 13,
  S: 14, T: 20, U: 12, V: 11,
});

// 日间关键词变化。
formatTitle(
  shifts,
  "标题词分布的日间变化",
  "Jensen–Shannon 散度范围 0–1：越接近 1，表示相邻观测日标题词分布变化越剧烈。",
  "H",
);
const jsHeaders = ["品类", "前一日", "当日", "Jensen-Shannon散度", "上升关键词", "下降关键词"];
const jsInfo = writeObjectTable(
  shifts,
  5,
  1,
  payload.js_changes,
  jsHeaders,
  { tableName: "JsChangeTable", tableStyle: "TableStyleMedium4" },
);
shifts.getRange(`D6:D${jsInfo.endRow}`).format.numberFormat = "0.000";
shifts.getRange(`D6:D${jsInfo.endRow}`).conditionalFormats.add("colorScale", {
  colors: [colors.tealLight, colors.orangeLight, "#F5B7B1"],
  thresholds: ["min", "50%", "max"],
});
shifts.getRange(`E6:F${jsInfo.endRow}`).format.wrapText = true;
const dailyThemeStart = jsInfo.endRow + 3;
shifts.getRange(`A${dailyThemeStart}:F${dailyThemeStart}`).merge();
shifts.getRange(`A${dailyThemeStart}`).values = [["每日标题主题覆盖率"]];
shifts.getRange(`A${dailyThemeStart}:F${dailyThemeStart}`).format = {
  fill: colors.teal,
  font: { bold: true, color: colors.white },
};
const dailyThemeHeaders = ["日期", "品类", "主题", "主题标题数", "自有产品命中标题数", "主题覆盖率"];
const dailyThemeInfo = writeObjectTable(
  shifts,
  dailyThemeStart + 1,
  1,
  payload.daily_themes,
  dailyThemeHeaders,
  { tableName: "DailyThemeTable", tableStyle: "TableStyleMedium2" },
);
shifts.getRange(`F${dailyThemeStart + 2}:F${dailyThemeInfo.endRow}`).format.numberFormat = "0.00%";
shifts.freezePanes.freezeRows(5);
setWidths(shifts, { A: 14, B: 13, C: 13, D: 22, E: 52, F: 52, G: 3, H: 3 });

// 数据质量。
formatTitle(
  quality,
  "数据质量与可解释边界",
  "正文未可靠归档的链接不能当作“未提及自有产品”；因此本报告的分母是“已确认命中”，属于可审计的保守下界。",
  "O",
);
const qualityHeaders = [
  "品类", "品类全部信源行", "确认自有产品命中行", "文章命中行", "视频命中行",
  "正文已归档行", "正文未可靠归档行", "未验证潜在漏计行", "正文补充命中行",
  "确认命中率（对全部信源行）", "正文补充贡献率（对确认命中）", "正文可靠归档率",
];
const qualityInfo = writeObjectTable(
  quality,
  5,
  1,
  payload.quality,
  qualityHeaders,
  { tableName: "QualityTable", tableStyle: "TableStyleMedium3" },
);
quality.getRange(`J6:L${qualityInfo.endRow}`).format.numberFormat = "0.00%";
quality.getRange("A10:O10").merge();
quality.getRange("A10").values = [["如何阅读质量指标"]];
quality.getRange("A10:O10").format = {
  fill: colors.orange,
  font: { bold: true, color: colors.white },
};
quality.getRange("A11:O16").merge();
quality.getRange("A11").values = [[
  "1）确认命中行：标题命中，或正文已成功归档且命中自有产品；这是占比的分母。\n" +
  "2）正文补充命中行：标题没有品牌/产品名，但正文里有；如果只看标题会漏掉。\n" +
  "3）未验证潜在漏计行：标题未命中且正文没有可靠归档，不能判断正文是否提及。它们不进入分母，也不能解释为“未提及”。\n" +
  "4）正文索引是当前抓取快照；历史页面若后续被改写，正文命中可能反映当前版本。",
]];
quality.getRange("A11:O16").format = {
  fill: colors.orangeLight,
  font: { color: colors.gray900 },
  wrapText: true,
  verticalAlignment: "top",
  borders: { preset: "outside", style: "medium", color: colors.orange },
};
setWidths(quality, {
  A: 15, B: 16, C: 18, D: 13, E: 13, F: 14, G: 18, H: 18, I: 17,
  J: 20, K: 22, L: 17, M: 3, N: 3, O: 3,
});

// 方法说明。
formatTitle(
  method,
  "口径与数学方法",
  `生成时间：${payload.generated_at}；问题范围：${payload.scope.questions.join("、")}`,
  "H",
);
const methodRows = [
  ["分析对象", "梵玢眉毛精华液、梵玢睫毛精华液；只分析对应推荐类问题，排除“依斯佩尔评价”等不同问题意图。"],
  ["主分母", payload.scope.primary_denominator],
  ["校验分母", payload.scope.secondary_denominator],
  ["文章/视频判定", "优先使用信源识别缓存；抖音、B站、快手、YouTube 等视频域名兜底归为视频，其余已识别文章归为文章。"],
  ["正文命中", "正文状态为 ok 且质量为 high/medium，并通过“品牌+产品描述近距离共现”规则；正文未归档不按未提及处理。"],
  ["重复口径", "同一链接跨轮次重复出现会重复计数，反映豆包的实际重复提取强度。"],
  ["唯一链接口径", "同品类、同日期按去追踪参数后的标准化 URL 去重，防止单一高频链接支配结论。"],
  ["TF-IDF", "对标题分词后，计算每个品类×文章/视频组内的平均 TF-IDF；自有品牌、品类通用词及媒体站名被列为停用词。"],
  ["主题差异", "用标题主题词典统计文章/视频命中率；双比例 z 检验判断差异，p<0.05 标为显著；平滑对数优势>0 表示视频更偏好。"],
  ["日间剧烈变化", "相邻观测日标题词分布计算 Jensen–Shannon 散度（0–1），越大说明标题结构变化越剧烈。"],
  ["解释限制", "结果是监控样本的描述与关联，不直接等于因果；日期是观测日，不连续的自然日之间仍按相邻观测日比较。"],
];
method.getRange(`A5:B${methodRows.length + 4}`).values = methodRows;
method.getRange(`A5:A${methodRows.length + 4}`).format = {
  fill: colors.tealLight,
  font: { bold: true, color: colors.teal },
  borders: { preset: "all", style: "thin", color: colors.gray300 },
};
method.getRange(`B5:B${methodRows.length + 4}`).format = {
  wrapText: true,
  verticalAlignment: "top",
  borders: { preset: "all", style: "thin", color: colors.gray300 },
};
method.getRange(`A5:B${methodRows.length + 4}`).format.rowHeight = 42;
setWidths(method, { A: 20, B: 100, C: 3, D: 3, E: 3, F: 3, G: 3, H: 3 });

// 分析报告与公式驱动图表。
formatTitle(
  report,
  "眉毛 / 睫毛自有产品信源结构与标题关键词报告",
  "主口径保留重复引用，分母为标题或可靠正文确认命中自有产品的信源行；唯一链接口径用于检查重复链接放大效应。",
  "P",
);
const categories = ["眉毛精华液", "睫毛精华液"];
const dailyRowMap = new Map();
sortedDaily.forEach((row, index) => dailyRowMap.set(`${row["品类"]}|${row["日期"]}`, index + 2));
const latestRows = Object.fromEntries(
  categories.map((category) => {
    const matches = sortedDaily
      .map((row, index) => ({ row, excelRow: index + 2 }))
      .filter((item) => item.row["品类"] === category);
    return [category, matches[matches.length - 1]];
  }),
);

report.getRange("A5:D5").merge();
report.getRange("E5:H5").merge();
report.getRange("I5:L5").merge();
report.getRange("M5:P5").merge();
report.getRange("A5").values = [["眉毛精华液 · 最新视频占比"]];
report.getRange("E5").values = [["睫毛精华液 · 最新视频占比"]];
report.getRange("I5").values = [["正文补充命中贡献"]];
report.getRange("M5").values = [["正文可靠归档率"]];
report.getRange("A6:D8").merge();
report.getRange("E6:H8").merge();
report.getRange("I6:L8").merge();
report.getRange("M6:P8").merge();
report.getRange("A6").formulas = [[`='每日占比'!I${latestRows["眉毛精华液"].excelRow}`]];
report.getRange("E6").formulas = [[`='每日占比'!I${latestRows["睫毛精华液"].excelRow}`]];
report.getRange("I6").formulas = [[
  `=SUM('每日占比'!S2:S${sortedDaily.length + 1})/SUM('每日占比'!D2:D${sortedDaily.length + 1})`,
]];
report.getRange("M6").formulas = [[`=AVERAGE('数据质量'!L6:L${qualityInfo.endRow})`]];
for (const addr of ["A6:D8", "E6:H8", "I6:L8", "M6:P8"]) {
  report.getRange(addr).format = {
    font: { bold: true, color: colors.gray900, size: 22 },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    numberFormat: "0.0%",
  };
}
for (const addr of ["A5:D8", "E5:H8", "I5:L8", "M5:P8"]) {
  styleCard(report, addr, colors.gray100);
}
report.getRange("A5:P5").format.font = { bold: true, color: colors.gray700, size: 10 };
report.getRange("A5:P5").format.horizontalAlignment = "center";

const browDates = sortedDaily
  .filter((row) => row["品类"] === "眉毛精华液")
  .map((row) => row["日期"]);
report.getRange(`R1:V${browDates.length + 1}`).values = [
  ["日期", "重复文章", "重复视频", "唯一文章", "唯一视频"],
  ...browDates.map((day) => [day, null, null, null, null]),
];
for (let i = 0; i < browDates.length; i += 1) {
  const row = i + 2;
  const day = browDates[i];
  const brow = dailyRowMap.get(`眉毛精华液|${day}`);
  report.getRange(`S${row}:V${row}`).formulas = [[
    `='每日占比'!H${brow}`,
    `='每日占比'!I${brow}`,
    `='每日占比'!N${brow}`,
    `='每日占比'!O${brow}`,
  ]];
}
report.getRange(`S2:V${browDates.length + 1}`).format.numberFormat = "0%";
const trendChart = report.charts.add("line", report.getRange(`R1:V${browDates.length + 1}`));
trendChart.title = "眉毛精华液：文章 / 视频每日占比";
trendChart.hasLegend = true;
trendChart.xAxis = { axisType: "textAxis", textStyle: { fontSize: 9 } };
trendChart.yAxis = { numberFormatCode: "0%", min: 0, max: 1 };
trendChart.setPosition("A10", "H28");

const lashDates = sortedDaily
  .filter((row) => row["品类"] === "睫毛精华液")
  .map((row) => row["日期"]);
report.getRange(`X1:AB${lashDates.length + 1}`).values = [
  ["日期", "重复文章", "重复视频", "唯一文章", "唯一视频"],
  ...lashDates.map((day) => [day, null, null, null, null]),
];
for (let i = 0; i < lashDates.length; i += 1) {
  const row = i + 2;
  const day = lashDates[i];
  const lash = dailyRowMap.get(`睫毛精华液|${day}`);
  report.getRange(`Y${row}:AB${row}`).formulas = [[
    `='每日占比'!H${lash}`,
    `='每日占比'!I${lash}`,
    `='每日占比'!N${lash}`,
    `='每日占比'!O${lash}`,
  ]];
}
report.getRange(`Y2:AB${lashDates.length + 1}`).format.numberFormat = "0%";
const uniqueChart = report.charts.add("line", report.getRange(`X1:AB${lashDates.length + 1}`));
uniqueChart.title = "睫毛精华液：文章 / 视频每日占比";
uniqueChart.hasLegend = true;
uniqueChart.xAxis = { axisType: "textAxis", textStyle: { fontSize: 9 } };
uniqueChart.yAxis = { numberFormatCode: "0%", min: 0, max: 1 };
uniqueChart.setPosition("I10", "P28");

const themeOrder = Object.keys({
  "榜单/推荐": 1, "测评/实测": 1, "安全/温和": 1, "功效/增长": 1, "成分/科学": 1,
  "教程/周期": 1, "避雷/风险": 1, "价格/性价比": 1, "年份/新鲜度": 1, "痛点场景": 1,
});
let helperCol = 30; // AD
for (let c = 0; c < categories.length; c += 1) {
  const category = categories[c];
  const startCol = helperCol + c * 4;
  const startLetter = colName(startCol);
  const endLetter = colName(startCol + 2);
  const data = themeOrder.map((theme) => {
    const row = payload.themes.find((item) => item["品类"] === category && item["主题"] === theme);
    return [theme, row?.["文章命中率"] ?? 0, row?.["视频命中率"] ?? 0];
  });
  report.getRange(`${startLetter}1:${endLetter}${data.length + 1}`).values = [
    ["主题", "文章", "视频"],
    ...data,
  ];
  report.getRange(`${colName(startCol + 1)}2:${endLetter}${data.length + 1}`).format.numberFormat = "0%";
  const chart = report.charts.add(
    "bar",
    report.getRange(`${startLetter}1:${endLetter}${data.length + 1}`),
  );
  chart.title = `${category}：标题主题覆盖率`;
  chart.hasLegend = true;
  chart.xAxis = { axisType: "textAxis", textStyle: { fontSize: 9 } };
  chart.yAxis = { numberFormatCode: "0%", min: 0, max: 1 };
  chart.setPosition(c === 0 ? "A30" : "I30", c === 0 ? "H49" : "P49");
}

report.getRange("A51:P51").merge();
report.getRange("A51").values = [["关键结论与可执行建议"]];
report.getRange("A51:P51").format = {
  fill: colors.teal,
  font: { bold: true, color: colors.white, size: 12 },
};
const conclusionLines = payload.conclusions.map((item) => (
  `${item["品类"]}：累计文章 ${Number(item["文章总占比"]).toLocaleString("zh-CN", { style: "percent", maximumFractionDigits: 1 })}、` +
  `视频 ${Number(item["视频总占比"]).toLocaleString("zh-CN", { style: "percent", maximumFractionDigits: 1 })}；` +
  `最新日视频 ${Number(item["最新视频占比"]).toLocaleString("zh-CN", { style: "percent", maximumFractionDigits: 1 })}。` +
  `视频标题更偏“${item["视频更偏好的标题主题"]}”，文章更偏“${item["文章更偏好的标题主题"]}”。` +
  `关键词结构变化最剧烈在 ${item["标题关键词变化最剧烈日期"]}（JSD ${Number(item["最大Jensen-Shannon散度"]).toFixed(3)}）。`
));
const recommendationText =
  `${conclusionLines.join("\n")}\n\n` +
  "策略建议：视频标题优先采用清晰功效结果、榜单/好物语气和具体痛点场景；文章标题强化年份、测评、成分与安全证据。任何结论都要同时看“重复引用”和“唯一链接”两张趋势图：若两者方向不一致，说明变化主要由少数高频链接驱动。";
report.getRange("A52:P61").merge();
report.getRange("A52").values = [[recommendationText]];
report.getRange("A52:P61").format = {
  fill: colors.gray100,
  font: { color: colors.gray900, size: 11 },
  wrapText: true,
  verticalAlignment: "top",
  borders: { preset: "outside", style: "medium", color: colors.gray300 },
};
setWidths(report, {
  A: 12, B: 12, C: 12, D: 12, E: 12, F: 12, G: 12, H: 12,
  I: 12, J: 12, K: 12, L: 12, M: 12, N: 12, O: 12, P: 12,
  Q: 3, R: 12, S: 12, T: 12, U: 12, V: 12, W: 3, X: 12, Y: 12, Z: 12,
  AA: 12, AB: 12,
});
report.freezePanes.freezeRows(3);

// 通用格式。
for (const sheet of [daily, details, keywords, shifts, quality, method]) {
  sheet.showGridLines = false;
}

await fs.mkdir(outputDir, { recursive: true });
const preview = await wb.render({
  sheetName: "分析报告",
  range: "A1:P61",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  path.join(outputDir, "preview_report.png"),
  new Uint8Array(await preview.arrayBuffer()),
);
const dailyPreview = await wb.render({
  sheetName: "每日占比",
  range: `A1:T${sortedDaily.length + 1}`,
  scale: 0.8,
  format: "png",
});
await fs.writeFile(
  path.join(outputDir, "preview_daily.png"),
  new Uint8Array(await dailyPreview.arrayBuffer()),
);

const inspect = await wb.inspect({
  kind: "workbook,sheet,table",
  maxChars: 12000,
  tableMaxRows: 6,
  tableMaxCols: 12,
  tableMaxCellChars: 120,
});
await fs.writeFile(path.join(outputDir, "workbook_inspect.json"), inspect.ndjson || JSON.stringify(inspect, null, 2));

const formulaErrors = await wb.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 200 },
  maxChars: 12000,
});
await fs.writeFile(
  path.join(outputDir, "formula_error_scan.json"),
  formulaErrors.ndjson || JSON.stringify(formulaErrors, null, 2),
);

const output = await SpreadsheetFile.exportXlsx(wb);
await output.save(outputPath);
console.log(JSON.stringify({
  outputPath,
  sheets: ["分析报告", "每日占比", "信源明细", "标题关键词", "日间关键词变化", "数据质量", "方法说明"],
  detailRows: payload.details.length,
  dailyRows: sortedDaily.length,
  preview: path.join(outputDir, "preview_report.png"),
}, null, 2));
