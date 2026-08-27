const state = {
  sourceHash: "",
  source: null,
  data: null,
  chapters: [],
  selectedId: null,
  readOnly: false,
  collapsed: new Set(),
  loadFailed: false,
  waitTimer: null,
  aiAdjustTimer: null,
  aiAdjustPending: false,
};

const $ = (selector) => document.querySelector(selector);
const clone = (value) => structuredClone(value);
const TYPES = ["章首总览图", "流程图", "泳道图", "矩阵图", "时间轴", "生命周期图", "对比图", "其他"];

if ("scrollRestoration" in history) history.scrollRestoration = "manual";

function uid() {
  return `img-user-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#039;",
  })[char]);
}

function lines(value) {
  return Array.isArray(value) ? value.join("\n") : "";
}

function array(value) {
  return String(value || "").split("\n").map((item) => item.trim()).filter((item, index, all) => item && all.indexOf(item) === index);
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => { node.className = "toast"; }, 2800);
}

function setNextStageStatus(message, warning = false) {
  const node = $("#next-stage-status");
  if (!node) return;
  node.innerHTML = warning ? `<span>!</span>${message}` : `<span class="spinner"></span>${message}`;
}

function stopNextWait() {
  if (state.waitTimer) window.clearInterval(state.waitTimer);
  state.waitTimer = null;
}

async function pollNextStage() {
  try {
    const [session, callback] = await Promise.all([requestJson("/api/session"), requestJson("/api/callback-status")]);
    const wait = callback.agent_wait;
    if (session.stage === "stage4" && session.handoff_ready && wait?.status === "waiting" && wait?.stage === "stage4" && wait.process_alive) {
      stopNextWait();
      setNextStageStatus("最终交付方案已生成，正在进入下一阶段…");
      window.BiaoshuNotifications?.notify("最终交付方案已生成，正在进入第 04 阶段。");
      location.replace("/final.html");
      return;
    }
    if (wait?.stage === "stage4" && wait?.status === "waiting" && wait.process_alive) {
      setNextStageStatus("AI正在读取本阶段回执并生成最终交付方案…");
      return;
    }
    if (callback.handoff_failed && callback.target_stage === "stage4") {
      setNextStageStatus("后台等待已中断或超时。确认回执已保留，请重新打开原对话恢复；恢复后本页会自动跳转，旧页面请刷新一次。", true);
      return;
    }
    if (callback.generation_delayed && callback.target_stage === "stage4") {
      setNextStageStatus("AI仍在生成最终交付方案，内容较多时会耗时更长，请保持本页打开");
      return;
    }
    setNextStageStatus("确认已保存，正在等待当前对话接续处理…");
  } catch (_) {
    setNextStageStatus("正在确认AI接续状态，请保持本页打开…");
  }
}

function startNextWait() {
  stopNextWait();
  pollNextStage();
  state.waitTimer = window.setInterval(pollNextStage, 1200);
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "请求失败");
  return payload;
}

function stopAiAdjustWait() {
  if (state.aiAdjustTimer) window.clearInterval(state.aiAdjustTimer);
  state.aiAdjustTimer = null;
}

function setAiAdjustUi(pending, message) {
  state.aiAdjustPending = pending;
  document.body.classList.toggle("ai-adjust-pending", pending);
  const input = $("#ai-adjust-input");
  const button = $("#ai-adjust-submit");
  const status = $("#ai-adjust-status");
  if (input) input.disabled = state.readOnly || pending;
  if (button) button.disabled = state.readOnly || pending;
  if (status && message) status.textContent = message;
  const confirm = $("#confirm-button");
  if (confirm && pending) confirm.disabled = true;
}

async function pollAiAdjust() {
  try {
    const result = await requestJson("/api/stage3/ai-adjust-status");
    if (result.status === "ready") {
      stopAiAdjustWait();
      if (result.source_sha256 && result.source_sha256 !== state.sourceHash) {
        setAiAdjustUi(false, "AI已完成整体调整，正在载入最新推荐…");
        location.reload();
        return;
      }
      setAiAdjustUi(false, "AI整体调整已完成，当前页面已是最新内容");
      return;
    }
    if (result.status === "failed" || result.status === "superseded") {
      stopAiAdjustWait();
      setAiAdjustUi(false, "整体调整未完成，请重新提交");
      toast("AI整体调整未完成，请重新提交", true);
      return;
    }
    if (result.active) {
      setAiAdjustUi(true, "AI正在依据整体要求重新生成图片规划，请保持本页打开…");
      return;
    }
    stopAiAdjustWait();
    setAiAdjustUi(false, "未提交整体调整");
  } catch (_) {
    const status = $("#ai-adjust-status");
    if (status) status.textContent = "正在确认AI整体调整状态，请保持本页打开…";
  }
}

function startAiAdjustWait() {
  stopAiAdjustWait();
  pollAiAdjust();
  state.aiAdjustTimer = window.setInterval(pollAiAdjust, 1200);
}

async function refreshAiAdjustStatus() {
  try {
    const result = await requestJson("/api/stage3/ai-adjust-status");
    if (!result.active) return;
    if (result.status === "ready") {
      if (result.source_sha256 && result.source_sha256 !== state.sourceHash) location.reload();
      return;
    }
    if (result.status === "waiting") startAiAdjustWait();
  } catch (_) {
    // No active request is the normal state.
  }
}

async function submitAiAdjust() {
  if (state.readOnly || state.aiAdjustPending) return;
  const instruction = $("#ai-adjust-input").value.trim();
  if (!instruction) {
    toast("请先填写整体调整要求", true);
    return;
  }
  collectEditor();
  collectDirection();
  setAiAdjustUi(true, "正在提交整体调整请求…");
  try {
    await requestJson("/api/stage3/ai-adjust", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_sha256: state.sourceHash, instruction, data: state.data }),
    });
    startAiAdjustWait();
  } catch (error) {
    setAiAdjustUi(false, "未提交整体调整");
    toast(error.message, true);
  }
}

function stage2Chapters(stage2) {
  const chapters = stage2?.receipt?.data?.chapters || stage2?.draft?.data?.chapters || stage2?.recommendation?.chapters;
  if (!Array.isArray(chapters) || !chapters.length) throw new Error("未找到有效的第二阶段标书框架");
  return clone(chapters).map((chapter, index) => ({
    ...chapter,
    number: String(chapter.number || index + 1),
    title: String(chapter.title || `第${index + 1}章`),
  }));
}

function outlineOptions(chapter) {
  const result = [];
  const walk = (node) => {
    result.push({ id: node.id, number: node.number, title: node.title });
    (node.children || []).forEach(walk);
  };
  if (chapter) walk(chapter);
  return result;
}

const DEFAULT_VISUAL_DIRECTION = {
  palette: "深蓝、红色与白色为主，使用蓝色作为信息层级强调色，红色用于关键节点和提醒",
  style: "商务科技风格：扁平化线条图标、简洁大方、结构清晰，突出专业性与可读性",
  background: "白色或浅灰纯色背景，浅蓝作为区块点缀，不使用复杂背景图",
  density: "适中，保证每个核心信息点清晰可读，避免单页信息堆叠",
  avoid: ["不得使用招标人标志、真实客户头像、复杂渐变、密集小字、卡通化人物夸张表情"],
};

function directionObject(raw) {
  return raw && typeof raw === "object" && raw.visual_direction && typeof raw.visual_direction === "object"
    ? raw.visual_direction
    : {};
}

function firstVisualText(primary, fallback, keys) {
  for (const source of [primary, fallback]) {
    for (const key of keys) {
      const value = source?.[key];
      if (typeof value !== "string" && typeof value !== "number") continue;
      const text = String(value).trim();
      if (text) return text;
    }
  }
  return "";
}

function firstVisualList(primary, fallback, keys) {
  for (const source of [primary, fallback]) {
    for (const key of keys) {
      if (!source || !Object.prototype.hasOwnProperty.call(source, key)) continue;
      const value = source[key];
      if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
      if (typeof value === "string") {
        const result = array(value);
        if (result.length) return result;
      }
    }
  }
  return [];
}

function normalizeVisualDirection(raw, fallback) {
  const primary = directionObject(raw);
  const backup = directionObject(fallback);
  const avoid = firstVisualList(primary, backup, ["avoid", "avoid_styles", "avoid_style", "negative_prompt", "avoid_visuals", "avoid_visual_features", "避免的风格", "应避免", "避免"]);
  return {
    palette: firstVisualText(primary, backup, ["palette", "primary_colors", "main_colors", "color_palette", "colour_palette", "color_scheme", "colour_scheme", "colors", "colour", "配色", "配色方案", "主色", "主辅色"]) || DEFAULT_VISUAL_DIRECTION.palette,
    style: firstVisualText(primary, backup, ["style", "visual_style", "style_description", "风格", "视觉风格"]) || DEFAULT_VISUAL_DIRECTION.style,
    background: firstVisualText(primary, backup, ["background", "background_style", "background_description", "背景", "背景风格"]) || DEFAULT_VISUAL_DIRECTION.background,
    density: firstVisualText(primary, backup, ["density", "information_density", "info_density", "density_description", "信息密度", "信息密度说明"]) || DEFAULT_VISUAL_DIRECTION.density,
    avoid: avoid.length ? avoid : DEFAULT_VISUAL_DIRECTION.avoid,
  };
}

function normalizeData(raw, fallback = {}) {
  if (!raw || typeof raw !== "object") throw new Error("第三阶段推荐数据格式无效");
  const data = {
    visual_direction: normalizeVisualDirection(raw, fallback),
    chapter_settings: clone(raw.chapter_settings || []),
    images: clone(raw.images || []),
    cleanup_actions: clone(raw.cleanup_actions || []),
  };
  if (!Array.isArray(data.chapter_settings)) throw new Error("chapter_settings 必须是数组");
  if (!Array.isArray(data.images)) throw new Error("images 必须是数组");
  if (!Array.isArray(data.cleanup_actions)) throw new Error("cleanup_actions 必须是数组");
  data.images = data.images.map((sourceImage) => {
    const image = {
      id: String(sourceImage.id || ""),
      figure_no: String(sourceImage.figure_no || ""),
      order: Number(sourceImage.order || 0),
      chapter_id: String(sourceImage.chapter_id || ""),
      chapter_number: String(sourceImage.chapter_number || ""),
      chapter_title: String(sourceImage.chapter_title || ""),
      position: sourceImage.position && typeof sourceImage.position === "object" ? clone(sourceImage.position) : {},
      name: String(sourceImage.name || ""),
      type: String(sourceImage.type || "其他"),
      purpose: String(sourceImage.purpose || ""),
      core_nodes: Array.isArray(sourceImage.core_nodes) ? sourceImage.core_nodes.map(String) : array(sourceImage.core_nodes),
      composition: String(sourceImage.composition || ""),
      orientation: normalizeOrientation(sourceImage.orientation),
      is_chapter_overview: Boolean(sourceImage.is_chapter_overview),
      origin: String(sourceImage.origin || "ai"),
    };
    const chapter = state.chapters.find((item) => item.id === image.chapter_id)
      || state.chapters.find((item) => String(item.number) === String(image.chapter_number));
    if (chapter) {
      image.chapter_id = chapter.id;
      image.chapter_number = String(chapter.number);
      image.chapter_title = chapter.title;
    }
    return image;
  });
  return data;
}

function normalizeOrientation(value) {
  const orientation = String(value || "").trim();
  return ["landscape", "portrait", "square", "auto"].includes(orientation) ? orientation : "auto";
}

function renumber() {
  state.chapters.forEach((chapter) => {
    const images = state.data.images
      .filter((image) => image.chapter_id === chapter.id)
      .sort((a, b) => Number(a.order || 0) - Number(b.order || 0));
    images.forEach((image, index) => {
      image.order = index + 1;
      image.figure_no = `图${chapter.number}-${index + 1}`;
      image.chapter_number = String(chapter.number);
      image.chapter_title = chapter.title;
    });
  });
}

function selectedImage() {
  return state.data?.images.find((image) => image.id === state.selectedId) || null;
}

function isLocated(image) {
  return Boolean(String(image.position?.outline_node_id || "").trim() && String(image.position?.placement_note || "").trim());
}

function isComplete(image) {
  return Boolean(
    String(image.id || "").trim()
    && String(image.name || "").trim()
    && String(image.chapter_id || "").trim()
    && isLocated(image)
    && String(image.purpose || "").trim()
    && image.core_nodes.some((node) => String(node).trim())
    && String(image.composition || "").trim()
    && TYPES.includes(image.type)
  );
}

function metrics() {
  const images = state.data.images;
  return {
    total: images.length,
    overview: images.filter((image) => image.is_chapter_overview).length,
    located: images.filter(isLocated).length,
    incomplete: images.filter((image) => !isComplete(image)).length,
  };
}

function validate() {
  if (state.loadFailed || !state.data) return false;
  const counts = metrics();
  let error = "";
  if (!counts.total) error = "至少需要保留1张规划图片";
  else if (![state.data.visual_direction.palette, state.data.visual_direction.style, state.data.visual_direction.background, state.data.visual_direction.density].every((value) => String(value || "").trim())) error = "请补全统一视觉方向";
  else if (counts.incomplete) error = `仍有 ${counts.incomplete} 张图片需要补全必填字段`;
  else if (new Set(state.data.images.map((image) => image.id)).size !== counts.total) error = "图片稳定ID存在重复";
  else if (new Set(state.data.images.map((image) => image.figure_no)).size !== counts.total) error = "图号存在重复";
  const box = $("#validation");
  box.classList.toggle("error", Boolean(error) && !state.readOnly);
  box.querySelector("strong").textContent = state.readOnly ? "图片规划已确认" : error || "图片规划校验通过";
  box.querySelector("small").textContent = state.readOnly
    ? "当前为只读回看，需调整时请点击右侧“修改本阶段”"
    : error ? "请补全后再确认" : `共 ${counts.total} 张，${counts.located} 张已定位`;
  $("#confirm-button").disabled = state.readOnly ? false : Boolean(error);
  return !error;
}

function updateSummary() {
  const counts = metrics();
  $("#image-count").textContent = counts.total;
  $("#overview-count").textContent = counts.overview;
  $("#located-count").textContent = counts.located;
  $("#incomplete-count").textContent = counts.incomplete;
  const visual = state.data.visual_direction;
  $("#direction-summary").textContent = [visual.palette, visual.style, visual.background, visual.density].filter(Boolean).join(" · ") || "待确认视觉方向";
  validate();
}

function renderDirection() {
  const visual = state.data.visual_direction;
  $("#visual-palette").value = visual.palette;
  $("#visual-style").value = visual.style;
  $("#visual-background").value = visual.background;
  $("#visual-density").value = visual.density;
  $("#visual-avoid").value = lines(visual.avoid);
}

function cleanupText(action) {
  if (typeof action === "string") return action;
  if (!action || typeof action !== "object") return "";
  const serialized = Object.values(action).filter(Boolean).join(" ");
  if (serialized.includes("图4-1")) {
    const reason = action.reason || action.note || action.description || "";
    return `删除图4-1占位${reason ? `：${reason}` : ""}`;
  }
  return [action.action, action.target, action.reason, action.note, action.description].filter(Boolean).join("：");
}

function renderCleanupActions() {
  const actions = state.data.cleanup_actions.map(cleanupText).filter(Boolean);
  $("#cleanup-strip").hidden = !actions.length;
  $("#cleanup-actions").innerHTML = actions.map((action) => `<p>${escapeHtml(action)}</p>`).join("");
}

function renderGroups() {
  renumber();
  const box = $("#chapter-groups");
  box.innerHTML = "";
  state.chapters.forEach((chapter) => {
    const images = state.data.images.filter((image) => image.chapter_id === chapter.id).sort((a, b) => a.order - b.order);
    const incomplete = images.filter((image) => !isComplete(image)).length;
    const group = document.createElement("section");
    group.className = `chapter-group${state.collapsed.has(chapter.id) ? " collapsed" : ""}`;
    group.innerHTML = `
      <button type="button" class="group-head">
        <span class="chapter-no">${escapeHtml(chapter.number)}</span>
        <span class="group-copy"><strong>${escapeHtml(chapter.title)}</strong><small>${images.length} 张图${incomplete ? ` · ${incomplete} 张待完善` : " · 规划已完整"}</small></span>
        <span class="group-toggle">⌄</span>
      </button>
      <div class="chapter-items"></div>
      <div class="quick-add"><input aria-label="新增图片名称" placeholder="输入新图片名称或一句话说明"><button type="button">＋ 添加</button></div>
    `;
    const list = group.querySelector(".chapter-items");
    if (!images.length) list.innerHTML = '<div class="empty-group">本章尚未规划图片</div>';
    images.forEach((image) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `image-row${image.id === state.selectedId ? " selected" : ""}`;
      row.innerHTML = `
        <span class="figure-no">${escapeHtml(image.figure_no)}</span>
        <span class="row-copy"><strong>${escapeHtml(image.name || "未命名图片")}</strong><small>${escapeHtml(image.position?.placement_note || "待填写放置位置")}</small></span>
        <span class="row-status${isComplete(image) ? "" : " incomplete"}" title="${isComplete(image) ? "规划已完整" : "待完善"}"></span>
      `;
      row.addEventListener("click", () => selectImage(image.id));
      list.appendChild(row);
    });
    group.querySelector(".group-head").addEventListener("click", () => {
      if (state.collapsed.has(chapter.id)) state.collapsed.delete(chapter.id);
      else state.collapsed.add(chapter.id);
      group.classList.toggle("collapsed");
      updateExpandLabel();
    });
    const input = group.querySelector(".quick-add input");
    const add = () => addImage(chapter.id, input.value);
    group.querySelector(".quick-add button").addEventListener("click", add);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); add(); }
    });
    box.appendChild(group);
  });
  updateExpandLabel();
}

function updateExpandLabel() {
  $("#expand-groups").textContent = state.collapsed.size ? "全部展开" : "全部收起";
}

function fillPositionOptions(image, chapterId = image.chapter_id) {
  const chapter = state.chapters.find((item) => item.id === chapterId);
  const select = $("#edit-position-node");
  select.innerHTML = "";
  outlineOptions(chapter).forEach((option) => {
    const node = document.createElement("option");
    node.value = option.id;
    node.textContent = `${option.number} ${option.title}`;
    select.appendChild(node);
  });
  select.value = image.position?.outline_node_id || chapter?.id || "";
}

function fillEditor() {
  const image = selectedImage();
  $("#empty-editor").hidden = Boolean(image);
  $("#image-form").hidden = !image;
  if (!image) return;
  $("#edit-figure-no").textContent = `${image.figure_no} · ${image.type}`;
  $("#edit-heading").textContent = image.name || "未命名图片";
  $("#edit-name").value = image.name || "";
  $("#edit-type").value = TYPES.includes(image.type) ? image.type : "其他";
  $("#edit-chapter").innerHTML = state.chapters.map((chapter) => `<option value="${escapeHtml(chapter.id)}">${escapeHtml(chapter.number)} ${escapeHtml(chapter.title)}</option>`).join("");
  $("#edit-chapter").value = image.chapter_id;
  fillPositionOptions(image);
  $("#edit-orientation").value = image.orientation || "auto";
  $("#edit-placement").value = image.position?.placement_note || "";
  $("#edit-purpose").value = image.purpose || "";
  $("#edit-nodes").value = lines(image.core_nodes);
  $("#edit-composition").value = image.composition || "";
  $("#edit-overview").checked = Boolean(image.is_chapter_overview);
  const complete = isComplete(image);
  $("#edit-state").textContent = complete ? "规划已完整" : "待完善";
  $("#edit-state").classList.toggle("incomplete", !complete);
  $("#image-form").querySelectorAll("input,textarea,select").forEach((control) => { control.disabled = state.readOnly; });
}

function resetEditorView({ focusName = false } = {}) {
  const pane = $("#editor-pane");
  const active = document.activeElement;
  if (active && pane?.contains(active) && typeof active.blur === "function") active.blur();
  const reset = () => {
    const scroller = $(".editor-scroll");
    if (scroller) {
      scroller.scrollTop = 0;
      scroller.scrollLeft = 0;
    }
    document.querySelectorAll(".editor-scroll textarea").forEach((textarea) => {
      textarea.scrollTop = 0;
      textarea.scrollLeft = 0;
    });
    if (focusName && !state.readOnly) $("#edit-name")?.focus({ preventScroll: true });
  };
  reset();
  requestAnimationFrame(() => requestAnimationFrame(reset));
}

function selectImage(id) {
  const changed = state.selectedId !== id;
  state.selectedId = id;
  renderGroups();
  fillEditor();
  if (changed) resetEditorView();
}

function addImage(chapterId, rawName) {
  if (state.readOnly) return;
  const name = String(rawName || "").trim();
  if (!name) {
    toast("请先输入新图片名称或一句话说明", true);
    return;
  }
  const chapter = state.chapters.find((item) => item.id === chapterId);
  if (!chapter) return;
  const image = {
    id: uid(),
    figure_no: "",
    order: state.data.images.filter((item) => item.chapter_id === chapter.id).length + 1,
    chapter_id: chapter.id,
    chapter_number: String(chapter.number),
    chapter_title: chapter.title,
    position: { outline_node_id: chapter.id, outline_number: String(chapter.number), outline_title: chapter.title, placement_note: "" },
    name,
    type: "其他",
    purpose: "",
    core_nodes: [],
    composition: "",
    orientation: "auto",
    is_chapter_overview: false,
    origin: "user",
  };
  state.data.images.push(image);
  state.collapsed.delete(chapter.id);
  state.selectedId = image.id;
  render();
  resetEditorView({ focusName: true });
  toast("已新增规划项，请在右侧补全位置、用途、节点和构图");
}

function collectEditor() {
  const image = selectedImage();
  if (!image || state.readOnly) return image;
  const previous = JSON.stringify({
    chapter_id: image.chapter_id,
    chapter_number: image.chapter_number,
    chapter_title: image.chapter_title,
    order: image.order,
    orientation: image.orientation,
    position: image.position,
    name: image.name,
    type: image.type,
    purpose: image.purpose,
    core_nodes: image.core_nodes,
    composition: image.composition,
    is_chapter_overview: image.is_chapter_overview,
  });
  const previousChapterId = image.chapter_id;
  const chapter = state.chapters.find((item) => item.id === $("#edit-chapter").value);
  const options = outlineOptions(chapter);
  const option = options.find((item) => item.id === $("#edit-position-node").value) || options[0];
  image.name = $("#edit-name").value.trim();
  image.type = $("#edit-type").value;
  image.chapter_id = chapter?.id || "";
  image.chapter_number = String(chapter?.number || "");
  image.chapter_title = chapter?.title || "";
  if (previousChapterId !== image.chapter_id) image.order = state.data.images.filter((item) => item.id !== image.id && item.chapter_id === image.chapter_id).length + 1;
  image.orientation = normalizeOrientation($("#edit-orientation").value);
  image.position = {
    outline_node_id: option?.id || "",
    outline_number: String(option?.number || ""),
    outline_title: option?.title || "",
    placement_note: $("#edit-placement").value.trim(),
  };
  image.purpose = $("#edit-purpose").value.trim();
  image.core_nodes = array($("#edit-nodes").value);
  image.composition = $("#edit-composition").value.trim();
  image.is_chapter_overview = $("#edit-overview").checked;
  const current = JSON.stringify({
    chapter_id: image.chapter_id,
    chapter_number: image.chapter_number,
    chapter_title: image.chapter_title,
    order: image.order,
    orientation: image.orientation,
    position: image.position,
    name: image.name,
    type: image.type,
    purpose: image.purpose,
    core_nodes: image.core_nodes,
    composition: image.composition,
    is_chapter_overview: image.is_chapter_overview,
  });
  if (previous !== current) delete image.ai_prompt;
  return image;
}

function collectDirection() {
  if (state.readOnly) return;
  const previous = JSON.stringify(state.data.visual_direction || {});
  state.data.visual_direction = {
    palette: $("#visual-palette").value.trim(),
    style: $("#visual-style").value.trim(),
    background: $("#visual-background").value.trim(),
    density: $("#visual-density").value.trim(),
    avoid: array($("#visual-avoid").value),
  };
  if (previous !== JSON.stringify(state.data.visual_direction)) {
    state.data.images.forEach((image) => { delete image.ai_prompt; });
  }
  updateSummary();
}

function applyReadOnly() {
  document.body.classList.toggle("stage-readonly", state.readOnly);
  $("#readonly-badge").hidden = !state.readOnly;
  $("#page-help").textContent = state.readOnly
    ? "本阶段已确认，以下图片名称、放置位置、核心表达与构图建议仅供回看。只有点击“修改本阶段”并确认影响范围后才会重新开启编辑。"
    : "本阶段只确认图片规划与逐图AI生图提示词，不生成、不插入图片。新增项只记录你输入的文字，不会在浏览器中调用AI。";
  $("#confirm-button").classList.add("stage-action");
  $("#confirm-button").innerHTML = state.readOnly ? '修改本阶段 <span>↺</span>' : '确认图片规划并继续 <span>→</span>';
  $("#direction-editor").hidden = state.readOnly;
  $("#toggle-direction").textContent = $("#direction-editor").hidden ? "展开" : "收起";
  $("#toggle-direction").setAttribute("aria-expanded", String(!$("#direction-editor").hidden));
  fillEditor();
  setAiAdjustUi(state.aiAdjustPending);
}

function render() {
  renumber();
  renderDirection();
  renderCleanupActions();
  renderGroups();
  fillEditor();
  updateSummary();
  applyReadOnly();
}

function showLoadError(message) {
  state.loadFailed = true;
  $(".plan-board").hidden = true;
  $(".direction-strip").hidden = true;
  $("#direction-editor").hidden = true;
  $(".ai-adjust-card").hidden = true;
  $("#cleanup-strip").hidden = true;
  $("#load-error").hidden = false;
  $("#load-error-message").textContent = message;
  $("#confirm-button").disabled = true;
  $("#validation").classList.add("error");
  $("#validation strong").textContent = "第三阶段数据尚未就绪";
  $("#validation small").textContent = "页面不会用浏览器示例数据代替AI推荐";
}

async function load() {
  state.loadFailed = false;
  $("#load-error").hidden = true;
  $(".plan-board").hidden = false;
  $(".direction-strip").hidden = false;
  $(".ai-adjust-card").hidden = false;
  const [stage3, stage2, stage1] = await Promise.all([
    requestJson("/api/stage3"),
    requestJson("/api/stage2"),
    requestJson("/api/stage1"),
  ]);
  if (!stage3.recommendation) throw new Error("未找到 stage3-recommendations.json 对应的推荐数据");
  state.chapters = stage2Chapters(stage2);
  state.sourceHash = stage3.source_sha256 || "";
  state.source = clone(stage3.recommendation);
  state.data = normalizeData(stage3.draft?.data || stage3.receipt?.data || stage3.recommendation, stage3.recommendation);
  state.readOnly = Boolean(stage3.receipt) && !(stage3.workflow?.active_stage === "stage3" && stage3.workflow?.mode === "editing");
  $("#side-project").textContent = stage1.receipt?.data?.project?.project_name || stage1.recommendation?.project?.project_name || "未命名项目";
  $("#edit-type").innerHTML = TYPES.map((type) => `<option value="${type}">${type}</option>`).join("");
  state.selectedId = state.data.images[0]?.id || null;
  render();
  refreshAiAdjustStatus();
  resetEditorView();
  const stage4Ready = await refreshStage4Availability();
  if (state.readOnly && !stage4Ready) {
    $("#success-modal").hidden = false;
    startNextWait();
  }
}

async function refreshStage4Availability() {
  try {
    await requestJson("/api/stage4");
    window.WorkflowNav?.refresh();
    return true;
  } catch (_) {
    window.WorkflowNav?.refresh();
    return false;
  }
}

async function goStage4() {
  try {
    const stage4 = await requestJson("/api/stage4");
    if (!stage4.recommendation) throw new Error("最终交付方案尚未准备好");
    location.href = "/final.html";
  } catch (error) {
    toast(error.message || "最终交付方案尚未准备好", true);
  }
}

$("#go-stage1").addEventListener("click", () => { location.href = "/?view=stage1"; });
$("#go-stage2").addEventListener("click", () => { location.href = "/framework.html?view=stage2"; });
$("#go-stage4").addEventListener("click", goStage4);
$("#retry-load").addEventListener("click", () => { location.reload(); });
$("#ai-adjust-submit").addEventListener("click", submitAiAdjust);

$("#toggle-direction").addEventListener("click", () => {
  if (!state.readOnly) {
    $("#direction-editor").hidden = !$("#direction-editor").hidden;
    $("#toggle-direction").textContent = $("#direction-editor").hidden ? "展开" : "收起";
    $("#toggle-direction").setAttribute("aria-expanded", String(!$("#direction-editor").hidden));
  }
});

$("#direction-editor").addEventListener("input", collectDirection);

$("#edit-chapter").addEventListener("change", () => {
  const image = selectedImage();
  if (image && !state.readOnly) fillPositionOptions(image, $("#edit-chapter").value);
});

$("#expand-groups").addEventListener("click", () => {
  if (state.collapsed.size) state.collapsed.clear();
  else state.chapters.forEach((chapter) => state.collapsed.add(chapter.id));
  renderGroups();
});

$("#save-image").addEventListener("click", () => {
  const image = collectEditor();
  if (!image) return;
  render();
  toast(isComplete(image) ? "本张图片规划已保存" : "已保存，仍有必填字段待完善", !isComplete(image));
});

$("#delete-image").addEventListener("click", () => {
  if (state.readOnly) return;
  const image = selectedImage();
  if (!image || !window.confirm(`确定删除“${image.figure_no} ${image.name}”吗？删除后本章图号会自动重排。`)) return;
  state.data.images = state.data.images.filter((item) => item.id !== image.id);
  state.selectedId = state.data.images[0]?.id || null;
  render();
  resetEditorView();
  toast("规划项已删除，图号已自动重排");
});

$("#confirm-button").addEventListener("click", async () => {
  if (state.readOnly) {
    $("#reopen-modal").hidden = false;
    return;
  }
  collectEditor();
  collectDirection();
  state.data.images.forEach((image) => { image.orientation = normalizeOrientation(image.orientation); });
  render();
  if (!validate()) return;
  const button = $("#confirm-button");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const result = await requestJson("/api/stage3/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_sha256: state.sourceHash, data: state.data }),
    });
    state.readOnly = Boolean(result.receipt);
    render();
    $("#success-modal").hidden = false;
    startNextWait();
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (!state.readOnly) {
      button.disabled = false;
      button.innerHTML = '确认图片规划并继续 <span>→</span>';
    }
  }
});

$("#cancel-reopen").addEventListener("click", () => { $("#reopen-modal").hidden = true; });
$("#confirm-reopen").addEventListener("click", async () => {
  const button = $("#confirm-reopen");
  button.disabled = true;
  button.textContent = "正在重新开启…";
  try {
    await requestJson("/api/stage3/reopen", { method: "POST" });
    location.reload();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = "确认修改并重新开始";
  }
});

$("#close-success").addEventListener("click", () => { $("#success-modal").hidden = true; });

load().catch((error) => showLoadError(error.message || "图片规划页载入失败"));
