// Dashboard live updates. Polls this page for a small server-rendered
// fragment (the #dashboard-content live region — routers/dashboard.py
// returns just that when X-Auto-Refresh is set) and patches each
// [data-cell] in place. Only cells whose HTML actually changed repaint,
// so there's no full-subtree swap, no flicker, no lost scroll position,
// and the Activity tab / any :hover survives a refresh. Plain
// fetch/DOMParser, no framework, no build step.
//
// Also runs a once-a-second ticker that keeps every <time data-ago>
// element ("3m ago") current between fetches, and drives the
// "updated Ns ago" text in the header.
(function () {
  "use strict";

  var script = document.currentScript;
  var intervalMs = parseInt(script.dataset.intervalMs, 10);

  var root = document.getElementById("dashboard-content");
  var statusEl = document.getElementById("refresh-status");
  if (!root) return;

  var lastUpdate = Date.now();
  var pollTimer = null;

  // ---- relative time -----------------------------------------------------

  function formatAgo(ms) {
    var s = Math.round(ms / 1000);
    if (s < 0) s = 0;
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function tickRelativeTimes(scope) {
    var now = Date.now();
    (scope || document).querySelectorAll("time[data-ago][datetime]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("datetime"));
      if (!isNaN(t)) el.textContent = formatAgo(now - t);
    });
  }

  function tickStatus() {
    if (!statusEl) return;
    var s = Math.round((Date.now() - lastUpdate) / 1000);
    statusEl.textContent = s < 3 ? "updated just now" : "updated " + formatAgo(Date.now() - lastUpdate);
  }

  // ---- cell patching ----------------------------------------------------

  function patch(nextRoot) {
    var changed = 0;
    nextRoot.querySelectorAll("[data-cell]").forEach(function (nextCell) {
      var name = nextCell.getAttribute("data-cell");
      var cur = root.querySelector('[data-cell="' + CSS.escape(name) + '"]');
      if (!cur) return;
      if (cur.innerHTML.trim() !== nextCell.innerHTML.trim()) {
        cur.innerHTML = nextCell.innerHTML;
        cur.classList.remove("cell-flash");
        // reflow so re-adding the class restarts the animation
        void cur.offsetWidth;
        cur.classList.add("cell-flash");
        changed++;
      }
      // width-style cells (the queue bar) carry their value in an attribute
      if (nextCell.style.cssText && nextCell.style.cssText !== cur.style.cssText) {
        cur.style.cssText = nextCell.style.cssText;
      }
    });
    return changed;
  }

  function refresh() {
    if (document.hidden) return;
    fetch(window.location.pathname + window.location.search, {
      headers: { "X-Auto-Refresh": "1" },
    })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.text();
      })
      .then(function (html) {
        var next = new DOMParser().parseFromString(html, "text/html").getElementById("dashboard-content");
        if (next) {
          patch(next);
          tickRelativeTimes(root);
        }
        lastUpdate = Date.now();
        tickStatus();
      })
      .catch(function () {
        if (statusEl) statusEl.textContent = "update failed — retrying";
      })
      .finally(function () {
        clearTimeout(pollTimer);
        if (intervalMs > 0) pollTimer = setTimeout(refresh, intervalMs);
      });
  }

  // (The Activity Items/Audit tabs are CSS-only — radio + :checked ~ — so
  // they work with no JS and survive a cell patch untouched.)

  // ---- wire up --------------------------------------------------------

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });

  setInterval(function () {
    tickRelativeTimes(document);
    tickStatus();
  }, 1000);

  if (intervalMs > 0) pollTimer = setTimeout(refresh, intervalMs);
})();
