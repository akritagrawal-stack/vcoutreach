/* Campaign and draft editor behavior. Loaded only by crm_drafts.html and crm_draft.html.
   No network calls and nothing here sends mail: it runs the step-by-step stepper (Back, Edit, history), makes table rows clickable,
   confirms a rewrite of existing drafts, and keeps the editor preview in step with the fields. */
(function () {
  'use strict';

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  /* Stepper. One step body is open at a time; every step keeps its header (title, summary, Edit/Open button).
     openStep is the single entry point: Back, Next, Edit, the progress squares, Alt+ArrowLeft, popstate and
     hash links all go through it. Nothing here sends mail or talks to the network. */
  var STORE_KEY = 'pk.campaign.step';
  var HASH_ALIAS = { drafts: 'draftsTable' };

  function stepEls() { return qsa('.cmp-stepper .cmp-step'); }

  function stepEl(n) { return qs('.cmp-stepper .cmp-step[data-step="' + n + '"]'); }

  function stepNumber(el) {
    var step = el && el.closest ? el.closest('.cmp-step') : null;
    return step ? parseInt(step.getAttribute('data-step'), 10) : 0;
  }

  function readStored() {
    try {
      var raw = window.sessionStorage.getItem(STORE_KEY);
      var n = parseInt(raw, 10);
      return n >= 1 && n <= 5 ? n : 0;
    } catch (e) { return 0; }
  }

  function writeStored(n) {
    try { window.sessionStorage.setItem(STORE_KEY, String(n)); } catch (e) { /* storage can be blocked */ }
  }

  /* Which step does a location hash point at? 0 when it points at nothing in the stepper. */
  function stepForHash(hash) {
    var id = String(hash || '').replace(/^#/, '');
    if (!id) { return 0; }
    var m = /^step-([1-5])$/.exec(id);
    if (m) { return parseInt(m[1], 10); }
    if (id === 'campaignReadiness') { return 4; }
    id = HASH_ALIAS[id] || id;
    var el = null;
    try { el = document.getElementById(decodeURIComponent(id)); } catch (e) { el = document.getElementById(id); }
    return stepNumber(el);
  }

  function openStep(n, opts) {
    opts = opts || {};
    var target = stepEl(n);
    if (!target) { return; }
    stepEls().forEach(function (step) {
      var isTarget = step === target;
      var done = step.getAttribute('data-done') === '1';
      step.classList.toggle('cmp-step--open', isTarget);
      step.classList.toggle('cmp-step--collapsed', !isTarget);
      step.classList.toggle('pk-step--current', isTarget);
      step.classList.toggle('pk-step--done', done && !isTarget);
      step.classList.toggle('pk-step--todo', !done && !isTarget);
      if (isTarget) { step.setAttribute('aria-current', 'step'); } else { step.removeAttribute('aria-current'); }
      var edit = qs('[data-step-edit]', step);
      if (edit) {
        edit.hidden = isTarget;
        edit.setAttribute('aria-expanded', isTarget ? 'true' : 'false');
      }
    });
    qsa('.cmp-progress__item').forEach(function (item) {
      var link = qs('[data-step-jump]', item);
      var here = link && parseInt(link.getAttribute('data-step-jump'), 10) === n;
      item.classList.toggle('is-current', !!here);
      if (link) { if (here) { link.setAttribute('aria-current', 'step'); } else { link.removeAttribute('aria-current'); } }
    });
    writeStored(n);
    if (opts.push) {
      var hash = '#step-' + n;
      if (window.location.hash !== hash) {
        try { window.history.pushState({ step: n }, '', hash); } catch (e) { /* file or sandboxed contexts */ }
      }
    }
    if (opts.focus !== false) {
      var heading = qs('.pk-step__title', target);
      if (heading) {
        try { heading.focus({ preventScroll: true }); } catch (e) { heading.focus(); }
      }
      if (target.scrollIntoView) { target.scrollIntoView({ block: 'nearest' }); }
    }
  }

  /* "#drafts" (the nav link) is the same place as "#draftsTable". */
  function revealAlias() {
    var id = (window.location.hash || '').replace(/^#/, '');
    var real = HASH_ALIAS[id];
    var el = real ? document.getElementById(real) : null;
    if (el && el.scrollIntoView) { el.scrollIntoView({ block: 'start' }); }
  }

  function followLocation() {
    var n = stepForHash(window.location.hash);
    if (n) { openStep(n, { push: false, focus: false }); }
    revealAlias();
  }

  function initSteps() {
    var stepper = qs('.cmp-stepper');
    if (!stepper) { return; }
    var firstTodo = parseInt(stepper.getAttribute('data-first-todo'), 10) || 1;
    var fromHash = stepForHash(window.location.hash);
    var start = fromHash;
    if (!start) {
      var stored = readStored();
      /* A remembered step is used only when it is not past the first unfinished step. */
      start = stored && stored <= firstTodo ? stored : firstTodo;
    }
    openStep(start, { push: false, focus: false });
    try { window.history.replaceState({ step: start }, '', window.location.href); } catch (e) { /* ignore */ }
    revealAlias();

    document.addEventListener('click', function (event) {
      var t = event.target && event.target.closest ? event.target : null;
      if (!t) { return; }
      var blocked = t.closest('a[aria-disabled="true"], .cmp-disabled');
      if (blocked) { event.preventDefault(); return; }
      var back = t.closest('[data-step-back]');
      if (back) { event.preventDefault(); openStep(stepNumber(back) - 1, { push: true }); return; }
      var next = t.closest('[data-step-next]');
      if (next) { event.preventDefault(); openStep(stepNumber(next) + 1, { push: true }); return; }
      var edit = t.closest('[data-step-edit]');
      if (edit) { event.preventDefault(); openStep(parseInt(edit.getAttribute('data-step-edit'), 10), { push: true }); return; }
      var jump = t.closest('[data-step-jump]');
      if (jump) { event.preventDefault(); openStep(parseInt(jump.getAttribute('data-step-jump'), 10), { push: true }); }
    });

    stepper.addEventListener('keydown', function (event) {
      if (event.altKey && event.key === 'ArrowLeft' && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
        var n = stepNumber(event.target);
        if (n > 1) { event.preventDefault(); openStep(n - 1, { push: true }); }
      }
    });

    window.addEventListener('popstate', function (event) {
      var n = event.state && event.state.step ? event.state.step : stepForHash(window.location.hash);
      openStep(n || start, { push: false, focus: false });
      revealAlias();
    });
    window.addEventListener('hashchange', followLocation);
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
