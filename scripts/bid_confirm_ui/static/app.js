const state = {
  sourceHash: "",
  recommendation: null,
  defaults: null,
  workflow: null,
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
  return data;
}

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
  node.innerHTML = warning ? message : `<span class="spinner"></span>${message}`;
  node.classList.toggle("wait-warning", warning);
}

function stopStageWait() {
  if (state.stageWait.pollTimer) window.clearTimeout(state.stageWait.pollTimer);
  state.stageWait.pollTimer = null;
}

function startStageWait() {
  stopStageWait();
  state.stageWait.startedAt = Date.now();
  setNextStageStatus("正在等待AI接续处理");
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
      setNextStageStatus("AI正在读取已确认内容并生成标书框架");
    } else if (callback.handoff_failed && callback.target_stage === "stage2") {
      setNextStageStatus("后台等待已中断、超时或被前序修改撤销。确认记录已保留；请重新打开原对话恢复，恢复后本页会自动跳转，旧页面请刷新一次。", true);
    } else if (callback.generation_delayed && callback.target_stage === "stage2") {
      setNextStageStatus("AI仍在生成标书框架，内容较多时会耗时更长，请保持本页打开");
    } else {
      setNextStageStatus("确认已保存，正在等待AI接续处理");
    }
  } catch (error) {
    setNextStageStatus("本地状态检测暂时不可用，正在重试…", false);
  }
  state.stageWait.pollTimer = window.setTimeout(pollStageWait, 1200);
}

async function loadIntake(session) {
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
  form.querySelectorAll("input,textarea,button").forEach((element) => element.disabled = enabled);
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
