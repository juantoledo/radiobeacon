// Live "ON AIR" indicator. Polls /dashboard/tx-state (a ~50-byte JSON the
// server derives from beacon.tx_monitor's SvxLink log tail) on its own
// short timer — deliberately separate from dashboard-refresh.js's 5s
// fragment poll so the glow reacts within ~2s and a brief frame
// transmission isn't missed.
//
// When the monitor isn't reporting (SvxLink unreachable, not configured,
// log unreadable) both flags come back false and this does nothing
// visible — no glow, no pill, no error. Plain fetch, no framework.
//
// Each poll result is also re-broadcast as a `rb:tx-state` CustomEvent so
// tx-audio.js can react to the keyed edge without a second poller. The
// event fires whether or not that feature is loaded (no listener = no-op).
(function () {
  "use strict";

  var script = document.currentScript;
  var intervalMs = parseInt(script.dataset.intervalMs, 10);
  if (!(intervalMs > 0)) return;

  var pill = document.getElementById("on-air-indicator");
  var timer = null;

  var IDLE = { on_air: false, monitor_active: false };

  function apply(onAir) {
    document.body.classList.toggle("is-on-air", onAir);
    if (pill) pill.hidden = !onAir;
  }

  function broadcast(data) {
    document.dispatchEvent(new CustomEvent("rb:tx-state", { detail: data }));
  }

  function poll() {
    if (document.hidden) return;
    fetch("/dashboard/tx-state")
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        data = data || IDLE;
        apply(Boolean(data.monitor_active && data.on_air));
        broadcast(data);
      })
      .catch(function () {
        apply(false); // fail safe — never leave a stale glow on
        broadcast(IDLE);
      })
      .finally(function () {
        clearTimeout(timer);
        timer = setTimeout(poll, intervalMs);
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });

  timer = setTimeout(poll, intervalMs);
})();
