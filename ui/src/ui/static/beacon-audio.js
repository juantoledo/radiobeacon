// Per-item audio playback for the dashboard Activity feed. Each feed row
// that has a rendered voice clip carries a <button class="feed-play"
// data-audio-url="...">; clicking it plays that clip through a single
// shared <audio id="beacon-audio"> (declared in dashboard.html, outside
// #dashboard-content so the auto-refresh can't destroy it mid-play).
// Clicking the active row again stops it. Plain event delegation — the
// buttons are re-rendered whenever dashboard-refresh.js patches the feed,
// so nothing is bound to them directly.
(function () {
  "use strict";

  var audio = document.getElementById("beacon-audio");
  if (!audio) return;

  var playingUrl = null;

  function syncButtons() {
    document.querySelectorAll(".feed-play").forEach(function (btn) {
      var on = playingUrl !== null && btn.dataset.audioUrl === playingUrl;
      btn.classList.toggle("is-playing", on);
      btn.setAttribute("aria-label", on ? "Stop bulletin audio" : "Play bulletin audio");
    });
  }

  function stop() {
    audio.pause();
    audio.removeAttribute("src");
    playingUrl = null;
    syncButtons();
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".feed-play");
    if (!btn) return;
    e.preventDefault();
    var url = btn.dataset.audioUrl;
    if (playingUrl === url) {
      stop();
      return;
    }
    playingUrl = url;
    audio.src = url;
    audio.play().catch(stop);
    syncButtons();
  });

  audio.addEventListener("ended", stop);
  audio.addEventListener("error", stop);

  // Cells inside #dashboard-content (the Items feed, the Recent manual
  // transmissions list) have their innerHTML swapped on every auto-refresh;
  // re-mark the active button (if its row is still there) once the new
  // nodes land.
  var live = document.getElementById("dashboard-content");
  if (live && "MutationObserver" in window) {
    new MutationObserver(syncButtons).observe(live, { childList: true, subtree: true });
  }
})();
