const state = {
  sourceHash: "",
  recommendation: null,
  defaults: null,
  workflow: null,
  scoringExtraction: null,
  readOnly: false,
  intake: { sourceHash: "", backgroundPaths: [], referencePaths: [], position: "main", locked: false, pollTimer: null },
  stageWait: { pollTimer: null, startedAt: 0 },
};

const $ = (selector) => document.querySelector(selector);
const form = $("#stage1-form");
const intakeForm = $("#intake-form");

function getPath(object, path) {
  return path.split(".").reduce((value, key) => value && value[key] !== undefined ? value[key] : "", object);
}

function setPath(object, path, value) {
  const keys = path.split(".");
  let cursor = object;
  keys.forEach((key, index) => {
    if (index === keys.length - 1) cursor[key] = value;
    else cursor = cursor[key] ??= {};
  });
}

function display(value) {
  return Array.isArray(value) ? value.join("\n") : value ?? "";
}

function normalize(name, value) {
  if (["formatting.target_pages", "formatting.max_heading_level"].includes(name)) return Number(value);
  return value;
}

function fill(data) {
  form.querySelectorAll("[name]").forEach((element) => {
    element.value = display(getPath(data, element.name));
  });
}

function collect() {
  const data = {};
  form.querySelectorAll("[name]").forEach((element) => {
    setPath(data, element.name, normalize(element.name, element.value.trim()));
  });
  data.scoring_extraction = structuredClone(state.scoringExtraction || {});
  data.scoring_extraction.reviewed = $("#scoring-reviewed").checked;
  return data;
}

function scoreText(value) {
  return String(value ?? "");
}

function appendHighlightedText(container, value, points, field) {
  const text = scoreText(value);
  const ranges = [];
  (Array.isArray(points) ? points : []).forEach((point) => {
    (Array.isArray(point?.source_quotes) ? point.source_quotes : []).forEach((quote) => {
      const needle = scoreText(quote?.text);
      if (quote?.field !== field || needle.trim().length < 2) return;
      let start = 0;
      while ((start = text.indexOf(needle, start)) >= 0) {
        ranges.push([start, start + needle.length]);
        start += needle.length;
      }
    });
  });
  ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [];
  ranges.forEach(([start, end]) => {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  });
  let cursor = 0;
  merged.forEach(([start, end]) => {
    if (start > cursor) container.append(document.createTextNode(text.slice(cursor, start)));
    const mark = document.createElement("mark");
    mark.className = "score-source-highlight";
    mark.textContent = text.slice(start, end);
    container.append(mark);
    cursor = end;
  });
  if (cursor < text.length) container.append(document.createTextNode(text.slice(cursor)));
  if (!text) container.textContent = "（评分表此栏为空）";
}

function scoringExtractionIssues() {
  const extraction = state.scoringExtraction;
  if (!extraction || extraction.status === "pending") return ["等待AI完成必需二级标题提取"];
  if (extraction.schema_version !== 1) return ["标题提取版本无效，请让AI重新生成"];
  const criteria = Array.isArray(extraction.criteria) ? extraction.criteria : [];
  if (extraction.status === "no_rubric") {
    if (criteria.length || Number(extraction.source_criterion_count) !== 0) return ["无评分表状态与评分标准列表不一致"];
    if (!String(extraction.no_rubric_reason || "").trim()) return ["请补充未识别到评分表的原因"];
  } else if (extraction.status !== "ready") {
    return ["评分表读取结果未完成，请让AI读取评分表后刷新页面"];
  } else if (!criteria.length) {
    return ["评分表提取结果为空"];
  }
  if (Number(extraction.source_criterion_count) !== criteria.length) {
    return [`评分表识别为 ${extraction.source_criterion_count ?? "—"} 条标准，当前列表有 ${criteria.length} 条`];
  }
  const ids = new Set();
  for (let ci = 0; ci < criteria.length; ci += 1) {
    const criterion = criteria[ci] || {};
    if (!String(criterion.criterion_id || "").trim() || !String(criterion.factor_id || "").trim() || !String(criterion.factor || "").trim()) return [`第 ${ci + 1} 条缺少评分因素或标准编号`];
    if (!String(criterion.requirement_text || "").trim() && !String(criterion.scoring_rule || "").trim()) return [`第 ${ci + 1} 条评分标准原文为空`];
    if (criterion.in_scope === false && !String(criterion.exclusion_reason || "").trim()) return [`第 ${ci + 1} 条未纳入框架，请写明原因`];
    const points = Array.isArray(criterion.points) ? criterion.points : [];
    if (!points.length) return [`第 ${ci + 1} 条还没有提取必须优先出现的二级标题`];
    for (let pi = 0; pi < points.length; pi += 1) {
      const point = points[pi] || {};
      if (!String(point.point_id || "").trim() || ids.has(point.point_id)) return [`第 ${ci + 1} 条的必需标题编号缺失或重复`];
      ids.add(point.point_id);
      if (!String(point.title || "").trim()) return [`第 ${ci + 1} 条的第 ${pi + 1} 个必需标题为空`];
      if (point.order !== pi + 1) return [`“${point.title}”的标题顺序无效`];
      const quotes = Array.isArray(point.source_quotes) ? point.source_quotes : [];
      if (!quotes.length) return [`“${point.title}”没有标出评分表原文`];
      for (const quote of quotes) {
        const source = quote?.field === "factor" ? criterion.factor : quote?.field === "requirement_text" ? criterion.requirement_text : quote?.field === "scoring_rule" ? criterion.scoring_rule : null;
        if (!source || String(quote?.text || "").trim().length < 2 || !String(source).includes(String(quote.text))) return [`“${point.title}”的原文标记没有匹配到对应评分标准`];
      }
    }
  }
  if (!$("#scoring-reviewed").checked) return ["请核对评分表并勾选确认"];
  return [];
}

