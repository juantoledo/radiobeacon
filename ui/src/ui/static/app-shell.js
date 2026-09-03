// Mobile navigation drawer for the app shell (see base.html). Plain DOM,
// no framework, no build step — matches the rest of this app. On desktop
// (>=961px) the sidebar is always visible and this is inert.
(function () {
  "use strict";

  var body = document.body;
  var toggle = document.querySelector(".nav-toggle");
  var closeBtn = document.querySelector(".nav-close");
  var scrim = document.querySelector(".nav-scrim");
  var sidebar = document.getElementById("sidebar");
  if (!toggle || !sidebar) return;

  function open() {
    body.classList.add("nav-open");
    toggle.setAttribute("aria-expanded", "true");
    if (scrim) scrim.hidden = false;
  }

  function close() {
    body.classList.remove("nav-open");
    toggle.setAttribute("aria-expanded", "false");
    if (scrim) scrim.hidden = true;
  }

  toggle.addEventListener("click", function () {
    body.classList.contains("nav-open") ? close() : open();
  });
  if (closeBtn) closeBtn.addEventListener("click", close);
  if (scrim) scrim.addEventListener("click", close);

  // Close after following an in-drawer link, and on Escape.
  sidebar.addEventListener("click", function (e) {
    if (e.target.closest("a")) close();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") close();
  });

  // If the viewport grows past the breakpoint while the drawer is open,
  // drop the open state so it doesn't linger as a fixed overlay.
  var mq = window.matchMedia("(min-width: 961px)");
  (mq.addEventListener ? mq.addEventListener.bind(mq, "change") : mq.addListener.bind(mq))(function (ev) {
    if (ev.matches) close();
  });
})();
