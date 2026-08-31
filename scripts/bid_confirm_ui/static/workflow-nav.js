(() => {
  const STAGES = [
    {id:"stage1", number:"01", href:"/?view=stage1"},
    {id:"stage2", number:"02", href:"/framework.html?view=stage2"},
    {id:"stage3", number:"03", href:"/images.html?view=stage3"},
    {id:"stage4", number:"04", href:"/final.html?view=stage4"},
  ];
  let latest = null;
  const page = document.body.dataset.pageStage || "stage1";
  const instanceId = sessionStorage.getItem("biaoshu-confirm-page-instance") || (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`);
  sessionStorage.setItem("biaoshu-confirm-page-instance", instanceId);

  function reportPresence() {
    fetch("/api/page-presence", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({page, instance_id: instanceId}),
      keepalive: true,
    }).catch(() => {});
  }

  function apply(session) {
    latest = session;
    const completed = new Set(session.completed || []);
    const viewedStage = document.body.dataset.pageStage || session.stage;
    document.querySelectorAll(".steps .step[data-stage]").forEach((button) => {
      const stage = STAGES.find((item) => item.id === button.dataset.stage);
      if (!stage) return;
      const isViewed = viewedStage === stage.id;
      const isWorkflowCurrent = session.stage === stage.id;
      // Stage 4 remains the workflow's current production stage even when a
      // user is temporarily viewing an earlier confirmed stage.
      const isDone = completed.has(stage.id) && !isViewed && !isWorkflowCurrent;
      const available = isWorkflowCurrent || completed.has(stage.id) || isViewed;
      button.classList.toggle("active", isViewed);
      button.classList.toggle("done", isDone);
      button.classList.toggle("workflow-current", isWorkflowCurrent && !isViewed);
      button.classList.toggle("locked", !available);
      button.disabled = !available;
      button.dataset.href = stage.href;
      if (stage.id === "stage4" && session.delivery_active) {
        const title = button.querySelector("strong");
        if (title) title.textContent = "生产与审校";
      }
      const marker = button.querySelector(":scope > span");
      if (marker) marker.textContent = isDone ? "✓" : stage.number;
      const status = button.querySelector("small");
      if (status) {
        if (stage.id === "stage4" && session.delivery_active && !isViewed) status.textContent = "当前阶段";
        else if (isViewed && completed.has(stage.id)) status.textContent = "已确认 · 当前查看";
        else if (isViewed && isWorkflowCurrent) status.textContent = session.mode === "confirmed" ? "已确认 · 当前查看" : "当前阶段";
        else if (isDone) status.textContent = "已确认 · 只读回看";
        else if (isWorkflowCurrent) status.textContent = session.delivery_active ? "生产与审校" : "当前进度 · 点击返回";
        else status.textContent = "尚未解锁";
      }
    });
  }

  async function refresh() {
    try {
      const [response, deliveryResponse] = await Promise.all([
        fetch("/api/session", {cache:"no-store"}),
        fetch("/api/delivery-status", {cache:"no-store"}),
      ]);
      const session = await response.json();
      const delivery = await deliveryResponse.json().catch(() => ({}));
      if (!response.ok || !session.ok) throw new Error(session.error || "流程状态读取失败");
      // Keep the three confirmed stages available for read-only review while
      // Stage 4 is producing.  Only the Stage-4 confirmation page itself
      // should hand off to the dedicated production/review service.
      if (page === "stage4" && delivery.delivery_ready && delivery.delivery_url) {
        location.replace(delivery.delivery_url);
        return session;
      }
      session.delivery_active = Boolean(delivery.delivery_ready);
      apply(session);
      return session;
    } catch (error) {
      console.warn("流程导航状态暂时不可用：", error);
      return latest;
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest(".steps .step[data-stage]");
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (button.disabled || button.classList.contains("locked")) return;
    const href = button.dataset.href;
    if (href) location.href = href;
  }, true);

  window.WorkflowNav = {refresh, apply, get state(){return latest;}};
  reportPresence();
  window.setInterval(reportPresence, 15000);
  document.addEventListener("visibilitychange", reportPresence);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", refresh, {once:true});
  else refresh();
  window.setInterval(refresh, 1200);
})();
