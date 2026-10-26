/* Publikreach analytics page behavior. External file because the CSP forbids inline scripts. */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  function initTabs(root) {
    var tabs = Array.prototype.slice.call(root.querySelectorAll('[data-tab]'));
    var panels = Array.prototype.slice.call(root.querySelectorAll('[data-panel]'));
    function select(key, focus) {
      tabs.forEach(function (tab) {
        var on = tab.getAttribute('data-tab') === key;
        tab.classList.toggle('is-active', on);
        tab.setAttribute('aria-selected', on ? 'true' : 'false');
        tab.tabIndex = on ? 0 : -1;
        if (on && focus) tab.focus();
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.getAttribute('data-panel') !== key;
      });
    }
    tabs.forEach(function (tab, index) {
      tab.addEventListener('click', function () { select(tab.getAttribute('data-tab'), false); });
      tab.addEventListener('keydown', function (event) {
        var next = null;
        if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
        else if (event.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
        else if (event.key === 'Home') next = tabs[0];
        else if (event.key === 'End') next = tabs[tabs.length - 1];
        if (next) { event.preventDefault(); select(next.getAttribute('data-tab'), true); }
      });
    });
    var active = root.querySelector('[data-tab].is-active');
    if (active) select(active.getAttribute('data-tab'), false);
  }

  function initDims(root) {
    var buttons = Array.prototype.slice.call(root.querySelectorAll('[data-dim-btn]'));
    var panels = Array.prototype.slice.call(root.querySelectorAll('[data-dim-panel]'));
    var rows = Array.prototype.slice.call(root.querySelectorAll('tr[data-dim]'));
    function select(key) {
      buttons.forEach(function (button) {
        var on = button.getAttribute('data-dim-btn') === key;
        button.classList.toggle('is-active', on);
        button.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      panels.forEach(function (panel) { panel.hidden = panel.getAttribute('data-dim-panel') !== key; });
      rows.forEach(function (row) { row.hidden = row.getAttribute('data-dim') !== key; });
    }
    buttons.forEach(function (button) {
      button.addEventListener('click', function () { select(button.getAttribute('data-dim-btn')); });
    });
  }

  function initRowLinks() {
    document.querySelectorAll('tr[data-href]').forEach(function (row) {
      row.addEventListener('click', function (event) {
        if (event.target.closest('a, button, input, select, textarea, summary')) return;
        var href = row.getAttribute('data-href');
        if (!href) return;
        if (event.metaKey || event.ctrlKey) window.open(href, '_blank', 'noopener');
        else window.location.assign(href);
      });
    });
  }

  ready(function () {
    document.querySelectorAll('[data-an-tabs]').forEach(initTabs);
    document.querySelectorAll('[data-an-dims]').forEach(initDims);
    initRowLinks();
    if (window.location.hash === '#tracking-card') {
      var card = document.getElementById('tracking-card');
      if (card) card.open = true;
    }
  });
})();