function updateScoringReviewState() {
  const extraction = state.scoringExtraction || {};
  const criteria = Array.isArray(extraction.criteria) ? extraction.criteria : [];
  const points = criteria.flatMap((criterion) => Array.isArray(criterion.points) ? criterion.points : []);
  const included = criteria.filter((criterion) => criterion.in_scope !== false).reduce((count, criterion) => count + (Array.isArray(criterion.points) ? criterion.points.length : 0), 0);
  const omitted = Array.isArray(extraction.omitted_categories) ? extraction.omitted_categories : [];
  $("#scoring-review-counts").textContent = extraction.status === "no_rubric"
    ? "评分标准 0 条 · 必需二级标题 0 个"
    : `本轮核对 ${criteria.length} 条 · 必需二级标题 ${points.length} 个 · 纳入框架 ${included} 个`;
  const omittedNote = $("#scoring-omitted-note");
  omittedNote.hidden = omitted.length === 0;
  omittedNote.textContent = omitted.map((item) => `${item.category || "评分类别"}${item.criterion_count ? ` ${item.criterion_count} 条` : ""}${item.weight_total ? ` / ${item.weight_total} 分` : ""}已按要求移除`).join("；");
  const issues = scoringExtractionIssues();
  const status = $("#scoring-extraction-status");
  status.className = `scoring-extraction-status${issues.length ? " is-warning" : " is-ready"}`;
  status.textContent = issues[0] || "优先标题已核对，可以确认项目口径并生成框架。";
  const button = $("#confirm-button");
  if (button && !state.readOnly) button.disabled = issues.length > 0;
}

