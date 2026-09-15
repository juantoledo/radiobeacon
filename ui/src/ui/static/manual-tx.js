// "Transmit now" modal on the dashboard. Opens the <dialog>, keeps a live
// length counter (characters for voice, UTF-8 bytes for frame — matching
// what the server enforces), and blocks submit while the message is empty
// or over the limit. The form posts via ajax-forms.js (see
// ui/routers/manual_tx.py's is_ajax branch) rather than navigating, so
// this file also closes the dialog itself on a successful rb:ajax-response
// — nothing else would, since the page is never replaced/reloaded.
(function () {
  "use strict";

  var dialog = document.getElementById("manual-tx-dialog");
  if (!dialog) return;

  // Translated via the form's data-i18n JSON blob (rendered from
  // ui/src/ui/translations/{en,es}/items.json's "manual_tx.*" keys — see
  // dashboard.html), same convention as dashboard-refresh.js's own
  // data-i18n attribute. English fallback covers a form rendered without
  // the attribute (shouldn't happen, but never crash the counter over it).
  var MANUAL_TX_FALLBACK = {
    count_chars: "{used} / {max} chars",
    count_bytes: "{used} / {max} bytes"
  };
  var manualTxStrings = MANUAL_TX_FALLBACK;
  try {
    var dialogForm = dialog.querySelector("form");
    if (dialogForm && dialogForm.dataset.i18n) {
      manualTxStrings = JSON.parse(dialogForm.dataset.i18n);
    }
  } catch (e) {
    manualTxStrings = MANUAL_TX_FALLBACK;
  }

  function formatCount(used, max, isFrame) {
    var template = isFrame ? manualTxStrings.count_bytes : manualTxStrings.count_chars;
    return template.replace("{used}", used).replace("{max}", max);
  }

  var form = dialog.querySelector("form");
  var textarea = form.querySelector("#manual-tx-text");
  var countEl = form.querySelector("#manual-tx-count");
  var overEl = form.querySelector("#manual-tx-over");
  var submit = form.querySelector("#manual-tx-submit");
  var modeNote = form.querySelector(".mtx-mode-note");
  var kindInputs = form.querySelectorAll('input[name="kind"]');

  var voiceMax = parseInt(form.dataset.voiceMax, 10) || 0;
  var frameMax = parseInt(form.dataset.frameMax, 10) || 0;
  var activeMode = form.querySelector(".mtx-kind label.active input")
    ? form.querySelector(".mtx-kind label.active input").value
    : "voice";

  var encoder = "TextEncoder" in window ? new TextEncoder() : null;

  function selectedKind() {
    var checked = form.querySelector('input[name="kind"]:checked');
    return checked ? checked.value : "voice";
  }

  function update() {
    var kind = selectedKind();
    var isFrame = kind === "frame";
    var max = isFrame ? frameMax : voiceMax;
    var value = textarea.value;
    var used = isFrame && encoder ? encoder.encode(value).length : value.length;

    countEl.textContent = formatCount(used, max, isFrame);
    var over = used > max;
    overEl.hidden = !over;
    countEl.classList.toggle("mtx-count-over", over);
    submit.disabled = over || value.trim().length === 0;

    // Reflect the radio choice on its label, and flag a mismatch with the
    // beacon's current mode.
    form.querySelectorAll(".mtx-kind label").forEach(function (label) {
      label.classList.toggle("active", label.querySelector("input").checked);
    });
    if (modeNote) modeNote.hidden = kind === activeMode;
  }

  function open() {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    update();
    textarea.focus();
  }

  function close() {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  document.addEventListener("click", function (e) {
    var opener = e.target.closest('[data-open-dialog="manual-tx-dialog"]');
    if (opener && !opener.disabled) {
      e.preventDefault();
      open();
      return;
    }
    if (e.target.closest("[data-close-dialog]")) {
      e.preventDefault();
      close();
    }
  });

  // Click on the ::backdrop (the dialog element itself, outside the form).
  dialog.addEventListener("click", function (e) {
    if (e.target === dialog) close();
  });

  textarea.addEventListener("input", update);
  kindInputs.forEach(function (input) {
    input.addEventListener("change", update);
  });

  // Dispatched by ajax-forms.js after this form's fetch resolves (see
  // rb:ajax-response there). On success, close the dialog and clear the
  // message so the next open starts fresh — on failure (e.g. validation),
  // leave everything as-is so the operator can fix and resubmit without
  // retyping.
  form.addEventListener("rb:ajax-response", function (e) {
    if (!e.detail || !e.detail.ok) return;
    close();
    textarea.value = "";
    update();
  });
})();
