/* Settings readiness board: keeps the three required rows in step with the Gmail and test recipient cards. */
(function () {
  'use strict';
  var board = document.querySelector('[data-readiness]');
  if (!board) { return; }
  var rows = Array.prototype.slice.call(board.querySelectorAll('[data-ready-row]'));
  var count = board.querySelector('[data-ready-count]');
  var segs = board.querySelectorAll('.ready-seg');

  function cardOf(row) { return document.getElementById('svc-' + row.getAttribute('data-ready-row')); }

  function isReady(row, card) {
    var state = card.getAttribute('data-state');
    return row.getAttribute('data-ready-row') === 'gmail' ? (state === 'ok' || state === 'saved') : state === 'ok';
  }

  function wordFor(row, card, ready) {
    if (row.getAttribute('data-ready-row') === 'test-recipient') {
      var input = document.getElementById('testRecipient');
      return ready ? (input && input.value) || 'Set' : 'Not set';
    }
    var label = card.querySelector('[data-svc-label]');
    return label ? label.textContent : '';
  }

  function paintRow(row) {
    var card = cardOf(row);
    if (!card) { return; }
    var ready = isReady(row, card);
    var act = row.querySelector('[data-ready-act]');
    row.setAttribute('data-state', ready ? 'ok' : 'unset');
    row.querySelector('[data-ready-word]').textContent = wordFor(row, card, ready);
    if (ready) {
      act.textContent = '';
    } else if (!act.firstChild) {
      var fix = document.createElement('a');
      fix.className = 'pk-btn pk-btn--secondary';
      fix.href = '#' + row.getAttribute('data-ready-target');
      fix.innerHTML = '<span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></span>';
      fix.appendChild(document.createTextNode('Fix'));
      act.appendChild(fix);
    }
  }

  function paint() {
    rows.forEach(paintRow);
    var n = rows.filter(function (r) { return r.getAttribute('data-state') === 'ok'; }).length;
    if (count) { count.textContent = n + ' of ' + rows.length + ' required set up'; }
    Array.prototype.forEach.call(segs, function (seg, i) {
      if (i < n) { seg.setAttribute('data-on', ''); } else { seg.removeAttribute('data-on'); }
    });
  }

  /* app.js writes the Gmail status copy; keep it free of position words like "below" (see needs_from_other_lanes). */
  var note = document.querySelector('[data-gmail-message]');
  var BELOW = / below\./;
  function trimNote() {
    if (note && BELOW.test(note.textContent)) { note.textContent = note.textContent.replace(BELOW, '.'); }
  }
  trimNote();

  if (typeof MutationObserver === 'function') {
    if (note) { new MutationObserver(trimNote).observe(note, { childList: true, characterData: true, subtree: true }); }
    var watcher = new MutationObserver(paint);
    rows.forEach(function (row) {
      var card = cardOf(row);
      if (card) { watcher.observe(card, { attributes: true, attributeFilter: ['data-state'] }); }
    });
  }
})();