function renderScoringExtraction() {
  const extraction = state.scoringExtraction || {};
  const content = $("#scoring-extraction-content");
  content.replaceChildren();
  $("#scoring-reviewed").checked = extraction.reviewed === true;
  if (extraction.status === "pending" || !extraction.status) {
    const notice = document.createElement("p");
    notice.className = "scoring-empty-state";
    notice.textContent = "评分表标题提取尚未生成。请等待AI读取评分表并完成分析，再刷新本页。";
    content.append(notice);
    updateScoringReviewState();
    return;
  }
  if (extraction.status === "no_rubric") {
    const notice = document.createElement("p");
    notice.className = "scoring-empty-state";
    notice.textContent = `AI没有识别到评分表：${extraction.no_rubric_reason || "未填写原因"}`;
    content.append(notice);
    updateScoringReviewState();
    return;
  }

  const table = document.createElement("table");
  table.className = "scoring-review-table";
  const thead = document.createElement("thead");
  const header = document.createElement("tr");
  ["评分因素", "评分要求与计分规则", "必须优先出现的二级标题"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    header.append(cell);
  });
  thead.append(header);
  const tbody = document.createElement("tbody");
  const criteria = Array.isArray(extraction.criteria) ? extraction.criteria : [];
  criteria.forEach((criterion, ci) => {
    const points = Array.isArray(criterion.points) ? criterion.points : [];
    const row = document.createElement("tr");
    row.dataset.criterionIndex = String(ci);
    if (criterion.in_scope === false) row.classList.add("is-excluded");

    const factorCell = document.createElement("td");
    factorCell.className = "score-factor-cell";
    const factor = document.createElement("strong");
    appendHighlightedText(factor, criterion.factor, points, "factor");
    factorCell.append(factor);
    if (String(criterion.category || "").trim()) {
      const category = document.createElement("small");
      category.className = "score-category";
      category.textContent = String(criterion.category).replace(/[（(].*$/, "");
      factorCell.append(category);
    }
    if (criterion.weight !== undefined && criterion.weight !== null && String(criterion.weight).trim()) {
      const weight = document.createElement("small");
      weight.className = "score-weight";
      weight.textContent = `权重：${criterion.weight}`;
      factorCell.append(weight);
    }
    const scopeLabel = document.createElement("label");
    scopeLabel.className = "score-scope-toggle";
    const scope = document.createElement("input");
    scope.type = "checkbox";
    scope.checked = criterion.in_scope !== false;
    scope.dataset.scoreEdit = "in_scope";
    scope.dataset.ci = String(ci);
    const scopeText = document.createElement("span");
    scopeText.textContent = "纳入框架";
    scopeLabel.append(scope, scopeText);
    factorCell.append(scopeLabel);
    if (criterion.in_scope === false) {
      const reason = document.createElement("input");
      reason.type = "text";
      reason.className = "score-exclusion-reason";
      reason.placeholder = "说明不纳入本次框架的原因";
      reason.value = scoreText(criterion.exclusion_reason);
      reason.dataset.scoreEdit = "exclusion_reason";
      reason.dataset.ci = String(ci);
      factorCell.append(reason);
    }
    row.append(factorCell);

    const standardCell = document.createElement("td");
    standardCell.className = "score-standard-cell";
    const criterionLabel = document.createElement("small");
    criterionLabel.textContent = "评分要求";
    const criterionText = document.createElement("div");
    criterionText.className = "score-source-text";
    appendHighlightedText(criterionText, criterion.requirement_text, points, "requirement_text");
    standardCell.append(criterionLabel, criterionText);
    if (String(criterion.scoring_rule || "").trim()) {
      const ruleLabel = document.createElement("small");
      ruleLabel.className = "score-rule-label";
      ruleLabel.textContent = "评分规则";
      const ruleText = document.createElement("div");
      ruleText.className = "score-source-text score-rule-text";
      appendHighlightedText(ruleText, criterion.scoring_rule, points, "scoring_rule");
      standardCell.append(ruleLabel, ruleText);
    }
    row.append(standardCell);

    const pointsCell = document.createElement("td");
    pointsCell.className = "score-points-cell";
    points.forEach((point, pi) => {
      const editor = document.createElement("article");
      editor.className = "score-point-editor";
      const top = document.createElement("div");
      top.className = "score-point-top";
      const order = document.createElement("span");
      order.className = "score-point-order";
      order.textContent = String(pi + 1);
      const title = document.createElement("input");
      title.type = "text";
      title.value = scoreText(point.title);
      title.placeholder = "必须优先出现的二级标题";
      title.setAttribute("aria-label", "必须优先出现的二级标题");
      title.dataset.scoreEdit = "title";
      title.dataset.ci = String(ci);
      title.dataset.pi = String(pi);
      top.append(order, title);
      [["上移", "move-up"], ["下移", "move-down"], ["删除", "remove-point"]].forEach(([label, action]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.dataset.scoreAction = action;
        button.dataset.ci = String(ci);
        button.dataset.pi = String(pi);
        button.disabled = state.readOnly || (action === "move-up" && pi === 0) || (action === "move-down" && pi === points.length - 1);
        top.append(button);
      });
      const evidence = document.createElement("details");
      evidence.className = "score-point-evidence";
      const evidenceSummary = document.createElement("summary");
      const quoteCount = Array.isArray(point.source_quotes) ? point.source_quotes.length : 0;
      evidenceSummary.textContent = `原文标记 · ${quoteCount} 处`;
      evidence.append(evidenceSummary);
      const quotes = document.createElement("div");
      quotes.className = "score-point-quotes";
      (Array.isArray(point.source_quotes) ? point.source_quotes : []).forEach((quote, qi) => {
        const quoteRow = document.createElement("div");
        quoteRow.className = "score-quote-row";
        const field = document.createElement("select");
        field.dataset.scoreEdit = "quote_field";
        field.dataset.ci = String(ci);
        field.dataset.pi = String(pi);
        field.dataset.qi = String(qi);
        [["factor", "评分因素"], ["requirement_text", "评分要求"], ["scoring_rule", "计分规则"]].forEach(([value, label]) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = label;
          field.append(option);
        });
        field.value = ["factor", "requirement_text", "scoring_rule"].includes(quote.field) ? quote.field : "requirement_text";
        const quoteInput = document.createElement("input");
        quoteInput.type = "text";
        quoteInput.value = scoreText(quote.text);
        quoteInput.placeholder = "粘贴评分表中的原文短语";
        quoteInput.setAttribute("aria-label", "对应评分表原文");
        quoteInput.dataset.scoreEdit = "quote_text";
        quoteInput.dataset.ci = String(ci);
        quoteInput.dataset.pi = String(pi);
        quoteInput.dataset.qi = String(qi);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "移除原文";
        remove.dataset.scoreAction = "remove-quote";
        remove.dataset.ci = String(ci);
        remove.dataset.pi = String(pi);
        remove.dataset.qi = String(qi);
        remove.disabled = state.readOnly || (point.source_quotes || []).length <= 1;
        quoteRow.append(field, quoteInput, remove);
        quotes.append(quoteRow);
      });
      const addQuote = document.createElement("button");
      addQuote.type = "button";
      addQuote.className = "score-add-quote";
      addQuote.textContent = "＋ 添加原文标记";
      addQuote.dataset.scoreAction = "add-quote";
      addQuote.dataset.ci = String(ci);
      addQuote.dataset.pi = String(pi);
      addQuote.disabled = state.readOnly;
      quotes.append(addQuote);
      evidence.append(quotes);
      editor.append(top, evidence);
      pointsCell.append(editor);
    });
    const addPoint = document.createElement("button");
    addPoint.type = "button";
    addPoint.className = "score-add-point";
    addPoint.textContent = "＋ 添加必需标题";
    addPoint.dataset.scoreAction = "add-point";
    addPoint.dataset.ci = String(ci);
    addPoint.disabled = state.readOnly;
    pointsCell.append(addPoint);
    row.append(pointsCell);
    tbody.append(row);
  });
  table.append(thead, tbody);
  content.append(table);
  $("#scoring-reviewed").disabled = state.readOnly;
  updateScoringReviewState();
}

