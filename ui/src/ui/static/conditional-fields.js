// Generic show/hide for <tr data-show-if="KEY=value&KEY2=value2"> rows —
// driven by SettingSpec.depends_on (see config_catalog.py) and rendered by
// _config_group_form_body.html. A no-op on any page with no such rows.
//
// A condition whose named control isn't present on the current page is
// ignored rather than treated as "hide" — the same spec's depends_on is
// reused across pages that only show some of the related fields together
// (e.g. an API key living in the Secrets group, its provider select living
// in a different group), so a missing control must fail open, not hide.
(function () {
  "use strict";

  function rowConditions(row) {
    return row.getAttribute("data-show-if").split("&").map(function (pair) {
      var parts = pair.split("=");
      return { key: parts[0], value: parts[1] };
    });
  }

  function evaluate(row) {
    var visible = rowConditions(row).every(function (cond) {
      var control = document.getElementById(cond.key);
      if (!control) return true; // not on this page — don't hide
      return control.value === cond.value;
    });
    row.hidden = !visible;
  }

  function wire() {
    var rows = document.querySelectorAll("tr[data-show-if]");
    if (!rows.length) return;

    var seen = {};
    rows.forEach(function (row) {
      rowConditions(row).forEach(function (cond) {
        seen[cond.key] = true;
      });
    });

    Object.keys(seen).forEach(function (key) {
      var control = document.getElementById(key);
      if (!control) return;
      control.addEventListener("change", function () {
        rows.forEach(evaluate);
      });
    });

    rows.forEach(evaluate);
  }

  document.addEventListener("DOMContentLoaded", wire);
})();
