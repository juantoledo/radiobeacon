// Instant client-side flip for the theme switcher in base.html. Clicking
// a theme button today still round-trips through ajax-forms.js's fetch
// to /theme (see ui/routers/theme.py) to actually persist the choice in
// the cookie — that's real work this script does not replace. But the
// entire *visible* effect of a theme switch is one attribute on <html>
// plus which button looks active, so there's no reason to make the
// visitor wait on the network for that part.
//
// This deliberately never calls event.preventDefault() — ajax-forms.js's
// own document-level submit listener still runs (submit listeners don't
// cancel each other just by one of them calling preventDefault) and is
// what actually sends the request and sets the cookie. This script only
// paints the change immediately, before/in parallel with that.
(function () {
  "use strict";

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.classList.contains("theme-switcher")) return;

    var button = e.submitter;
    if (!button || button.name !== "theme") return;
    var theme = button.value;

    // "system" is the sentinel for "no explicit theme, follow OS" — see
    // base.html, which likewise omits data-theme entirely for it.
    if (theme === "system") {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = theme;
    }

    form.querySelectorAll("button[name='theme']").forEach(function (btn) {
      var active = btn === button;
      btn.classList.toggle("active", active);
      if (active) {
        btn.setAttribute("aria-current", "true");
      } else {
        btn.removeAttribute("aria-current");
      }
    });
  });
})();