function markScoringChanged() {
  if (!state.scoringExtraction) return;
  state.scoringExtraction.reviewed = false;
  $("#scoring-reviewed").checked = false;
  setDirty();
  updateScoringReviewState();
}

$("#scoring-extraction-content").addEventListener("input", (event) => {
  const control = event.target.closest("[data-score-edit]");
  if (!control) return;
  const criterion = state.scoringExtraction?.criteria?.[Number(control.dataset.ci)];
  if (!criterion) return;
  const pi = Number(control.dataset.pi);
  if (control.dataset.scoreEdit === "title" && criterion.points?.[pi]) criterion.points[pi].title = control.value;
  if (control.dataset.scoreEdit === "quote_text" && criterion.points?.[pi]?.source_quotes?.[Number(control.dataset.qi)]) criterion.points[pi].source_quotes[Number(control.dataset.qi)].text = control.value;
  if (control.dataset.scoreEdit === "exclusion_reason") criterion.exclusion_reason = control.value;
  markScoringChanged();
});

$("#scoring-extraction-content").addEventListener("change", (event) => {
  const control = event.target.closest("[data-score-edit]");
  if (!control) return;
  const criterion = state.scoringExtraction?.criteria?.[Number(control.dataset.ci)];
  if (!criterion) return;
  if (control.dataset.scoreEdit === "in_scope") {
    criterion.in_scope = control.checked;
    criterion.exclusion_reason = control.checked ? "" : (criterion.exclusion_reason || "在Stage1确认页选择不纳入本次框架");
  }
  if (control.dataset.scoreEdit === "quote_field") {
    const quote = criterion.points?.[Number(control.dataset.pi)]?.source_quotes?.[Number(control.dataset.qi)];
    if (quote) quote.field = control.value;
  }
  markScoringChanged();
  renderScoringExtraction();
});

