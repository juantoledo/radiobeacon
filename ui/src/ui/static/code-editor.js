"use strict";

/* Lightweight, dependency-free Python syntax highlighter for a plain
   <textarea class="code-editor"> (the CUSTOM adapter's snippet box on
   /config/adapters — see adapters.custom_adapter). No CDN, no build step,
   matching the rest of this UI (see style.css's header comment).

   Technique: a <pre><code> is painted directly behind the textarea, kept
   pixel-synced on every input/scroll; the textarea's own text is made
   transparent (only its native caret and selection still show), so typing,
   selecting, and undo/redo all stay exactly what the browser already does
   for a plain textarea — this only ever *reads* textarea.value, it never
   rewrites it (except the Tab-key handler below). See style.css's "Code
   editor" section for the layout half of this.

   Progressive enhancement: this file is optional. Without it (or before
   it runs) the bare <textarea class="code-editor"> from
   textarea.code-editor's own CSS rule already renders full-width,
   resizable, and perfectly editable — just unhighlighted. */
(function () {
  var KEYWORDS = new Set([
    "False", "None", "True", "and", "as", "assert", "async", "await",
    "break", "class", "continue", "def", "del", "elif", "else", "except",
    "finally", "for", "from", "global", "if", "import", "in", "is",
    "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try",
    "while", "with", "yield",
  ]);

  // Not exhaustive — just the names that show up constantly in this
  // repo's own CUSTOM snippets (HTTP/AWS-signed fetches, item mapping) and
  // the common exception types; anything else renders as plain text
  // rather than guessing.
  var BUILTINS = new Set([
    "self", "cls", "print", "len", "range", "isinstance", "issubclass",
    "str", "int", "float", "bool", "list", "dict", "set", "frozenset",
    "tuple", "type", "object", "super", "open", "enumerate", "zip", "map",
    "filter", "sorted", "reversed", "min", "max", "sum", "abs", "any",
    "all", "getattr", "setattr", "hasattr", "delattr", "iter", "next",
    "repr", "format", "vars", "callable", "staticmethod", "classmethod",
    "property", "Exception", "ValueError", "TypeError", "KeyError",
    "RuntimeError", "StopIteration", "AttributeError", "NotImplementedError",
    "OSError", "IOError", "ImportError",
  ]);

  // Tried in order at each position, before the identifier/gap fallback
  // below — so e.g. the word "class" inside a string or a comment never
  // gets colored as the keyword.
  var RULES = [
    { cls: "tok-com", re: /^#[^\n]*/ },
    { cls: "tok-str", re: /^(?:[rRbBuUfF]{1,3})?'''[\s\S]*?(?:'''|$)/ },
    { cls: "tok-str", re: /^(?:[rRbBuUfF]{1,3})?"""[\s\S]*?(?:"""|$)/ },
    { cls: "tok-str", re: /^(?:[rRbBuUfF]{1,3})?'(?:\\.|[^'\\\n])*'?/ },
    { cls: "tok-str", re: /^(?:[rRbBuUfF]{1,3})?"(?:\\.|[^"\\\n])*"?/ },
    { cls: "tok-deco", re: /^@[A-Za-z_][A-Za-z0-9_.]*/ },
    { cls: "tok-num", re: /^0[xX][0-9a-fA-F]+/ },
    { cls: "tok-num", re: /^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[jJ]?/ },
  ];
  var WORD_RE = /^[A-Za-z_][A-Za-z0-9_]*/;
  var GAP_RE = /^[^#'"@A-Za-z0-9_]+/;

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlightPython(src) {
    var out = [];
    var pos = 0;
    var len = src.length;
    // Tracks a just-seen "def"/"class" so the *next* identifier (the
    // function/class name) renders as tok-def instead of plain text.
    var prevKeyword = null;

    while (pos < len) {
      var rest = src.slice(pos);
      var ruleMatched = false;

      for (var i = 0; i < RULES.length; i++) {
        var m = RULES[i].re.exec(rest);
        if (m && m[0]) {
          out.push('<span class="' + RULES[i].cls + '">' + escapeHtml(m[0]) + "</span>");
          pos += m[0].length;
          prevKeyword = null;
          ruleMatched = true;
          break;
        }
      }
      if (ruleMatched) continue;

      var w = WORD_RE.exec(rest);
      if (w) {
        var word = w[0];
        var cls = null;
        if (KEYWORDS.has(word)) {
          cls = "tok-kw";
        } else if (prevKeyword === "def" || prevKeyword === "class") {
          cls = "tok-def";
        } else if (BUILTINS.has(word)) {
          cls = "tok-builtin";
        }
        out.push(cls ? '<span class="' + cls + '">' + escapeHtml(word) + "</span>" : escapeHtml(word));
        prevKeyword = KEYWORDS.has(word) ? word : null;
        pos += word.length;
        continue;
      }

      var g = GAP_RE.exec(rest);
      if (g && g[0]) {
        out.push(escapeHtml(g[0]));
        pos += g[0].length;
        continue;
      }

      // Safety net so the loop always advances even on an input shape
      // none of the rules above anticipated — one raw character at a
      // time is slow but never infinite-loops.
      out.push(escapeHtml(rest[0]));
      pos += 1;
    }
    return out.join("");
  }

  function enhance(textarea) {
    var wrap = document.createElement("div");
    wrap.className = "code-editor-wrap";
    textarea.parentNode.insertBefore(wrap, textarea);

    var pre = document.createElement("pre");
    pre.className = "code-editor-highlight";
    pre.setAttribute("aria-hidden", "true");
    var code = document.createElement("code");
    pre.appendChild(code);
    // textarea BEFORE pre in the DOM: style.css's focus ring uses a
    // `textarea:focus ~ pre` sibling selector, which only ever matches a
    // *later* sibling. Paint order still puts the (editable, on-top)
    // textarea above the (colored, background) pre via z-index, not DOM
    // order — see style.css.
    wrap.appendChild(textarea);
    wrap.appendChild(pre);

    textarea.spellcheck = false;
    textarea.setAttribute("autocapitalize", "off");
    textarea.setAttribute("autocorrect", "off");
    // No soft-wrap: Python indentation only reads correctly on one
    // logical line per visual line, matching the <pre>'s own `white-space:
    // pre` (see style.css) — long lines scroll horizontally instead.
    textarea.setAttribute("wrap", "off");

    function syncScroll() {
      pre.scrollTop = textarea.scrollTop;
      pre.scrollLeft = textarea.scrollLeft;
    }

    // Keeps `pre` at *exactly* the textarea's own rendered box size —
    // copied in pixels from offsetWidth/offsetHeight (its real border-box,
    // both use box-sizing: border-box) rather than left to CSS layout to
    // infer, so `pre` can never end up smaller than the textarea and clip
    // off the bottom of the visible, editable box. Runs on load, on every
    // manual resize-handle drag, and on window resize.
    function syncSize() {
      pre.style.width = textarea.offsetWidth + "px";
      pre.style.height = textarea.offsetHeight + "px";
    }

    function render() {
      var value = textarea.value;
      // A trailing "\n" needs a trailing space in the overlay, or <pre>
      // collapses it to no extra visual line and every line below the
      // last would drift out of sync with the (untouched) textarea.
      code.innerHTML = highlightPython(value) + (/\n$/.test(value) ? " " : "");
      syncScroll();
    }

    textarea.addEventListener("input", render);
    textarea.addEventListener("scroll", syncScroll);

    // Tab inserts 4 spaces (Python indentation) instead of moving focus
    // to the next form field, same as every real code editor.
    textarea.addEventListener("keydown", function (e) {
      if (e.key !== "Tab") return;
      e.preventDefault();
      var start = textarea.selectionStart;
      var end = textarea.selectionEnd;
      textarea.value = textarea.value.slice(0, start) + "    " + textarea.value.slice(end);
      textarea.selectionStart = textarea.selectionEnd = start + 4;
      render();
    });

    if (window.ResizeObserver) {
      new ResizeObserver(syncSize).observe(textarea);
    } else {
      window.addEventListener("resize", syncSize);
    }

    // Grow the box once, on load, to fit the existing snippet (capped at
    // 70% of the viewport height, so a 200-line file doesn't need
    // scrolling at all to review top-to-bottom) — never shrinks it, and
    // never runs again, so it can't fight a later manual resize-handle
    // drag. Below the cap, or for a short snippet, the CSS min-height /
    // browser's own scrolling handles it exactly as before.
    var wanted = textarea.scrollHeight + 2; // + the (now-transparent) border
    var cap = Math.round(window.innerHeight * 0.7);
    if (wanted > textarea.clientHeight) {
      textarea.style.height = Math.min(wanted, cap) + "px";
    }

    render();
    syncSize();
  }

  document.querySelectorAll("textarea.code-editor").forEach(enhance);
})();
