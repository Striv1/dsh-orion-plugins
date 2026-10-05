(() => {
  const activeTimers = new Set();

  const everyVisible = (callback, intervalMs) => {
    if (typeof callback !== "function") throw new TypeError("轮询回调必须是函数。");
    if (!Number.isFinite(intervalMs) || intervalMs < 250) {
      throw new RangeError("轮询间隔不得小于 250ms。");
    }
    let running = false;
    const tick = () => {
      if (document.visibilityState === "hidden" || running) return;
      running = true;
      Promise.resolve()
        .then(callback)
        .finally(() => {
          running = false;
        });
    };
    const timer = window.setInterval(tick, intervalMs);
    activeTimers.add(timer);
    return () => {
      activeTimers.delete(timer);
      window.clearInterval(timer);
    };
  };

  const stopAll = () => {
    for (const timer of activeTimers) window.clearInterval(timer);
    activeTimers.clear();
  };

  window.__ORION_POLLING__ = Object.freeze({ everyVisible, stopAll });
})();