$("#scoring-extraction-content").addEventListener("click", (event) => {
  const button = event.target.closest("[data-score-action]");
  if (!button || state.readOnly) return;
  const ci = Number(button.dataset.ci);
  const pi = Number(button.dataset.pi);
  const criterion = state.scoringExtraction?.criteria?.[ci];
  if (!criterion) return;
  const points = criterion.points || (criterion.points = []);
  const point = points[pi];
  switch (button.dataset.scoreAction) {
    case "add-point":
      points.push({ point_id: `manual-${Date.now()}-${Math.random().toString(16).slice(2)}`, title: "", order: points.length + 1, source_quotes: [{ field: String(criterion.requirement_text || "").trim() ? "requirement_text" : "scoring_rule", text: "" }] });
      break;
    case "remove-point":
      points.splice(pi, 1);
      break;
    case "move-up":
      if (pi > 0) [points[pi - 1], points[pi]] = [points[pi], points[pi - 1]];
      break;
    case "move-down":
      if (pi < points.length - 1) [points[pi + 1], points[pi]] = [points[pi], points[pi + 1]];
      break;
    case "add-quote":
      if (point) point.source_quotes.push({ field: String(criterion.requirement_text || "").trim() ? "requirement_text" : "scoring_rule", text: "" });
      break;
    case "remove-quote":
      if (point?.source_quotes?.length > 1) point.source_quotes.splice(Number(button.dataset.qi), 1);
      break;
    default:
      return;
  }
  criterion.points.forEach((item, index) => item.order = index + 1);
  markScoringChanged();
  renderScoringExtraction();
});

$("#scoring-reviewed").addEventListener("change", (event) => {
  if (!state.scoringExtraction || state.readOnly) return;
  state.scoringExtraction.reviewed = event.target.checked;
  setDirty();
  updateScoringReviewState();
});

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => node.className = "toast", 3000);
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!payload.ok) throw new Error(payload.error || "请求失败");
  return payload;
}

function setDirty() {
  if (state.readOnly) return;
  const node = $("#save-state");
  node.innerHTML = "<span></span>有尚未提交的修改";
  node.style.color = "#b57a00";
}

