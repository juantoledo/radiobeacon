// Pan/zoom crop tool for the logo upload forms (/config/branding and the
// setup wizard's "branding" step). Locked to a square (1:1) crop — see
// ui.branding's server-side pipeline, which already caps/normalizes to a
// square-friendly 512x512 max, so this just lets the operator choose
// *which* 512x512-ish region of their source image to keep, rather than
// an arbitrary top-left crop.
//
// Deliberately does NOT intercept the form's submit or touch its action/
// method/enctype: instead, every time the crop changes (drag release,
// zoom release), the cropped result is baked directly into the real
// <input type="file">'s .files (via DataTransfer — supported in every
// evergreen browser) so the form's own native submission — or
// ajax-forms.js's enhanced one, whichever a given page uses — carries the
// cropped image with zero changes to either. If this script never runs,
// the file input still holds whatever the operator originally picked,
// and the server-side pipeline (ui.branding.save_logo) validates/
// normalizes that exactly as it always has — cropping is a pure
// enhancement, never a requirement to upload at all.
(function () {
  "use strict";

  var FRAME = 260; // on-screen crop viewport, CSS px (also the canvas's own size)
  var EXPORT_SIZE = 512; // matches ui.branding.MAX_DIMENSION

  function dataUrlToBlob(dataUrl) {
    var parts = dataUrl.split(",");
    var mime = parts[0].match(/:(.*?);/)[1];
    var binary = atob(parts[1]);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }

  function initEditor(root) {
    var fileInput = root.querySelector('input[type="file"]');
    var stage = root.querySelector(".logo-editor-stage");
    var canvas = root.querySelector(".logo-editor-canvas");
    var zoomSlider = root.querySelector(".logo-editor-zoom");
    if (!fileInput || !stage || !canvas || !zoomSlider) return;
    var ctx = canvas.getContext("2d");

    var img = null;
    var baseline = 1; // cover-fit scale: shortest side exactly fills FRAME
    var offsetX = 0;
    var offsetY = 0;
    var dragging = false;
    var dragStartX = 0;
    var dragStartY = 0;
    var dragOffsetX = 0;
    var dragOffsetY = 0;

    function effectiveScale() {
      return baseline * parseFloat(zoomSlider.value);
    }

    function clampOffsets() {
      var scale = effectiveScale();
      var dispW = img.naturalWidth * scale;
      var dispH = img.naturalHeight * scale;
      offsetX = Math.min(0, Math.max(FRAME - dispW, offsetX));
      offsetY = Math.min(0, Math.max(FRAME - dispH, offsetY));
    }

    function redraw() {
      ctx.clearRect(0, 0, FRAME, FRAME);
      var scale = effectiveScale();
      ctx.drawImage(
        img, offsetX, offsetY, img.naturalWidth * scale, img.naturalHeight * scale
      );
    }

    // Bakes the current crop into fileInput.files, replacing whatever it
    // held (the originally-picked file, or a previous crop). Synchronous
    // (toDataURL, not the async toBlob) so it can run directly from an
    // event handler with no callback juggling.
    function updateFileInput() {
      var exportCanvas = document.createElement("canvas");
      exportCanvas.width = EXPORT_SIZE;
      exportCanvas.height = EXPORT_SIZE;
      var exportScale = EXPORT_SIZE / FRAME;
      var scale = effectiveScale() * exportScale;
      exportCanvas
        .getContext("2d")
        .drawImage(
          img, offsetX * exportScale, offsetY * exportScale,
          img.naturalWidth * scale, img.naturalHeight * scale
        );
      var blob = dataUrlToBlob(exportCanvas.toDataURL("image/png"));
      var file = new File([blob], "logo.png", { type: "image/png" });
      var dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
    }

    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      if (!file) return;
      var url = URL.createObjectURL(file);
      var next = new Image();
      next.onload = function () {
        URL.revokeObjectURL(url);
        img = next;
        baseline = Math.max(FRAME / img.naturalWidth, FRAME / img.naturalHeight);
        zoomSlider.value = "1";
        offsetX = (FRAME - img.naturalWidth * baseline) / 2;
        offsetY = (FRAME - img.naturalHeight * baseline) / 2;
        stage.hidden = false;
        redraw();
        // Bakes the square cover-fit crop in immediately, so a source
        // that's already fine as-is doesn't require touching pan/zoom
        // at all before submitting.
        updateFileInput();
      };
      next.src = url;
    });

    canvas.addEventListener("pointerdown", function (e) {
      if (!img) return;
      dragging = true;
      canvas.setPointerCapture(e.pointerId);
      canvas.classList.add("dragging");
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      dragOffsetX = offsetX;
      dragOffsetY = offsetY;
    });
    canvas.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      offsetX = dragOffsetX + (e.clientX - dragStartX);
      offsetY = dragOffsetY + (e.clientY - dragStartY);
      clampOffsets();
      redraw();
    });
    function endDrag() {
      if (!dragging) return;
      dragging = false;
      canvas.classList.remove("dragging");
      updateFileInput();
    }
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

    zoomSlider.addEventListener("input", function () {
      if (!img) return;
      clampOffsets();
      redraw();
    });
    zoomSlider.addEventListener("change", function () {
      if (!img) return;
      updateFileInput();
    });
  }

  document.querySelectorAll("[data-logo-editor]").forEach(initEditor);
})();
