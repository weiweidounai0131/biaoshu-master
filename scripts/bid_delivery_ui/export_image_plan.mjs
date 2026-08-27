#!/usr/bin/env node
/* Create exactly one image-planning workbook from a confirmed local JSON plan.
 * This export is a prompt brief for a separate image-generation AI only; it
 * deliberately contains no generated images, asset paths, or insertion state.
 */

import fs from "node:fs/promises";
import path from "node:path";
import { Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const [sourcePath, outputPath, previewPath, resultPath] = process.argv.slice(2);
if (!sourcePath || !outputPath) {
  throw new Error("usage: export_image_plan.mjs <source.json> <output.xlsx> [preview.png]");
}

const source = JSON.parse(await fs.readFile(sourcePath, "utf8"));
const workbook = Workbook.create();
const sheet = workbook.worksheets.add("图片规划清单");
const headers = [
  "图号", "一级章节", "精确放置位置", "具体放置说明", "图片名称", "图片类型",
  "用途/核心表达", "核心节点", "构图建议", "画面方向", "是否章首总览图",
  "统一视觉要求", "应避免的风格", "生图补充说明", "AI生图提示词",
];
const visual = source.visual_direction || {};
const visualValue = (english, chinese) => visual[english] || visual[chinese];
const visualText = [visualValue("palette", "主色"), visualValue("style", "风格"), visualValue("background", "背景"), visualValue("density", "信息密度")].filter(Boolean).join("；");
const avoidValue = visualValue("avoid", "应避免");
const avoidText = Array.isArray(avoidValue) ? avoidValue.join("、") : String(avoidValue || "");
const text = (value) => String(value ?? "").trim();
const orientationText = { landscape: "横向", portrait: "纵向", square: "方形", auto: "自适应" };
const composePrompt = (image) => {
  const position = image.position || {};
  const nodes = Array.isArray(image.core_nodes) && image.core_nodes.length
    ? image.core_nodes.filter(Boolean).join("、")
    : "按已确认的核心表达组织信息";
  const chapter = `${text(image.chapter_number)} ${text(image.chapter_title)}`.trim();
  const outline = `${text(position.outline_number)} ${text(position.outline_title)}`.trim();
  const direction = orientationText[image.orientation] || text(image.orientation) || "自适应";
  const parts = [
    `生成一张中文${text(image.type) || "信息图"}，图号为“${text(image.figure_no)}”，图片名称为“${text(image.name)}”。`,
    `它用于${chapter}，放置在“${outline}”位置；具体放置说明：${text(position.placement_note)}。`,
    `核心表达：${text(image.purpose)}。画面只围绕以下已确认核心节点组织：${nodes}。`,
    `构图要求：${text(image.composition)}。画面方向为${direction}，层级、连线和阅读顺序应清晰，重点信息优先可读。`,
  ];
  if (visualText) parts.push(`统一视觉方向：${visualText}。`);
  if (avoidText) parts.push(`应避免的风格或限制：${avoidText}。`);
  parts.push("图中文字只使用规划中已经出现或由规划字段明确要求的中文，不添加未经确认的客户名称、人员姓名、证书、业绩、数据、日期、Logo、品牌标识或承诺；不出现英文、乱码、水印、虚构事实、无关装饰和与正文无关的内容。");
  return parts.join("");
};
const rows = source.images.map((image) => [
  image.figure_no,
  `${image.chapter_number} ${image.chapter_title}`,
  `${image.position.outline_number} ${image.position.outline_title}`,
  image.position.placement_note,
  image.name,
  image.type,
  image.purpose,
  Array.isArray(image.core_nodes) ? image.core_nodes.join("；") : "",
  image.composition,
  image.orientation === "landscape" ? "横向" : image.orientation === "portrait" ? "纵向" : image.orientation,
  image.is_chapter_overview ? "是" : "否",
  visualText,
  avoidText,
  "本表仅定义图片内容、构图要求和逐图生图提示词；最终交付后仅在用户明确回复“继续”时进入可选生图流程。",
  text(image.ai_prompt) || composePrompt(image),
]);

sheet.mergeCells("A1:O1");
sheet.getRange("A1").values = [["图片规划表（含逐图AI生图提示词）"]];
sheet.getRange("A1").format = {
  fill: "#C91F37", font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center", verticalAlignment: "center",
};
sheet.getRange("A1:O1").format.rowHeight = 30;
sheet.mergeCells("A2:O2");
sheet.getRange("A2").values = [["本表包含逐图 AI 生图提示词；最终交付后只有用户明确回复“继续”才进入可选生图流程，默认不生成图片。"]];
sheet.getRange("A2").format = {
  fill: "#FFF5E6", font: { color: "#7A4B00", italic: true }, wrapText: true,
  horizontalAlignment: "left", verticalAlignment: "center",
};
sheet.getRange("A2:O2").format.rowHeight = 34;
sheet.getRange(`A4:O4`).values = [headers];
sheet.getRange("A4:O4").format = {
  fill: "#1E5EAA", font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
};
if (rows.length) {
  sheet.getRange(`A5:O${4 + rows.length}`).values = rows;
  sheet.getRange(`A5:O${4 + rows.length}`).format = {
    verticalAlignment: "center", wrapText: true,
  };
}
const widths = [12, 20, 24, 30, 22, 16, 28, 30, 36, 12, 16, 36, 24, 38, 72];
for (let index = 0; index < widths.length; index += 1) {
  sheet.getRangeByIndexes(0, index, Math.max(rows.length + 4, 5), 1).format.columnWidth = widths[index];
}
sheet.getRange(`A4:O${Math.max(4 + rows.length, 4)}`).format.borders = {
  top: { style: "continuous", color: "#D9E0EA" }, bottom: { style: "continuous", color: "#D9E0EA" },
  left: { style: "continuous", color: "#D9E0EA" }, right: { style: "continuous", color: "#D9E0EA" },
  insideHorizontal: { style: "continuous", color: "#D9E0EA" }, insideVertical: { style: "continuous", color: "#D9E0EA" },
};
sheet.freezePanes.freezeRows(4);
if (rows.length) sheet.tables.add(`A4:O${4 + rows.length}`, true, "ImagePlan");

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
const inspect = await workbook.inspect({ kind: "sheet,table", maxChars: 2000, tableMaxRows: 4, tableMaxCols: 6 });
if (previewPath) {
  const preview = await workbook.render({ sheetName: "图片规划清单", autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
}
const result = JSON.stringify({ row_count: rows.length, worksheet_count: 1, inspection: inspect.ndjson || String(inspect) });
if (resultPath) await fs.writeFile(resultPath, result, "utf8");
process.stdout.write(result + "\n");