function normalizedPath(value) {
  return value.trim().replace(/^file:\/\//, "");
}

function isAbsolutePath(value) {
  return value.startsWith("/");
}

function intakePaths(category) {
  return category === "background" ? state.intake.backgroundPaths : state.intake.referencePaths;
}

function renderIntakePaths() {
  ["background", "reference"].forEach((category) => {
    const paths = intakePaths(category);
    const prefix = category === "background" ? "background" : "reference";
    const list = $(`#intake-${prefix}-paths`);
    list.innerHTML = "";
    $(`#intake-${prefix}-count`).textContent = `${paths.length} 个路径`;

    if (!paths.length) {
      const empty = document.createElement("div");
      empty.className = "intake-path-empty";
      empty.innerHTML = category === "background"
        ? "<strong>暂未添加背景资料</strong><span>建议先加入正式需求书、招标文件和评分表。</span>"
        : "<strong>暂未添加参考资料</strong><span>可选；没有合适材料时可以留空。</span>";
      list.appendChild(empty);
      return;
    }

    paths.forEach((path, index) => {
      const row = document.createElement("div");
      row.className = "intake-path-row";
      row.innerHTML = '<span class="intake-file-icon">PATH</span><span class="intake-path-text"></span><button type="button" aria-label="删除此路径">×</button>';
      row.querySelector(".intake-path-text").textContent = path;
      row.querySelector(".intake-path-text").title = path;
      row.querySelector("button").disabled = state.intake.locked;
      row.querySelector("button").addEventListener("click", () => {
        if (state.intake.locked) return;
        paths.splice(index, 1);
        renderIntakePaths();
      });
      list.appendChild(row);
    });
  });
}

function addIntakePaths(paths, category) {
  const target = intakePaths(category);
  const other = intakePaths(category === "background" ? "reference" : "background");
  let added = 0;
  let conflict = false;
  (paths || []).forEach((rawPath) => {
    const path = normalizedPath(String(rawPath));
    if (path && other.includes(path)) {
      conflict = true;
    } else if (path && !target.includes(path)) {
      target.push(path);
      added += 1;
    }
  });
  renderIntakePaths();
  if (added) toast(`已添加 ${added} 个${category === "background" ? "背景" : "参考"}资料路径`);
  if (conflict) toast("同一路径不能同时归入背景资料和参考资料", true);
}

async function chooseIntakeMaterials(kind, category) {
  if (state.intake.locked) return;
  const prefix = category === "background" ? "background" : "reference";
  const button = $(`#intake-choose-${prefix}-${kind}`);
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "等待选择…";
  try {
    const payload = await requestJson("/api/materials/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    });
    addIntakePaths(payload.paths, category);
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (!state.intake.locked) button.disabled = false;
    button.textContent = original;
  }
}

function showIntakeModal() {
  $("#intake-modal").hidden = false;
  $(".app-shell").setAttribute("inert", "");
  document.body.classList.add("intake-open");
}

function hideIntakeModal() {
  $("#intake-modal").hidden = true;
  $(".app-shell").removeAttribute("inert");
  document.body.classList.remove("intake-open");
  if (state.intake.pollTimer) window.clearTimeout(state.intake.pollTimer);
  state.intake.pollTimer = null;
}

function setIntakeAnalyzing() {
  state.intake.locked = true;
  $("#intake-editor").hidden = true;
  $("#intake-analysis").hidden = false;
  const backgroundCount = state.intake.backgroundPaths.length;
  const referenceCount = state.intake.referencePaths.length;
  const total = backgroundCount + referenceCount;
  $("#analysis-material-title").textContent = total ? `背景资料 ${backgroundCount} 个 · 参考资料 ${referenceCount} 个` : "本次未附加本地资料";
  $("#analysis-material-detail").textContent = total ? "正在按资料类别建立读取边界" : "AI将根据项目背景生成初步口径";
  renderIntakePaths();
  scheduleSessionPoll();
}

function applyTenderPosition(position) {
  const normalized = position === "companion" ? "companion" : "main";
  state.intake.position = normalized;
  const radio = intakeForm.querySelector(`[name="tender-position"][value="${normalized}"]`);
  if (radio) radio.checked = true;
  const label = normalized === "companion" ? "陪标标书确认台" : "主标标书确认台";
  $("#brand-subtitle").textContent = label;
  document.title = label;
}

function scheduleSessionPoll() {
  if (state.intake.pollTimer) window.clearTimeout(state.intake.pollTimer);
  state.intake.pollTimer = window.setTimeout(pollSession, 1200);
}

async function pollSession() {
  try {
    const session = await requestJson("/api/session");
    if (session.stage === "intake" && session.mode === "editing" && state.intake.locked) {
      await loadIntake(session);
      return;
    }
    if ((session.stage === "stage1" || session.stage === "stage2") && session.handoff_ready) {
      hideIntakeModal();
      window.BiaoshuNotifications?.notify("项目口径已生成，已进入第 01 阶段。");
      await loadStage1();
      return;
    }
  } catch (error) {
    console.warn("等待项目口径时暂时无法连接：", error);
  }
  scheduleSessionPoll();
}

function setNextStageStatus(message, warning = false) {
  const node = $("#next-stage-status");
  if (!node) return;
  window.BiaoshuWorkflow.setStatus(node, message, warning);
}

function stopStageWait() {
  if (state.stageWait.pollTimer) window.clearTimeout(state.stageWait.pollTimer);
  state.stageWait.pollTimer = null;
}

function startStageWait() {
  stopStageWait();
  state.stageWait.startedAt = Date.now();
  setNextStageStatus(window.BiaoshuWorkflow.continuationHint);
  pollStageWait();
}

async function pollStageWait() {
  try {
    const [session, callback] = await Promise.all([requestJson("/api/session"), requestJson("/api/callback-status")]);
    const wait = callback.agent_wait;
    // The next page is only safe after the agent has actually entered its
    // blocking wait for that page.  A generated recommendation alone is not
    // enough: the user could otherwise confirm a page while the agent is
    // still writing it.
    if (session.stage === "stage2" && session.handoff_ready && wait?.status === "waiting" && wait?.stage === "stage2" && wait.process_alive) {
      stopStageWait();
      window.BiaoshuNotifications?.notify("标书框架已生成，正在进入第 02 阶段。");
      location.replace("/framework.html");
      return;
    }
    if (wait?.stage === "stage2" && wait?.status === "waiting" && wait.process_alive) {
      setNextStageStatus(window.BiaoshuWorkflow.continuationHint);
    } else if (callback.handoff_failed && callback.target_stage === "stage2") {
      setNextStageStatus("后台等待已中断、超时或被前序修改撤销。确认记录已保留；请重新打开原对话恢复，恢复后本页会自动跳转，旧页面请刷新一次。", true);
    } else if (callback.generation_delayed && callback.target_stage === "stage2") {
      setNextStageStatus(window.BiaoshuWorkflow.continuationHint);
    } else {
      setNextStageStatus(window.BiaoshuWorkflow.continuationHint);
    }
  } catch (error) {
    setNextStageStatus(window.BiaoshuWorkflow.continuationHint);
  }
  state.stageWait.pollTimer = window.setTimeout(pollStageWait, 1200);
}

async function loadIntake(session) {
  state.intake.locked = false;
  const submitButton = $("#intake-submit");
  submitButton.disabled = false;
  submitButton.innerHTML = '确认并开始分析 <span>→</span>';
  showIntakeModal();
  const payload = await requestJson("/api/intake");
  state.intake.sourceHash = payload.source_sha256;
  const recommendation = payload.recommendation || {};
  state.intake.backgroundPaths = [...(recommendation.background_paths || recommendation.source_paths || [])];
  state.intake.referencePaths = [...(recommendation.reference_paths || [])];
  applyTenderPosition(payload.receipt?.tender_position || recommendation.tender_position || "main");
  $("#intake-background").value = payload.receipt?.background ?? recommendation.background ?? "";
  if (payload.receipt) {
    state.intake.backgroundPaths = [...(payload.receipt.background_paths || payload.receipt.source_paths || [])];
    state.intake.referencePaths = [...(payload.receipt.reference_paths || [])];
  }
  renderIntakePaths();

  const workflow = payload.workflow || session;
  if (payload.receipt || workflow.mode === "awaiting_analysis") setIntakeAnalyzing();
  else $("#intake-background").focus();
}

function setReadOnly(enabled) {
  state.readOnly = enabled;
  document.body.classList.toggle("readonly-mode", enabled);
  form.querySelectorAll("input,textarea,select,button").forEach((element) => element.disabled = enabled);
  $("#confirm-button").disabled = false;

  if (enabled) {
    const banner = document.createElement("div");
    banner.className = "readonly-banner";
    banner.id = "readonly-banner";
    banner.textContent = "当前为只读回看模式。已确认内容不会因查看而失效；如需调整，请点击底部“修改本阶段”。";
    form.prepend(banner);
    $("#confirm-button").classList.add("edit-mode");
    $("#confirm-button").classList.add("stage-action");
    $("#confirm-button").innerHTML = '修改本阶段 <span>↺</span>';
    $("#save-state").innerHTML = "<span></span>项目口径已确认 · 当前只读回看";
    $("#save-state").style.color = "#0f9d70";
  } else {
    $("#readonly-banner")?.remove();
    $("#confirm-button").classList.remove("edit-mode");
    $("#confirm-button").classList.add("stage-action");
    $("#confirm-button").innerHTML = '确认项目口径并继续 <span>→</span>';
    $("#stage1-step-status").textContent = "当前阶段";
  }
  updateScoringReviewState();
}

async function loadStage1() {
  try {
    const payload = await requestJson("/api/stage1");
    state.sourceHash = payload.source_sha256;
    state.recommendation = structuredClone(payload.recommendation);
    state.intake.position = payload.recommendation.tender_position === "companion" ? "companion" : "main";
    applyTenderPosition(state.intake.position);
    state.defaults = structuredClone(payload.recommendation.formatting);
    state.workflow = payload.workflow;
    const data = payload.draft?.data || payload.receipt?.data || payload.recommendation;
    fill(data);
    state.scoringExtraction = structuredClone(data.scoring_extraction || payload.recommendation.scoring_extraction || {
      schema_version: 1, status: "pending", source_criterion_count: 0, no_rubric_reason: "", reviewed: false, criteria: [],
    });
    renderScoringExtraction();
    $("#side-project").textContent = data.project.project_name || "未命名项目";
    $("#source-count").textContent = payload.recommendation.source_summary?.file_count ?? "—";

    const completed = payload.workflow?.completed || [];
    const stage1Confirmed = Boolean(payload.receipt) || completed.includes("stage1");
    const stage1Editing = payload.workflow?.active_stage === "stage1" && payload.workflow?.mode === "editing";
    if (stage1Confirmed && !stage1Editing) {
      setReadOnly(true);
      window.WorkflowNav?.refresh();
    } else {
      setReadOnly(false);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function initialize() {
  try {
    const session = await requestJson("/api/session");
    if (session.stage === "intake") await loadIntake(session);
    else await loadStage1();
  } catch (error) {
    toast(error.message, true);
  }
}

intakeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.intake.locked || !intakeForm.reportValidity()) return;
  window.BiaoshuNotifications?.requestPermission();
  const background = $("#intake-background").value.trim();
  const tenderPosition = intakeForm.querySelector('[name="tender-position"]:checked')?.value;
  if (!background) {
    toast("请填写项目背景后再开始分析", true);
    $("#intake-background").focus();
    return;
  }

  const button = $("#intake-submit");
  button.disabled = true;
  button.textContent = "正在确认…";
  try {
    await requestJson("/api/intake/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_sha256: state.intake.sourceHash,
        background,
        background_paths: [...state.intake.backgroundPaths],
        reference_paths: [...state.intake.referencePaths],
        tender_position: tenderPosition,
      }),
    });
    setIntakeAnalyzing();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.innerHTML = '确认并开始分析 <span>→</span>';
  }
});

