/* Publikreach visual layer: hover, touch and keyboard reading for the daily sends and replies chart.
   The chart is already drawn by the server as inline SVG; this only adds the tooltip. No network, no libraries. */
(function () {
  "use strict";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function nums(text) {
    return String(text || "").split(",").map(function (v) { return parseInt(v, 10) || 0; });
  }
  function label(iso) {
    var p = String(iso).split("-");
    if (p.length < 3) { return iso; }
    return MONTHS[(parseInt(p[1], 10) || 1) - 1] + " " + (parseInt(p[2], 10) || 0);
  }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }

  function init(plot) {
    var days = String(plot.getAttribute("data-days") || "").split(",");
    var sent = nums(plot.getAttribute("data-sent"));
    var replied = nums(plot.getAttribute("data-replied"));
    var n = days.length;
    var hover = plot.querySelector(".vz-hover");
    var tip = plot.querySelector(".vz-tip");
    if (n < 2 || !hover || !tip) { return; }

    var top = 0;
    sent.concat(replied).forEach(function (v) { if (v > top) { top = v; } });
    var cands = [4, 6, 8, 10, 20, 30, 40, 50, 60, 80, 100, 150, 200, 300, 400, 500, 600, 800, 1000, 2000, 5000, 10000];
    var scale = 0;
    for (var i = 0; i < cands.length && !scale; i++) { if (cands[i] >= top) { scale = cands[i]; } }
    if (!scale) { scale = Math.ceil(top / 1000) * 1000; }

    var current = -1;
    // Markers are SVG squares in an overlay without a viewBox: x and y take percentages, the transform centers them.
    function place(mark, x, yPct) {
      mark.setAttribute("x", x);
      mark.setAttribute("y", yPct.toFixed(2) + "%");
    }
    function show(idx) {
      idx = Math.max(0, Math.min(n - 1, idx));
      current = idx;
      var pct = idx * 100 / (n - 1);
      var x = pct + "%";
      var rule = hover.querySelector(".vz-hover__rule");
      if (rule) { rule.setAttribute("x1", x); rule.setAttribute("x2", x); }
      var dots = hover.querySelectorAll(".vz-end");
      if (dots[0]) { place(dots[0], x, 100 - sent[idx] / scale * 100); }
      if (dots[1]) { place(dots[1], x, 100 - replied[idx] / scale * 100); }
      hover.classList.add("is-on");
      tip.innerHTML = "";
      var head = document.createElement("strong");
      head.textContent = label(days[idx]);
      var a = document.createElement("span");
      a.appendChild(document.createTextNode("Sent"));
      var ab = document.createElement("b"); ab.textContent = String(sent[idx]); a.appendChild(ab);
      var b = document.createElement("span");
      b.appendChild(document.createTextNode("Replied"));
      var bb = document.createElement("b"); bb.textContent = String(replied[idx]); b.appendChild(bb);
      tip.appendChild(head); tip.appendChild(a); tip.appendChild(b);
      tip.setAttribute("aria-label", label(days[idx]) + ": " + plural(sent[idx], "email", "emails") + " sent, " + plural(replied[idx], "reply", "replies"));
      tip.style.setProperty("--vz-tip-x", pct + "%");
      tip.classList.toggle("is-left", pct > 62);
      tip.classList.add("is-on");
    }
    function hide() {
      current = -1;
      hover.classList.remove("is-on");
      tip.classList.remove("is-on");
    }
    function fromPointer(ev) {
      var r = plot.getBoundingClientRect();
      if (!r.width) { return; }
      var f = (ev.clientX - r.left) / r.width;
      show(Math.round(Math.max(0, Math.min(1, f)) * (n - 1)));
    }

    plot.addEventListener("pointermove", fromPointer);
    plot.addEventListener("pointerdown", fromPointer);
    plot.addEventListener("pointerleave", function () { if (document.activeElement !== plot) { hide(); } });
    plot.addEventListener("blur", hide);
    plot.addEventListener("focus", function () { if (current < 0) { show(n - 1); } });
    plot.addEventListener("keydown", function (ev) {
      var k = ev.key;
      if (k === "ArrowLeft") { show((current < 0 ? n : current) - 1); }
      else if (k === "ArrowRight") { show((current < 0 ? -1 : current) + 1); }
      else if (k === "Home") { show(0); }
      else if (k === "End") { show(n - 1); }
      else if (k === "Escape") { hide(); return; }
      else { return; }
      ev.preventDefault();
    });
  }

  function boot() {
    var plots = document.querySelectorAll("[data-vz-chart]");
    for (var i = 0; i < plots.length; i++) { init(plots[i]); }
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", boot); } else { boot(); }
})();
