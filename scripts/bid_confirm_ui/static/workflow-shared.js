(function () {
  function setStatus(node, message, warning) {
    if (!node) return;
    node.classList.toggle("wait-warning", Boolean(warning));

    if (warning) {
      node.replaceChildren(document.createTextNode(String(message ?? "")));
      return;
    }

    let spinner = null;
    let copy = null;
    Array.from(node.children).forEach((child) => {
      if (child.classList.contains("spinner")) spinner = child;
      if (child.classList.contains("status-copy")) copy = child;
    });

    // The first call normalizes the server-rendered markup. Later polling calls
    // update only this text node, so the CSS animation keeps its own timeline.
    if (!spinner || !copy) {
      node.replaceChildren();
      spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      copy = document.createElement("span");
      copy.className = "status-copy";
      node.append(spinner, copy);
    }
    copy.textContent = String(message ?? "");
  }

  window.BiaoshuWorkflow = window.BiaoshuWorkflow || {};
  window.BiaoshuWorkflow.setStatus = setStatus;
}());
