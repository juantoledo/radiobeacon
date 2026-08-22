// Polls this page on an interval and swaps in the freshly-rendered
// #dashboard-content in place — no full page reload/flicker, no SPA
// framework, no build step. Polling (not push) because there's no
// cross-process DB change notification to hook into: data-adapters and
// dispatcher write storage/radiobeacon.db from their own separate
// processes/connections, so the browser re-asking the server "what does
// the dashboard look like now" is the same mechanism every other
// consumer of that file already uses, just client-side.
(function () {
  "use strict";

  var script = document.currentScript;
  var intervalMs = parseInt(script.dataset.intervalMs, 10);
  if (!intervalMs || intervalMs <= 0) return;

  var target = document.getElementById("dashboard-content");
  var statusEl = document.getElementById("refresh-status");
  if (!target) return;

  var timer = null;

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function scheduleNext() {
    clearTimeout(timer);
    timer = setTimeout(refresh, intervalMs);
  }

  function refresh() {
    // Don't burn requests on a backgrounded tab — catch up immediately
    // when it becomes visible again instead (see the listener below).
    if (document.hidden) return;

    fetch(window.location.pathname, { headers: { "X-Auto-Refresh": "1" } })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
      })
      .then(function (html) {
        var next = new DOMParser()
          .parseFromString(html, "text/html")
          .getElementById("dashboard-content");
        if (next) target.replaceChildren.apply(target, next.childNodes);
        setStatus("Updated just now");
      })
      .catch(function () {
        // Best-effort: leave the current view in place and retry next
        // tick — a transient failure here must never break the page.
        setStatus("Update failed — retrying");
      })
      .finally(scheduleNext);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });

  scheduleNext();
})();
