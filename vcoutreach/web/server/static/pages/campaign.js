/* Campaign and draft editor behavior. Loaded only by crm_drafts.html and crm_draft.html.
   No network calls and nothing here sends mail: it toggles collapsed steps, makes table rows clickable,
   confirms a rewrite of existing drafts, and keeps the editor preview in step with the fields. */
(function () {
  'use strict';

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  /* Collapsed steps: an Edit/Open link opens the step in place. A hash link to a step (or to something inside one) opens it too. */
  function openStep(step) {
    if (!step) { return; }
    step.classList.add('cmp-step--open');
    var link = qs('[data-step-edit]', step);
    if (link) { link.setAttribute('aria-expanded', 'true'); }
  }

  function stepForHash() {
    var id = (window.location.hash || '').replace(/^#/, '');
    if (!id) { return null; }
    var el = null;
    try { el = document.getElementById(decodeURIComponent(id)); } catch (e) { el = document.getElementById(id); }
    return el ? el.closest('.cmp-step') : null;
  }

  function initSteps() {
    qsa('[data-step-edit]').forEach(function (link) {
      link.setAttribute('aria-expanded', 'false');
      link.addEventListener('click', function (event) {
        var step = document.getElementById(link.getAttribute('data-step-edit'));
        if (!step) { return; }
        event.preventDefault();
        if (step.classList.contains('cmp-step--open')) {
          step.classList.remove('cmp-step--open');
          link.setAttribute('aria-expanded', 'false');
        } else {
          openStep(step);
          if (step.scrollIntoView) { step.scrollIntoView({ block: 'start' }); }
        }
      });
    });
    var initial = stepForHash();
    if (initial) { openStep(initial); }
    window.addEventListener('hashchange', function () { openStep(stepForHash()); });
    /* In-page links such as "Sender profile saved" point at a step that may be collapsed. */
    document.addEventListener('click', function (event) {
      var a = event.target.closest ? event.target.closest('a[href^="#"]') : null;
      if (!a || a.hasAttribute('data-step-edit')) { return; }
      var id = a.getAttribute('href').slice(1);
      var target = id ? document.getElementById(id) : null;
      var step = target ? target.closest('.cmp-step') : null;
      if (step && !step.classList.contains('cmp-step--open')) { openStep(step); }
    });
  }

  /* Whole-row click on the drafts table. Links, buttons and form controls keep their own behavior. */
  function initRows() {
    document.addEventListener('click', function (event) {
      var row = event.target.closest ? event.target.closest('tr[data-href]') : null;
      if (!row) { return; }
      if (event.target.closest('a, button, input, select, textarea, label, summary')) { return; }
      var sel = window.getSelection ? String(window.getSelection()) : '';
      if (sel) { return; }
      var href = row.getAttribute('data-href');
      if (href && href.charAt(0) === '/') { window.location.assign(href); }
    });
    qsa('tr[data-href]').forEach(function (row) {
      row.setAttribute('tabindex', '0');
      row.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' && event.target === row) {
          var href = row.getAttribute('data-href');
          if (href && href.charAt(0) === '/') { window.location.assign(href); }
        }
      });
    });
  }

  /* Rewriting existing drafts needs a confirm. Capture phase so app.js data-busy never disables the button on a cancel. */
  function initOverwriteConfirm() {
    document.addEventListener('submit', function (event) {
      var form = event.target;
      if (!form || !form.matches || !form.matches('form.cmp-write')) { return; }
      var box = qs('input[name="overwrite"]', form);
      var existing = parseInt(form.getAttribute('data-existing') || '0', 10);
      if (box && box.checked && existing > 0) {
        var msg = 'Replace ' + existing + (existing === 1 ? ' existing draft' : ' existing drafts') + '? Your edits to them will be lost.';
        if (!window.confirm(msg)) {
          event.preventDefault();
          event.stopImmediatePropagation();
        }
      }
    }, true);
  }

  /* Draft editor: live preview and Approve guard. */
  function paragraphs(text) {
    return String(text || '').split(/\n\s*\n/).map(function (p) { return p.trim(); }).filter(Boolean);
  }

  function initEditor() {
    var subject = document.getElementById('draftSubject');
    var body = document.getElementById('draftBody');
    var preview = document.getElementById('draftPreview');
    if (!subject || !body || !preview) { return; }
    var subjectOut = qs('[data-preview-subject]', preview);
    var bodyOut = qs('[data-preview-body]', preview);
    var state = document.getElementById('previewState');
    var startSubject = subject.value;
    var startBody = body.value;
    var approveBtn = document.getElementById('approveBtn');
    var reason = document.getElementById('approveReason');
    var reasonText = reason ? reason.textContent : '';

    function sync() {
      if (subjectOut) { subjectOut.textContent = subject.value || 'Untitled draft'; }
      if (bodyOut) {
        while (bodyOut.firstChild) { bodyOut.removeChild(bodyOut.firstChild); }
        paragraphs(body.value).forEach(function (text) {
          var p = document.createElement('p');
          p.textContent = text;
          bodyOut.appendChild(p);
        });
      }
      var dirty = subject.value !== startSubject || body.value !== startBody;
      if (state) { state.textContent = dirty ? 'Unsaved changes' : 'Updates as you type'; }
      if (approveBtn) {
        approveBtn.disabled = dirty;
        if (dirty) { approveBtn.setAttribute('aria-disabled', 'true'); } else { approveBtn.removeAttribute('aria-disabled'); }
        if (reason) { reason.textContent = dirty ? 'Save your edits to approve' : reasonText; }
      }
    }
    subject.addEventListener('input', sync);
    body.addEventListener('input', sync);
  }

  function initTimes() {
    if (typeof window.pkRelativeTimes === 'function') { window.pkRelativeTimes(document); }
  }

  function init() {
    initSteps();
    initRows();
    initOverwriteConfirm();
    initEditor();
    initTimes();
  }

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