$("#intake-choose-background-files").addEventListener("click", () => chooseIntakeMaterials("files", "background"));
$("#intake-choose-background-folder").addEventListener("click", () => chooseIntakeMaterials("folder", "background"));
$("#intake-choose-reference-files").addEventListener("click", () => chooseIntakeMaterials("files", "reference"));
$("#intake-choose-reference-folder").addEventListener("click", () => chooseIntakeMaterials("folder", "reference"));

function addManualIntakePath(category) {
  const prefix = category === "background" ? "background" : "reference";
  const input = $(`#intake-${prefix}-manual-path`);
  const path = normalizedPath(input.value);
  if (!isAbsolutePath(path)) {
    toast("请输入以 / 开头的本地绝对路径", true);
    input.focus();
    return;
  }
  addIntakePaths([path], category);
  input.value = "";
}

$("#intake-add-background-path").addEventListener("click", () => addManualIntakePath("background"));
$("#intake-add-reference-path").addEventListener("click", () => addManualIntakePath("reference"));

[
  ["background", "background"],
  ["reference", "reference"],
].forEach(([category, prefix]) => {
  $(`#intake-${prefix}-manual-path`).addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      $(`#intake-add-${prefix}-path`).click();
    }
  });
});

form.addEventListener("input", (event) => {
  setDirty();
  if (event.target.name === "project.project_name") $("#side-project").textContent = event.target.value || "未命名项目";
});

