// Progressive-enhancement AJAX submit handler for every <form method="post">
// in the app. Forms need no markup change to opt in beyond staying exactly
// as they are (method="post" action="...") — that's also the no-JS
// fallback, so this file only ever intercepts a submit, never replaces the
// underlying form/action. A route opts in server-side by branching on
// ui.ajax.is_ajax(request) and returning ui.ajax.ajax_ok/ajax_error instead
// of its usual redirect/re-render.
//
// Response contract (see ui/ajax.py):
//   {"ok": true,  "message": "...", "fragment"?: "<html>", "target"?: "#sel", "redirect"?: "/url"}
//   {"ok": false, "message": "...", "fragment"?: "<html>", "target"?: "#sel"}
//
// "fragment" with a "target" replaces that element's innerHTML outright
// (the validation-error-re-render case: a form body partial). "fragment"
// with no "target" is treated as a bag of [data-cell] regions to patch
// wherever they already exist on the current page — the same convention
// dashboard-refresh.js's polling refresh uses — so RB.patchCells is shared
// between the two files rather than duplicated.
//
// Opt a specific form out (rare) with `data-no-ajax` — it then submits and
// navigates exactly as if this script weren't loaded.
window.RB = window.RB || {};

(function () {
  "use strict";

  function getCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : null;
  }

  // Diffs by rendered HTML string so only cells that actually changed
  // repaint — no full-subtree replace, no lost scroll position/focus/hover.
  // `root` is searched for the current `[data-cell]` elements; `nextRoot`
  // (an Element or a whole parsed Document) supplies their replacements.
  RB.patchCells = function (root, nextRoot) {
    var changed = 0;
    nextRoot.querySelectorAll("[data-cell]").forEach(function (nextCell) {
      var name = nextCell.getAttribute("data-cell");
      var cur = root.querySelector('[data-cell="' + CSS.escape(name) + '"]');
      if (!cur) return;
      if (cur.innerHTML.trim() !== nextCell.innerHTML.trim()) {
        cur.innerHTML = nextCell.innerHTML;
        changed++;
      }
      // width-style cells (e.g. the queue bar) carry their value in an attribute
      if (nextCell.style.cssText && nextCell.style.cssText !== cur.style.cssText) {
        cur.style.cssText = nextCell.style.cssText;
      }
    });
    return changed;
  };

  function toast(message, ok) {
    var region = document.getElementById("toast-region");
    if (!region || !message) return;
    var el = document.createElement("p");
    el.className = (ok ? "banner" : "banner-warn") + " toast";
    el.textContent = (ok ? "✓ " : "⚠ ") + message;
    region.appendChild(el);
    setTimeout(function () {
      el.classList.add("toast-out");
      setTimeout(function () {
        el.remove();
      }, 200);
    }, 4000);
  }

  function applyFragment(fragment, target) {
    if (!fragment) return;
    var parsed = new DOMParser().parseFromString(fragment, "text/html");
    if (target) {
      var host = document.querySelector(target);
      var next = parsed.querySelector(target);
      if (host && next) host.innerHTML = next.innerHTML;
      return;
    }
    RB.patchCells(document, parsed);
  }

  function focusFirstInvalid(target) {
    if (!target) return;
    var host = document.querySelector(target);
    var field = host && host.querySelector("[aria-invalid='true'], .field-error");
    if (field) field.focus && field.focus();
  }

  function submitAjax(form, submitter) {
    var data = new FormData(form, submitter || undefined);
    var csrf = getCookie("csrf_token");
    var action = (submitter && submitter.hasAttribute("formaction"))
      ? submitter.formAction
      : form.getAttribute("action") || window.location.pathname;
    var method = (submitter && submitter.hasAttribute("formmethod"))
      ? submitter.formMethod.toUpperCase()
      : (form.getAttribute("method") || "post").toUpperCase();

    fetch(action, {
      method: method,
      body: data,
      headers: {
        "X-Requested-With": "fetch",
        "X-CSRF-Token": csrf || "",
      },
    })
      .then(function (res) {
        var contentType = res.headers.get("content-type") || "";
        if (contentType.indexOf("application/json") === -1) {
          // This route hasn't been converted to the JSON envelope yet
          // (see ui/ajax.py) — fetch already followed any redirect, so
          // `res` is whatever the browser would have ended up showing
          // for a plain form submission. The write already happened;
          // render that response's own body in place of the current
          // page rather than issuing a second request for it (a fresh
          // GET at this URL may 404/405, or lose an error re-render's
          // inline state — this is a full drop-in replacement instead).
          return res.text().then(function (html) {
            history.pushState({}, "", res.url);
            document.open();
            document.write(html);
            document.close();
          });
        }
        return res.json().then(function (body) {
          if (body.fragment) applyFragment(body.fragment, body.target);
          toast(body.message, !!body.ok);
          if (body.ok && body.redirect) {
            window.location.assign(body.redirect);
          } else if (!body.ok) {
            focusFirstInvalid(body.target);
          }
          // Bubbling CustomEvent, same rb: convention as on-air.js's
          // rb:tx-state — lets a form living inside e.g. a <dialog> react
          // to its own submit outcome (close on success) without this file
          // needing to know that dialogs exist.
          form.dispatchEvent(new CustomEvent("rb:ajax-response", { detail: body, bubbles: true }));
        });
      })
      .catch(function () {
        toast("Request failed — check your connection and try again.", false);
      });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-no-ajax")) return;
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    e.preventDefault();
    submitAjax(form, e.submitter);
  });
})();
