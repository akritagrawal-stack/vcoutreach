/* Publikreach glass boards: pointer tilt (at most 5 degrees) with a moving shine on KPI tiles and chart cards.
   Only a fine mouse pointer tilts. Touch, pen and prefers-reduced-motion leave the boards flat. No network, no libraries. */
(function () {
  "use strict";
  var MAX_TILT = 5;
  var SELECTOR = ".glass-board, .pk-kpi, .kpi";
  var mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };

  function reset(el) {
    el.classList.remove("is-tilting");
    el.style.removeProperty("--tilt-rx");
    el.style.removeProperty("--tilt-ry");
    el.style.removeProperty("--tilt-x");
    el.style.removeProperty("--mx");
    el.style.removeProperty("--my");
  }

  function onMove(ev) {
    if (ev.pointerType !== "mouse" || mq.matches) { return; }
    var el = ev.currentTarget;
    var r = el.getBoundingClientRect();
    if (!r.width || !r.height) { return; }
    var x = (ev.clientX - r.left) / r.width;
    var y = (ev.clientY - r.top) / r.height;
    // Big cards tilt less than small tiles so wide charts never look skewed.
    var cap = r.width > 480 ? MAX_TILT / 2 : MAX_TILT;
    el.style.setProperty("--tilt-ry", ((x - 0.5) * 2 * cap).toFixed(2) + "deg");
    el.style.setProperty("--tilt-rx", ((0.5 - y) * 2 * cap).toFixed(2) + "deg");
    el.style.setProperty("--tilt-x", ((x - 0.5) * 2).toFixed(2));
    el.style.setProperty("--mx", (x * 100).toFixed(1) + "%");
    el.style.setProperty("--my", (y * 100).toFixed(1) + "%");
    el.classList.add("is-tilting");
  }

  function boot() {
    var boards = document.querySelectorAll(SELECTOR);
    for (var i = 0; i < boards.length; i++) {
      boards[i].addEventListener("pointermove", onMove);
      boards[i].addEventListener("pointerleave", function (ev) { reset(ev.currentTarget); });
    }
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", boot); } else { boot(); }
})();
