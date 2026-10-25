/* Publikreach shell: phone menu toggle and stacked-table labels. Static file because the CSP blocks inline scripts. */
(function () {
  'use strict';

  function initMenu() {
    var nav = document.querySelector('nav.nav');
    var toggle = nav && nav.querySelector('.nav-toggle');
    if (!toggle) return;
    function setOpen(open) {
      if (open) nav.setAttribute('data-open', ''); else nav.removeAttribute('data-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    toggle.addEventListener('click', function () {
      setOpen(!nav.hasAttribute('data-open'));
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && nav.hasAttribute('data-open')) {
        setOpen(false);
        toggle.focus();
      }
    });
    var wide = window.matchMedia ? window.matchMedia('(min-width: 900px)') : null;
    if (wide) {
      var reset = function (event) { if (event.matches) setOpen(false); };
      if (wide.addEventListener) wide.addEventListener('change', reset);
      else if (wide.addListener) wide.addListener(reset);
    }
  }

  function headerText(th) {
    return (th.textContent || '').replace(/[↕↑↓]/g, '').replace(/\s+/g, ' ').trim();
  }

  function initTableLabels() {
    var tables = document.querySelectorAll('.crm-table, .en-table, .verification-table');
    Array.prototype.forEach.call(tables, function (table) {
      var head = table.querySelector('thead tr');
      if (!head) return;
      var labels = [];
      Array.prototype.forEach.call(head.children, function (th) {
        var span = parseInt(th.getAttribute('colspan') || '1', 10) || 1;
        var text = headerText(th);
        for (var i = 0; i < span; i += 1) labels.push(text);
      });
      Array.prototype.forEach.call(table.querySelectorAll('tbody tr'), function (row) {
        Array.prototype.forEach.call(row.children, function (cell, index) {
          if (cell.tagName !== 'TD' || cell.hasAttribute('data-label')) return;
          if (labels[index]) cell.setAttribute('data-label', labels[index]);
        });
      });
    });
  }

  function init() {
    initMenu();
    initTableLabels();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
