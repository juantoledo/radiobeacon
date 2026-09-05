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
//
// Cell-patching itself lives in ajax-forms.js (RB.patchCells), shared with
// the AJAX write-action responses that page also handles — base.html loads
// it first so it's defined by the time this file runs.
(function () {
  "use strict";

  var script = document.currentScript;
  var intervalMs = parseInt(script.dataset.intervalMs, 10);

  // Translated strings for the relative-time ticker and the "updated ..."
  // header text, rendered server-side by dashboard.html onto this same
  // <script> tag (same data-attribute convention as data-interval-ms
  // above). Falls back to English if the attribute is somehow missing.
  var i18n;
  try {
    i18n = JSON.parse(script.dataset.i18n || "{}");
  } catch (e) {
    i18n = {};
  }
  i18n = {
    just_now: i18n.just_now || "just now",
    minutes_ago: i18n.minutes_ago || "{n}m ago",
    hours_ago: i18n.hours_ago || "{n}h ago",
    days_ago: i18n.days_ago || "{n}d ago",
    updated_just_now: i18n.updated_just_now || "updated just now",
    updated_ago: i18n.updated_ago || "updated {ago}",
    update_failed: i18n.update_failed || "update failed — retrying",
  };

  var root = document.getElementById("dashboard-content");
  var statusEl = document.getElementById("refresh-status");
  if (!root) return;

  var lastUpdate = Date.now();
  var pollTimer = null;

  // ---- relative time -----------------------------------------------------

  function formatAgo(ms) {
    var s = Math.round(ms / 1000);
    if (s < 0) s = 0;
    if (s < 45) return i18n.just_now;
    if (s < 3600) return i18n.minutes_ago.replace("{n}", Math.round(s / 60));
    if (s < 86400) return i18n.hours_ago.replace("{n}", Math.round(s / 3600));
    return i18n.days_ago.replace("{n}", Math.round(s / 86400));
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
    statusEl.textContent = s < 3 ? i18n.updated_just_now : i18n.updated_ago.replace("{ago}", formatAgo(Date.now() - lastUpdate));
  }

  // ---- cell patching ----------------------------------------------------

  // Factored into ajax-forms.js (loaded first, see base.html) as
  // RB.patchCells so the same diff-by-string/flash logic patches cells
  // after a write action's AJAX response, not just this polling refresh.
  function patch(nextRoot) {
    return RB.patchCells(root, nextRoot);
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
        if (statusEl) statusEl.textContent = i18n.update_failed;
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