$("#restore-format").addEventListener("click", () => {
  Object.entries(state.defaults || {}).forEach(([key, value]) => {
    const element = form.querySelector(`[name="formatting.${key}"]`);
    if (element) element.value = display(value);
  });
  setDirty();
  toast("已恢复当前标书定位的默认排版");
});

$("#cancel-reopen").addEventListener("click", () => $("#reopen-modal").hidden = true);
$("#confirm-reopen").addEventListener("click", async () => {
  const button = $("#confirm-reopen");
  button.disabled = true;
  try {
    await requestJson("/api/stage1/reopen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    location.replace("/?edit=stage1");
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

$("#confirm-button").addEventListener("click", async () => {
  if (state.readOnly) {
    $("#reopen-modal").hidden = false;
    return;
  }
  if (!form.reportValidity()) return;
  const button = $("#confirm-button");
  button.disabled = true;
  button.textContent = "正在提交…";
  try {
    await requestJson("/api/stage1/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_sha256: state.sourceHash, data: collect() }),
    });
    $("#save-state").innerHTML = "<span></span>项目口径已确认并回传给AI";
    $("#save-state").style.color = "#0f9d70";
    $("#success-modal").hidden = false;
    startStageWait();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.classList.add("stage-action");
    button.innerHTML = '确认项目口径并继续 <span>→</span>';
  }
});

$("#close-success").addEventListener("click", () => { $("#success-modal").hidden = true; });
initialize();
