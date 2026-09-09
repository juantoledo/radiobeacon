// On-air transmission audio. While the SvxLink transmitter is keyed, plays
// the operator's live audio stream of what's going out (the UI proxies and
// fans it out at /dashboard/tx-stream — configured via
// UI_DASHBOARD_TX_STREAM_URL). Never polls: it listens for the `rb:tx-state`
// CustomEvent that on-air.js already broadcasts every 2s.
//
// Plays by default; the viewer can mute it, and that choice persists per
// browser (localStorage). The first autoplay-with-sound may be blocked by
// the browser until the viewer interacts with the page once — the rejected
// play() promise is swallowed and the next keyed edge (or a mute-toggle
// click, itself a user gesture) retries.
//
// Its own <audio id="tx-audio">, independent of #beacon-audio / the
// Activity-feed player — the only interaction is: a deliberate feed click
// pauses us (the operator asked for that clip).
(function () {
  "use strict";

  var script = document.currentScript;
  var audio = document.getElementById("tx-audio");
  var toggles = document.querySelectorAll(".tx-audio-toggle");
  var streamSrc = script && script.dataset.streamSrc;
  if (!audio || !toggles.length || !streamSrc) return;

  var STORE_KEY = "rb.txAudioMuted";
  var RETRY_MS = 3000;
  var i18n = {};
  try {
    i18n = JSON.parse(script.dataset.i18n || "{}");
  } catch (e) {
    i18n = {};
  }

  var muted = readMuted(); // absent / storage blocked -> false (plays by default)
  var wantPlaying = false; // last rb:tx-state said monitor_active && on_air
  var playing = false; // we've told <audio> to play the stream
  var retryTimer = null;

  syncToggles();
  audio.muted = muted;

  toggles.forEach(function (btn) {
    btn.addEventListener("click", function () {
      muted = !muted;
      writeMuted();
      audio.muted = muted;
      syncToggles();
      // this click is a genuine user gesture — safe to (re)start now
      if (!muted && wantPlaying && !playing) start();
    });
  });

  document.addEventListener("rb:tx-state", function (e) {
    var d = e.detail || {};
    wantPlaying = Boolean(d.monitor_active && d.on_air);
    if (wantPlaying) {
      if (!playing) start();
    } else {
      stop();
    }
  });

  // The Activity-feed player wins if the operator deliberately plays a clip.
  var feed = document.getElementById("beacon-audio");
  if (feed) feed.addEventListener("play", stop);

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop(); // on-air.js re-drives us via rb:tx-state on return
  });

  audio.addEventListener("ended", onDrop);
  audio.addEventListener("error", onDrop);

  function start() {
    clearTimeout(retryTimer);
    retryTimer = null;
    playing = true;
    audio.src = streamSrc + "?t=" + Date.now(); // fresh upstream connection
    audio.muted = muted;
    audio.play().catch(function () {
      /* autoplay blocked or the stream 404'd — retry on the next edge/gesture */
    });
  }

  function stop() {
    clearTimeout(retryTimer);
    retryTimer = null;
    playing = false;
    audio.pause();
    audio.removeAttribute("src"); // drops the proxied connection
  }

  // Upstream closed / stalled while we still want audio — reconnect shortly.
  function onDrop() {
    if (!playing) return;
    playing = false;
    audio.removeAttribute("src");
    if (wantPlaying && !retryTimer) {
      retryTimer = setTimeout(function () {
        retryTimer = null;
        if (wantPlaying) start();
      }, RETRY_MS);
    }
  }

  function readMuted() {
    try {
      return localStorage.getItem(STORE_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function writeMuted() {
    try {
      localStorage.setItem(STORE_KEY, muted ? "1" : "0");
    } catch (e) {
      /* private mode / storage disabled — preference just won't persist */
    }
  }

  function syncToggles() {
    var label = muted
      ? i18n.unmute || "Play transmission audio"
      : i18n.mute || "Mute transmission audio";
    toggles.forEach(function (btn) {
      btn.classList.toggle("is-muted", muted);
      btn.setAttribute("aria-pressed", String(!muted));
      btn.setAttribute("title", label);
      btn.setAttribute("aria-label", label);
    });
  }
})();
