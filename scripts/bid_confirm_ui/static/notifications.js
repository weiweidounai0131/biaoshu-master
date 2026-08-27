(() => {
  const supported = "Notification" in window;
  const tag = "biaoshu-stage-transition";

  async function requestPermission() {
    if (!supported || Notification.permission !== "default") return supported ? Notification.permission : "unsupported";
    try {
      return await Notification.requestPermission();
    } catch (_) {
      return "denied";
    }
  }

  function notify(body) {
    if (!supported || Notification.permission !== "granted") return;
    try {
      const notice = new Notification("标书确认台", { body, tag, renotify: true });
      notice.onclick = () => { window.focus(); notice.close(); };
      window.setTimeout(() => notice.close(), 8000);
    } catch (_) {
      // Browser or operating-system settings must not block workflow transitions.
    }
  }

  window.BiaoshuNotifications = Object.freeze({ requestPermission, notify });
})();
