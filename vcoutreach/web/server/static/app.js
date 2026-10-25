/* Publikreach interactions. Page data stays in escaped HTML attributes. */
(function () {
  'use strict';
  // Early theme (this script runs in <head> before first paint): a saved Toggle theme choice wins, else follow the OS setting.
  var saved = null;
  try { saved = localStorage.getItem('publik-signal-theme'); } catch (error) { saved = null; }
  var osDark = null;
  try { osDark = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null; } catch (error) { osDark = null; }
  document.documentElement.dataset.theme = saved === 'dark' || saved === 'light' ? saved : (osDark && osDark.matches ? 'dark' : 'light');
  if (!(saved === 'dark' || saved === 'light') && osDark && osDark.addEventListener) {
    osDark.addEventListener('change', function (event) {
      var chosen = null;
      try { chosen = localStorage.getItem('publik-signal-theme'); } catch (error) { chosen = null; }
      if (chosen === 'dark' || chosen === 'light') return;
      document.documentElement.dataset.theme = event.matches ? 'dark' : 'light';
      var button = document.getElementById('themeToggle');
      if (button) button.setAttribute('aria-pressed', String(event.matches));
    });
  }
  document.addEventListener('DOMContentLoaded', function () {
    var page = document.body.dataset.page;
    var csrf = document.querySelector('meta[name="csrf-token"]').content;
    function openHashDetails() {
      var el = location.hash.length > 1 ? document.getElementById(decodeURIComponent(location.hash.slice(1))) : null;
      if (el && el.tagName === 'DETAILS') el.open = true;
    }
    window.addEventListener('hashchange', openHashDetails);
    openHashDetails();

    function fmtDur(secs) {
      if (secs < 60) return secs + ' sec';
      if (secs < 3600) {
        var m = Math.floor(secs / 60), s = secs % 60;
        return s ? m + ' min ' + s + ' sec' : m + ' min';
      }
      var h = Math.floor(secs / 3600), m2 = Math.floor((secs % 3600) / 60);
      return m2 ? h + ' hr ' + m2 + ' min' : h + ' hr';
    }
    function setTomorrowDefault(dateEl, timeEl) {
      var t = new Date();
      t.setDate(t.getDate() + 1);
      dateEl.value = t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' + String(t.getDate()).padStart(2, '0');
      timeEl.value = '09:00';
    }
    var themeButton = document.getElementById('themeToggle');
    themeButton.addEventListener('click', function () {
      var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      themeButton.setAttribute('aria-pressed', String(next === 'dark'));
      try { localStorage.setItem('publik-signal-theme', next); } catch (error) {}
    });
    themeButton.setAttribute('aria-pressed', String(document.documentElement.dataset.theme === 'dark'));
    var pitchbookPoll = null;
    function stopPitchbookPoll() { if (pitchbookPoll) { window.clearTimeout(pitchbookPoll); pitchbookPoll = null; } }
    // After "Open sign-in", check lightly (every 5 s, at most 36 times) until the session is saved.
    function startPitchbookPoll(message) {
      stopPitchbookPoll();
      var tries = 0;
      (function tick() {
        pitchbookPoll = window.setTimeout(function () {
          tries += 1;
          fetch('/demo/pitchbook-status', {credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
            .then(function (response) { return response.json(); })
            .then(function (payload) {
              document.dispatchEvent(new CustomEvent('pitchbook-status', {detail: {status: payload.status, ok: payload.ok !== false}}));
              if (payload.message) message.textContent = payload.message;
              if (payload.status === 'session_ready' || payload.status === 'attention_required') { stopPitchbookPoll(); return; }
              if (tries < 36) tick(); else { pitchbookPoll = null; message.textContent = 'Still waiting for sign in. Use Check session after you finish signing in.'; }
            })
            .catch(function () { pitchbookPoll = null; });
        }, 5000);
      })();
    }
    document.querySelectorAll('[data-pitchbook-launch], [data-pitchbook-check]').forEach(function (button) {
      button.addEventListener('click', function () {
        var message = document.querySelector('[data-pitchbook-message]') || document.createElement('span');
        var launching = button.hasAttribute('data-pitchbook-launch');
        var data = new FormData();
        data.append('csrf_token', csrf);
        button.disabled = true;
        stopPitchbookPoll();
        message.textContent = launching ? 'Opening the dedicated PitchBook browser...' : 'Checking your browser session...';
        fetch(launching ? '/demo/pitchbook-visible' : '/demo/pitchbook-status', {
          method: launching ? 'POST' : 'GET',
          body: launching ? data : undefined,
          credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}
        }).then(function (response) {
          return response.json().then(function (payload) {
            document.dispatchEvent(new CustomEvent('pitchbook-status', {detail: {status: payload.status, ok: response.ok}}));
            message.textContent = payload.message || (response.ok ? String(payload.status || 'Session checked').replaceAll('_', ' ') : 'PitchBook connection unavailable.');
            if (payload.status !== 'session_ready' && payload.status !== 'attention_required' && (launching || payload.status === 'waiting_for_sign_in' || payload.status === 'browser_open')) startPitchbookPoll(message);
          });
        }).catch(function () { message.textContent = 'PitchBook connection unavailable. Check the local setup instructions.'; })
          .finally(function () { button.disabled = false; });
      });
    });
    document.querySelectorAll('[data-pitchbook-close]').forEach(function (button) {
      button.addEventListener('click', function () {
        var message = document.querySelector('[data-pitchbook-message]') || document.createElement('span');
        var data = new FormData();
        data.append('csrf_token', csrf);
        button.disabled = true;
        stopPitchbookPoll();
        message.textContent = 'Closing the dedicated PitchBook browser...';
        fetch('/demo/pitchbook-close', {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
          .then(function (response) {
            return response.json().then(function (payload) {
              document.dispatchEvent(new CustomEvent('pitchbook-status', {detail: {status: 'idle', ok: response.ok}}));
              message.textContent = payload.message || 'The PitchBook browser was closed.';
            });
          }).catch(function () { message.textContent = 'Could not close the PitchBook browser. Quit it from the Dock.'; })
          .finally(function () { button.disabled = false; });
      });
    });
    document.querySelectorAll('[data-copy-text]').forEach(function (button) {
      button.addEventListener('click', function () {
        navigator.clipboard.writeText(button.dataset.copyText).then(function () {
          button.textContent = 'Copied';
          window.setTimeout(function () { button.textContent = 'Copy URL'; }, 1400);
        }).catch(function () { button.textContent = 'Select the URL to copy'; });
      });
    });
    var goLiveForm = document.getElementById('goLiveForm');
    var goLiveToggle = document.getElementById('bannerGoLiveToggle');
    function cancelGoLive() {
      goLiveForm.hidden = true;
      goLiveForm.reset();
      goLiveToggle.setAttribute('aria-expanded', 'false');
      goLiveToggle.focus();
    }
    var goLiveCancel = document.querySelector('[data-cancel-go-live]');
    if (goLiveCancel) goLiveCancel.addEventListener('click', cancelGoLive);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && goLiveForm && !goLiveForm.hidden) {
        event.preventDefault();
        cancelGoLive();
      }
      var dialog = document.getElementById('bulkScheduleOverlay');
      if (event.key === 'Escape' && dialog && dialog.style.display !== 'none') {
        dialog.style.display = 'none';
        var opener = document.querySelector('[data-action="partners_list-8"]');
        if (opener) opener.focus();
      }
    });
    document.querySelectorAll('form[data-busy]').forEach(function (form) {
      form.addEventListener('submit', function () {
        var button = form.querySelector('button[type="submit"]');
        if (!button) return;
        window.setTimeout(function () {
          button.disabled = true;
          button.textContent = form.dataset.busy;
        }, 0);
      });
    });
    document.querySelectorAll('[data-confirm]').forEach(function (element) {
      element.addEventListener(element.tagName === 'FORM' ? 'submit' : 'click', function (event) {
        if (!window.confirm(element.dataset.confirm)) event.preventDefault();
      });
    });
    document.querySelectorAll('[data-toggle]').forEach(function (element) {
      element.addEventListener('click', function () {
        var target = document.getElementById(element.dataset.toggle);
        var open = target.hidden;
        if (!open && target === goLiveForm) { cancelGoLive(); return; }
        target.hidden = !open;
        element.setAttribute('aria-expanded', String(open));
        if (open) { var field = target.querySelector('input:not([type="hidden"])'); if (field) field.focus(); }
      });
    });
    document.querySelectorAll('[data-dismiss-flash]').forEach(function (button) {
      button.addEventListener('click', function () { button.closest('li').remove(); });
    });

if (page === 'enrichment_status') {

// ── Client-side column sort ─────────────────────────────────────────────────

var _sortDir = {};  // data-sort key -> 'asc'|'desc'
// data-sort key -> row data attribute holding the numeric value
var _sortAttr = {tier: 'data-tier', pb_api: 'data-pb-api', pb: 'data-pb', linkedin: 'data-linkedin',
                 web: 'data-web', web2: 'data-web2', fw: 'data-fw', eg: 'data-eg', facts: 'data-total'};
var _hookOrder = {'cite_specific': 0, 'grounded': 0, 'generic': 1, 'ungrounded': 1, 'unknown': 2, 'not_checked': 2, 'no_enrichment': 3, 'no_hook': 4};

function sortTable(key, th) {
  var table = document.getElementById('enrichTable');
  var tbody = table.querySelector('tbody');
  var rows = Array.from(tbody.querySelectorAll('tr[data-total]'));
  var dir = _sortDir[key] === 'asc' ? 'desc' : 'asc';
  _sortDir[key] = dir;
  table.querySelectorAll('thead th').forEach(function (cell) {
    cell.setAttribute('aria-sort', cell === th ? (dir === 'asc' ? 'ascending' : 'descending') : 'none');
  });
  var colIdx = th.cellIndex;
  function value(row) {
    if (_sortAttr[key]) return parseInt(row.getAttribute(_sortAttr[key]) || '0', 10);
    if (key === 'hook') {
      var o = _hookOrder[row.getAttribute('data-hook-quality')];
      return o !== undefined ? o : 5;
    }
    return row.cells[colIdx] ? row.cells[colIdx].textContent.trim().toLowerCase() : '';
  }
  rows.sort(function (a, b) {
    var av = value(a), bv = value(b);
    if (av < bv) return dir === 'asc' ? -1 : 1;
    if (av > bv) return dir === 'asc' ? 1 : -1;
    return 0;
  });
  rows.forEach(function (r) { tbody.appendChild(r); });
}

var enrichTableEl = document.getElementById('enrichTable');
if (enrichTableEl) {
  enrichTableEl.querySelector('thead').addEventListener('click', function (event) {
    var th = event.target.closest('th[data-sort]');
    if (th) sortTable(th.getAttribute('data-sort'), th);
  });
}

// ── Filter chips ────────────────────────────────────────────────────────────

var activeFilters = new Set();

function toggleChip(btn, key) {
  if (activeFilters.has(key)) {
    activeFilters.delete(key);
    btn.classList.remove('active');
  } else {
    activeFilters.add(key);
    btn.classList.add('active');
  }
  applyFilters();
  document.getElementById('chip-clear').style.display = activeFilters.size ? 'inline-flex' : 'none';
}

function clearAllChips() {
  activeFilters.clear();
  document.querySelectorAll('#filterBar .chip').forEach(function(c) {
    c.classList.remove('active');
  });
  document.getElementById('chip-clear').style.display = 'none';
  applyFilters();
}

function rowMatchesFilters(row) {
  if (activeFilters.size === 0) return true;

  // Source filters
  if (activeFilters.has('has_pb_api') && row.getAttribute('data-pb-api') !== '1') return false;
  if (activeFilters.has('miss_pb_api') && row.getAttribute('data-pb-api') === '1') return false;
  if (activeFilters.has('miss_linkedin') && row.getAttribute('data-linkedin') === '1') return false;
  if (activeFilters.has('miss_web') && row.getAttribute('data-web') === '1') return false;

  // Hook quality
  var hq = row.getAttribute('data-hook-quality');
  if (activeFilters.has('hq_cite') && hq !== 'cite_specific') return false;
  if (activeFilters.has('hq_generic') && hq !== 'generic') return false;
  if (activeFilters.has('hq_none') && hq !== 'no_hook') return false;

  // Tier
  var tier = row.getAttribute('data-tier');
  var tierFilters = ['tier_1','tier_2','tier_3','tier_4'].filter(function(t) { return activeFilters.has(t); });
  if (tierFilters.length > 0) {
    var matchTier = tierFilters.some(function(t) { return 'tier_' + tier === t; });
    if (!matchTier) return false;
  }

  return true;
}

function applyFilters() {
  var rows = document.querySelectorAll('#enrichTable tbody tr[data-total]');
  rows.forEach(function(row) {
    row.style.display = rowMatchesFilters(row) ? '' : 'none';
  });
}

document.querySelectorAll('[data-action="enrichment_status-0"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'has_pb_api'); }); });
document.querySelectorAll('[data-action="enrichment_status-1"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'miss_pb_api'); }); });
document.querySelectorAll('[data-action="enrichment_status-2"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'miss_linkedin'); }); });
document.querySelectorAll('[data-action="enrichment_status-3"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'miss_web'); }); });
document.querySelectorAll('[data-action="enrichment_status-4"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'hq_cite'); }); });
document.querySelectorAll('[data-action="enrichment_status-5"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'hq_generic'); }); });
document.querySelectorAll('[data-action="enrichment_status-6"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'hq_none'); }); });
document.querySelectorAll('[data-action="enrichment_status-7"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleChip(this,'tier_' + this.dataset.arg0); }); });
document.querySelectorAll('[data-action="enrichment_status-8"]').forEach(function (element) { element.addEventListener('click', function (event) { clearAllChips(); }); });
}

if (page === 'partner_detail') {

function showText() {
  document.getElementById('preview-text').hidden = false;
  document.getElementById('preview-html').hidden = true;
  document.getElementById('tab-text').classList.add('active');
  document.getElementById('tab-html').classList.remove('active');
}
function showHtml() {
  document.getElementById('preview-text').hidden = true;
  document.getElementById('preview-html').hidden = false;
  document.getElementById('tab-text').classList.remove('active');
  document.getElementById('tab-html').classList.add('active');
}

function toggleReject() {
  var panel = document.getElementById('rejectPanel');
  panel.classList.toggle('open');
}

// Schedule form ETA (single-partner detail page)
(function() {
  var dateEl = document.getElementById('sd1');
  var timeEl = document.getElementById('st1');
  var modeEl = document.getElementById('sm1');
  var etaEl  = document.getElementById('schedEta1');
  var liveEl = document.getElementById('liveSchedConfirm1');

  if (!dateEl) return;

  setTomorrowDefault(dateEl, timeEl);

  function updateEta() {
    var n = 1; // single partner
    var minS = n * 2, maxS = n * 32, avgS = n * 17;
    var d = dateEl.value, t = timeEl.value;
    if (liveEl) {
      var isLive = modeEl && modeEl.value === 'live';
      liveEl.hidden = !isLive;
      liveEl.style.display = isLive ? 'block' : 'none';
      var confirmation = liveEl.querySelector('input');
      if (confirmation) { confirmation.required = !!isLive; confirmation.disabled = !isLive; }
    }
    if (!d || !t) { etaEl.style.display = 'none'; return; }
    var start = new Date(d + 'T' + t);
    var compMax = new Date(start.getTime() + maxS * 1000);
    var h = compMax.getHours(), mi = compMax.getMinutes();
    var ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    var compStr = h + ':' + String(mi).padStart(2,'0') + ' ' + ap;
    etaEl.innerHTML = '<strong>Estimated:</strong> ' + fmtDur(minS) + ' - ' + fmtDur(maxS) + ' (avg ' + fmtDur(avgS) + '). <strong>Completes by ~' + compStr + '</strong>';
    etaEl.style.display = '';
  }

  dateEl.addEventListener('change', updateEta);
  timeEl.addEventListener('change', updateEta);
  if (modeEl) modeEl.addEventListener('change', updateEta);
  updateEta();
})();

document.querySelectorAll('[data-action="partner_detail-0"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleReject(); }); });
document.querySelectorAll('[data-action="partner_detail-1"]').forEach(function (element) { element.addEventListener('click', function (event) { var panel = document.getElementById('liveSendPanel'); panel.hidden = !panel.hidden; }); });
document.querySelectorAll('[data-action="partner_detail-2"]').forEach(function (element) { element.addEventListener('click', function (event) { var panel = document.getElementById('schedulePanel1'); panel.hidden = !panel.hidden; }); });
document.querySelectorAll('[data-action="partner_detail-3"]').forEach(function (element) { element.addEventListener('click', function (event) { showText(); }); });
document.querySelectorAll('[data-action="partner_detail-4"]').forEach(function (element) { element.addEventListener('click', function (event) { showHtml(); }); });
}

if (page === 'candidates_list') {

var bulkBar = document.getElementById('bulkBar');
var bulkCount = document.getElementById('bulkCount');
var selectAll = document.getElementById('selectAll');

function getChecked() {
  return Array.from(document.querySelectorAll('.row-check:not([disabled]):checked'));
}

function getEnabledChecks() {
  return Array.from(document.querySelectorAll('.row-check:not([disabled])'));
}

function updateBulkBar() {
  var checked = getChecked();
  var n = checked.length;
  if (n > 0) {
    bulkBar.style.display = 'flex';
    bulkCount.textContent = n + ' selected';
  } else {
    bulkBar.style.display = 'none';
  }
  var enabled = getEnabledChecks();
  if (selectAll) {
    selectAll.indeterminate = (checked.length > 0 && checked.length < enabled.length);
    selectAll.checked = enabled.length > 0 && checked.length === enabled.length;
  }
}

function toggleAll(masterCb) {
  getEnabledChecks().forEach(function(cb) { cb.checked = masterCb.checked; });
  updateBulkBar();
}

function clearChecks() {
  document.querySelectorAll('.row-check:not([disabled])').forEach(function(cb) { cb.checked = false; });
  if (selectAll) { selectAll.checked = false; selectAll.indeterminate = false; }
  bulkBar.style.display = 'none';
}

// Initialise bar based on pre-checked boxes (defaulted by server)
updateBulkBar();

document.querySelectorAll('[data-action="candidates_list-0"]').forEach(function (element) { element.addEventListener('click', function (event) { clearChecks(); }); });
document.querySelectorAll('[data-action="candidates_list-1"]').forEach(function (element) { element.addEventListener('change', function (event) { toggleAll(this); }); });
document.querySelectorAll('[data-action="candidates_list-2"]').forEach(function (element) { element.addEventListener('change', function (event) { updateBulkBar(); }); });
}

if (page === 'settings') {
  var WORDS = {unset: 'Not set', saved: 'Key saved', ok: 'Connected', fail: 'Failed'};
  var SAVED_WORD = {searxng: 'URL saved'};
  var cards = {};
  document.querySelectorAll('.svc-card').forEach(function (card) { cards[card.dataset.service] = card; });
  function noteOf(card) { return card.querySelector('[data-svc-note]'); }
  function say(card, text) { var note = card && noteOf(card); if (note) note.textContent = text; }
  function updateSummary() {
    // Counts exactly the header tiles (the same list and states as the chips), so the number matches what is on screen.
    var tiles = document.querySelectorAll('.svc-tile');
    var ready = Array.prototype.filter.call(tiles, function (tile) { return tile.dataset.state === 'ok' || tile.dataset.state === 'saved'; }).length;
    document.querySelectorAll('[data-svc-summary]').forEach(function (node) { node.textContent = ready + ' of ' + tiles.length + ' services connected'; });
  }
  // The one place a service status changes: card chip, header tile and summary stay in sync, no reload.
  function setServiceStatus(id, state, text) {
    var card = cards[id];
    if (!card || !WORDS[state]) return;
    card.dataset.state = state;
    var label = card.querySelector('.svc-status [data-svc-label]');
    var shown = (state === 'saved' && SAVED_WORD[id]) || WORDS[state];
    if (label) label.textContent = shown;
    document.querySelectorAll('[data-tile-for="' + id + '"]').forEach(function (tile) {
      tile.dataset.state = state;
      var word = tile.querySelector('[data-svc-label]');
      if (word) word.textContent = shown;
    });
    var removeKey = card.querySelector('[data-svc-remove]');
    if (removeKey) removeKey.hidden = state === 'unset';
    if (text) say(card, text);
    updateSummary();
  }
  window.setServiceStatus = setServiceStatus;
  function postJson(form) {
    return fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin', headers: {'X-CSRF-Token': csrf, 'Accept': 'application/json'}})
      .then(function (response) {
        return response.json().then(function (payload) { return {ok: response.ok && payload.ok !== false, payload: payload}; });
      });
  }
  function problem(payload, fallback) {
    return String(payload.error || payload.error_code || payload.status || fallback).replaceAll('_', ' ');
  }

  // Secret fields: every password input gets a Show/Hide button. Values are never logged or stored by this code.
  var secretShell = document.querySelector('.settings-shell');
  var secretSeq = 0;
  function secretInputsIn(root) {
    var found = root.matches && root.matches('input[type="password"]') ? [root] : [];
    return found.concat(Array.prototype.slice.call(root.querySelectorAll ? root.querySelectorAll('input[type="password"]') : []));
  }
  function setSecretShown(input, button, shown) {
    input.type = shown ? 'text' : 'password';
    button.textContent = shown ? 'Hide' : 'Show';
    button.setAttribute('aria-pressed', String(shown));
    button.setAttribute('aria-label', (shown ? 'Hide ' : 'Show ') + button.dataset.secretName);
  }
  function fieldNameOf(label) {
    var span = label && label.querySelector(':scope > span');
    if (!span) return '';
    return Array.prototype.filter.call(span.childNodes, function (node) { return node.nodeType === 3; })
      .map(function (node) { return node.textContent; }).join('').trim();
  }
  function augmentSecretInputs(root) {
    secretInputsIn(root).forEach(function (input) {
      if (input.dataset.secretReady) return;
      if (input.hasAttribute('data-pk-key')) return; // the Publik API card (setup_ai.js) owns its own Show button
      input.dataset.secretReady = '1';
      var label = input.closest('label');
      var span = label && label.querySelector(':scope > span');
      var name = fieldNameOf(label) || input.name || 'secret';
      if (!input.id) input.id = 'secret-input-' + (++secretSeq);
      if (span) {
        if (!span.id) span.id = 'secret-label-' + (++secretSeq);
        input.setAttribute('aria-labelledby', span.id);
      } else {
        input.setAttribute('aria-label', name);
      }
      var wrap = document.createElement('span');
      wrap.className = 'secret-wrap';
      input.parentNode.insertBefore(wrap, input);
      wrap.appendChild(input);
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'svc-btn secret-toggle';
      button.dataset.secretName = name;
      button.setAttribute('aria-controls', input.id);
      button.addEventListener('click', function () {
        setSecretShown(input, button, input.type === 'password');
      });
      wrap.appendChild(button);
      setSecretShown(input, button, false);
    });
  }
  // Any submit inside Settings returns its secret fields to hidden; the existing handlers clear the values.
  function hideSecrets(form) {
    form.querySelectorAll('.secret-wrap').forEach(function (wrap) {
      var input = wrap.querySelector('input');
      var button = wrap.querySelector('.secret-toggle');
      if (input && button && input.type !== 'password') setSecretShown(input, button, false);
    });
  }
  augmentSecretInputs(secretShell || document.body);
  if (secretShell) {
    secretShell.addEventListener('submit', function (event) {
      if (event.target && event.target.tagName === 'FORM') hideSecrets(event.target);
    }, true);
  }
  if (secretShell && window.MutationObserver) {
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        Array.prototype.forEach.call(record.addedNodes, function (node) {
          if (node.nodeType === 1) augmentSecretInputs(node);
        });
      });
    }).observe(secretShell, {childList: true, subtree: true});
  }

  // Key cards: Store and Test connection.
  document.querySelectorAll('.svc-card [data-svc-form]').forEach(function (form) {
    var card = form.closest('.svc-card');
    var id = card.dataset.service;
    var inputs = Array.prototype.slice.call(form.querySelectorAll('input:not([type="hidden"])'));
    var testButton = form.querySelector('[data-svc-test]');
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!inputs.length) return;
      var removing = event.submitter && event.submitter.value === 'remove';
      if (!removing && !inputs.some(function (input) { return input.value.trim(); })) { say(card, 'No key entered; saved key unchanged'); return; }
      var action = form.querySelector('input[data-key-action]');
      if (!action) { action = document.createElement('input'); action.type = 'hidden'; action.name = 'action'; action.dataset.keyAction = ''; form.appendChild(action); }
      action.value = removing ? 'remove' : 'save';
      var button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      postJson(form).then(function (result) {
        if (!result.ok) { say(card, 'Could not store: ' + problem(result.payload, 'settings unavailable') + '.'); return; }
        if (result.payload.status === 'unchanged') { say(card, result.payload.message); return; }
        if (removing) { setServiceStatus(id, 'unset', 'Key removed.'); return; }
        inputs.forEach(function (input) { input.value = ''; });
        var status = result.payload.key_status;
        var complete = !status || inputs.every(function (input) { return status[input.name]; });
        setServiceStatus(id, complete ? 'saved' : 'unset', complete ? 'Stored in the workspace. The value is never shown again.' + (testButton ? ' Run Test connection.' : '') : 'Stored. The other field is still needed.');
      }).catch(function () { say(card, 'Could not reach the local settings service.'); })
        .finally(function () { button.disabled = false; });
    });
    if (testButton) {
      testButton.addEventListener('click', function () {
        var kind = card.dataset.test;
        var data = new FormData();
        data.append('csrf_token', csrf);
        if (kind === 'data') data.append('provider', id);
        testButton.disabled = true;
        say(card, kind === 'gemini' ? 'Running 3 anonymized emails...' : 'Checking the connection...');
        fetch(kind === 'gemini' ? '/api/gemini/test' : kind === 'tracking' ? '/api/tracking/test' : '/api/data-provider/test', {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
          .then(function (response) { return response.json().then(function (payload) { return {ok: response.ok && payload.ok !== false, payload: payload}; }); })
          .then(function (result) {
            var code = result.payload.error_code || '';
            if (result.ok) {
              var credits = result.payload.credits_remaining;
              setServiceStatus(id, 'ok', kind === 'tracking' ? 'Worker reachable and token accepted.' : kind === 'gemini' ? 'Probe passed on 3 anonymized emails. Nothing stored.' : 'Verified without using credits' + (credits != null ? '. Credits: ' + credits + '.' : '.'));
            } else if (code === 'provider_key_not_configured' || result.payload.unset) {
              setServiceStatus(id, 'unset', 'Not set up yet. Optional.');
            } else {
              setServiceStatus(id, 'fail', kind === 'tracking' ? (result.payload.message || 'Could not verify the tracking Worker.') : 'Test stopped: ' + problem(result.payload, 'provider unavailable') + '.');
            }
          })
          .catch(function () { setServiceStatus(id, 'fail', 'Could not reach the local test service.'); })
          .finally(function () { testButton.disabled = false; });
      });
    }
  });

  // PitchBook has no key; the global session check reports through this event.
  document.addEventListener('pitchbook-status', function (event) {
    var status = event.detail && event.detail.status;
    if (status === 'session_ready') setServiceStatus('pitchbook', 'ok');
    else if (status === 'attention_required') setServiceStatus('pitchbook', 'fail');
    else if (status === 'idle' || status === 'browser_open' || status === 'waiting_for_sign_in') setServiceStatus('pitchbook', 'unset');
  });

  // More settings: per-key Set / Not set chips update in place.
  document.querySelectorAll('[data-more]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var note = form.querySelector('[data-svc-note]');
      var fields = Array.prototype.slice.call(form.querySelectorAll('input:not([type="hidden"])'));
      if (!fields.length) return;
      postJson(form).then(function (result) {
        if (!result.ok) { note.textContent = 'Could not save: ' + problem(result.payload, 'settings unavailable') + '.'; return; }
        fields.forEach(function (input) { if (input.name !== 'TEST_RECIPIENT_EMAIL') input.value = ''; });
        var status = result.payload.key_status || {};
        form.querySelectorAll('[data-key-chip]').forEach(function (chip) { chip.textContent = status[chip.dataset.keyChip] ? 'Set' : 'Not set'; chip.classList.toggle('on', !!status[chip.dataset.keyChip]); });
        note.textContent = result.payload.message || 'Saved. Key values are never shown.';
      }).catch(function () { note.textContent = 'Could not reach the local settings service.'; });
    });
  });

  // Gmail card state machine.
  var gmailCard = cards.gmail;
  var gmailFile = document.querySelector('[data-gmail-file]');
  var gmailPoll;
  var gmailSwitching = false;
  var VIEW_STATE = {'no-client': 'unset', ready: 'saved', running: 'saved', connected: 'ok', failed: 'fail'};
  var VIEW_TEXT = {
    'no-client': 'Gmail is not connected. Choose your Google OAuth client file, then choose Connect Gmail. Then set your test recipient below.',
    ready: 'OAuth client saved privately. Connect Gmail to continue. TEST mode still applies.',
    running: 'Finish consent in your system browser. This expires after three minutes.',
    connected: 'Gmail connected. The connection is saved privately in this workspace. TEST mode still applies.',
    failed: 'Gmail authorization did not finish. Check your Desktop client and try again.'
  };
  function gmailButtons(busy) {
    if (gmailCard) gmailCard.querySelectorAll('button').forEach(function (button) { button.disabled = busy; });
  }
  function showGmail(view, text) {
    if (!gmailCard) return;
    gmailCard.dataset.gmailView = view;
    var passwordForm = gmailCard.querySelector('[data-gmail-app-password]');
    if (passwordForm) passwordForm.hidden = view === 'connected' || view === 'running';
    var alternative = gmailCard.querySelector('[data-gmail-advanced]');
    if (alternative) alternative.hidden = view === 'connected' || view === 'running';
    var steps = gmailCard.querySelector('[data-gmail-steps]');
    if (steps) steps.hidden = view === 'connected' || view === 'running';
    gmailCard.querySelectorAll('[data-gmail-panel]').forEach(function (panel) { panel.hidden = panel.dataset.gmailPanel !== view; });
    gmailButtons(view === 'running');
    setServiceStatus('gmail', VIEW_STATE[view], text || VIEW_TEXT[view]);
  }
  function gmailViewOf(payload) {
    if (payload.running) return 'running';
    if (payload.failed) return 'failed';
    if (payload.state === 'connected') return 'connected';
    return payload.state === 'not connected' || payload.state === 'add OAuth client' ? 'no-client' : 'ready';
  }
  function gmailStatus() {
    clearTimeout(gmailPoll);
    gmailPoll = null;
    return fetch('/api/gmail/status', {credentials: 'same-origin'}).then(function (response) {
      if (!response.ok) throw new Error('status');
      return response.json();
    }).then(function (payload) {
      if (typeof payload.state !== 'string') return;
      gmailCard.querySelector('[data-gmail-address]').textContent = payload.address || '';
      gmailCard.querySelector('[data-gmail-switch]').hidden = payload.mode === 'app_password';
      showGmail(gmailViewOf(payload));
      if (payload.running) gmailPoll = setTimeout(gmailStatus, 1500);
    }).catch(function () {
      say(gmailCard, 'Could not reach the local Gmail setup service. Reload Settings to retry.');
      gmailButtons(false);
    });
  }
  function gmailPost(path, data) {
    gmailButtons(true);
    return fetch(path, {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (!payload.ok) { gmailSwitching = false; gmailButtons(false); say(gmailCard, payload.error || 'Gmail setup is unavailable.'); return false; }
        return gmailStatus().then(function () { return true; });
      }).catch(function () { gmailSwitching = false; gmailButtons(false); say(gmailCard, 'Gmail setup is unavailable. Reload Settings to retry.'); return false; });
  }
  function gmailForm(extra) {
    var data = new FormData();
    data.append('csrf_token', csrf);
    return data;
  }
  function gmailConnect() {
    gmailSwitching = false;
    gmailPost('/api/gmail/connect', gmailForm());
  }
  function gmailChoose(auto) {
    if (!auto) gmailSwitching = false;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_oauth_client) {
      window.pywebview.api.choose_oauth_client(csrf).then(function (payload) {
        if (payload.cancelled) { gmailSwitching = false; return; }
        if (!payload.ok) { gmailSwitching = false; say(gmailCard, payload.error); return; }
        return gmailStatus().then(function () { if (gmailSwitching) gmailConnect(); });
      }).catch(function () { gmailSwitching = false; say(gmailCard, 'Could not choose the OAuth client. Try again.'); });
    } else if (gmailFile) {
      gmailFile.click();
    }
  }
  if (gmailCard && gmailFile) {
    var gmailPasswordForm = gmailCard.querySelector('[data-gmail-app-password]');
    gmailPasswordForm.addEventListener('submit', function (event) {
      event.preventDefault();
      var data = new FormData(gmailPasswordForm);
      gmailPasswordForm.querySelector('[name="password"]').value = '';
      say(gmailCard, 'Checking the secure Gmail connection...');
      gmailPost('/api/gmail/connect', data).then(function () { data.delete('password'); });
    });
    gmailCard.querySelectorAll('[data-gmail-choose]').forEach(function (button) { button.addEventListener('click', function () { gmailChoose(false); }); });
    gmailCard.querySelectorAll('[data-gmail-connect]').forEach(function (button) { button.addEventListener('click', gmailConnect); });
    // First-time Connect Gmail: choose the client file, then connect straight away.
    gmailCard.querySelectorAll('[data-gmail-choose-connect]').forEach(function (button) {
      button.addEventListener('click', function () {
        gmailSwitching = true;
        gmailChoose(true);
      });
    });
    gmailFile.addEventListener('change', function () {
      if (!gmailFile.files.length) return;
      var upload = new FormData(gmailCard.querySelector('[data-gmail-upload]'));
      gmailFile.value = '';
      gmailPost('/api/gmail/client', upload).then(function (ok) { if (ok && gmailSwitching) gmailConnect(); });
    });
    function gmailDisconnect(then) {
      gmailPost('/api/gmail/disconnect', gmailForm()).then(function (ok) { if (ok && then) then(); });
    }
    var removeButton = gmailCard.querySelector('[data-gmail-remove]');
    var switchButton = gmailCard.querySelector('[data-gmail-switch]');
    if (removeButton) removeButton.addEventListener('click', function () {
      gmailSwitching = false;
      gmailDisconnect();
    });
    if (switchButton) switchButton.addEventListener('click', function () {
      if (!window.confirm('Disconnect this Gmail account and connect a different one? The saved OAuth client and token are removed, so you will choose a Desktop client file again.')) return;
      gmailSwitching = true;
      gmailDisconnect(function () { gmailChoose(true); });
    });
    gmailStatus();
  }

  // Jump links: open the target, focus its first field, and flash it.
  function reveal(id) {
    var target = id && document.getElementById(id);
    if (!target) return;
    for (var node = target; node; node = node.parentElement) if (node.tagName === 'DETAILS') node.open = true;
    target.classList.remove('svc-hl');
    void target.offsetWidth;
    target.classList.add('svc-hl');
    window.setTimeout(function () { target.classList.remove('svc-hl'); }, 1800);
    // Focus the first usable input (skipping hidden panels and closed alternatives), else the first button.
    function usable(node) {
      for (var up = node; up && up !== target.parentElement; up = up.parentElement) {
        if (up.hidden || (up.tagName === 'DETAILS' && !up.open)) return false;
      }
      return true;
    }
    function firstUsable(selector) {
      return Array.prototype.slice.call(target.querySelectorAll(selector)).filter(usable)[0];
    }
    var field = firstUsable('input:not([type="hidden"]):not([type="file"]), textarea, select') || firstUsable('button:not([disabled])');
    // No field to land on: focus the card heading (or the target itself) so keyboard and screen reader users move with the jump.
    var heading = target.querySelector('h1, h2, h3, summary');
    var landing = field || heading || target;
    if (!field && landing.tabIndex < 0 && !landing.hasAttribute('tabindex')) landing.setAttribute('tabindex', '-1');
    landing.focus({preventScroll: true});
    target.scrollIntoView({block: 'start', behavior: 'smooth'});
  }
  document.querySelectorAll('.svc-tile').forEach(function (tile) {
    tile.addEventListener('click', function (event) {
      event.preventDefault();
      history.replaceState(null, '', tile.getAttribute('href'));
      reveal(tile.getAttribute('href').slice(1));
    });
  });
  // Same-page #svc-* / #sec-* links (the Next step banner "Go" link) move focus even when the hash does not change.
  document.addEventListener('click', function (event) {
    var link = event.target.closest && event.target.closest('a[href*="#svc-"], a[href*="#sec-"]');
    if (!link || link.classList.contains('svc-tile') || link.origin !== location.origin || link.pathname !== location.pathname) return;
    var id = link.hash.slice(1);
    if (!document.getElementById(id)) return;
    event.preventDefault();
    history.replaceState(null, '', link.hash);
    reveal(id);
  });
  window.addEventListener('hashchange', function () { reveal(location.hash.slice(1)); });
  if (location.hash) reveal(location.hash.slice(1));
  document.addEventListener('invalid', function (event) {
    var holder = event.target.closest && event.target.closest('details');
    if (holder) holder.open = true;
  }, true);

  // Sample email preview follows the workspace form while its section is open.
  var settingsForm = document.getElementById('workspaceSettingsForm');
  var sampleBox = document.getElementById('sec-sample');
  var sampleCopy = document.getElementById('sampleEmailCopy');
  var sampleTimer = null;
  function refreshSample() {
    if (!settingsForm || !sampleBox.open) return;
    fetch('/demo/sample-email', {method: 'POST', body: new FormData(settingsForm), credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function (response) { if (!response.ok) throw new Error('preview'); return response.json(); })
      .then(function (payload) {
        document.getElementById('sampleEmailSubject').textContent = payload.subject;
        document.getElementById('sampleEmailAttachment').textContent = payload.attachment;
        sampleCopy.replaceChildren();
        (payload.paragraphs || []).forEach(function (text) {
          var paragraph = document.createElement('p');
          paragraph.textContent = text;
          sampleCopy.appendChild(paragraph);
        });
      }).catch(function () {});
  }
  if (settingsForm && sampleBox) {
    settingsForm.addEventListener('input', function () { window.clearTimeout(sampleTimer); sampleTimer = window.setTimeout(refreshSample, 180); });
    sampleBox.addEventListener('toggle', refreshSample);
  }
  updateSummary();
}

if (page === 'partners_list') {

// ── Multi-select filter chips (URL toggle) ──────────────────────────────────

function getParams() {
  return new URLSearchParams(window.location.search);
}

function toggleFilter(key, value) {
  var params = getParams();
  var existing = params.getAll(key);
  if (existing.includes(value)) {
    // Remove this value
    params.delete(key);
    existing.filter(function(v) { return v !== value; }).forEach(function(v) {
      params.append(key, v);
    });
  } else {
    params.append(key, value);
  }
  window.location.search = params.toString();
}

function clearFilter(key) {
  var params = getParams();
  params.delete(key);
  window.location.search = params.toString();
}

function toggleSingle(key, value) {
  var params = getParams();
  if (params.get(key) === value) {
    params.delete(key);
  } else {
    params.set(key, value);
  }
  window.location.search = params.toString();
}

function clearAllFilters() {
  var params = getParams();
  ['tier', 'status', 'verif', 'channel'].forEach(function(k) { params.delete(k); });
  window.location.search = params.toString();
}

// ── Bulk select + floating action bar ──────────────────────────────────────

var bulkBar = document.getElementById('bulkBar');
var bulkCount = document.getElementById('bulkCount');
var selectAll = document.getElementById('selectAll');

function getChecked() {
  return Array.from(document.querySelectorAll('.row-check:not([disabled]):checked'));
}

function getVisibleChecks() {
  return Array.from(document.querySelectorAll('.row-check:not([disabled])')).filter(function(cb) {
    return cb.closest('tr').style.display !== 'none';
  });
}

function updateBulkBar() {
  var checked = getChecked();
  var n = checked.length;
  if (n > 0) {
    bulkBar.style.display = 'flex';
    bulkCount.textContent = n + ' selected';
    // Update live confirm placeholder if present
    var inp = document.getElementById('bulkConfirmInput');
    if (inp) inp.placeholder = 'SEND BATCH OF ' + n;
  } else {
    bulkBar.style.display = 'none';
  }
  // Sync master checkbox
  var visible = getVisibleChecks();
  if (selectAll) {
    selectAll.indeterminate = (checked.length > 0 && checked.length < visible.length);
    selectAll.checked = visible.length > 0 && checked.length === visible.length;
  }
}

function toggleAll(masterCb) {
  var visible = getVisibleChecks();
  visible.forEach(function(cb) { cb.checked = masterCb.checked; });
  updateBulkBar();
}

function clearBulkChecks() {
  document.querySelectorAll('.row-check').forEach(function(cb) { cb.checked = false; });
  if (selectAll) { selectAll.checked = false; selectAll.indeterminate = false; }
  bulkBar.style.display = 'none';
}

function submitBulkLive() {
  var inp = document.getElementById('bulkConfirmInput');
  var n = getChecked().length;
  var expected = 'SEND BATCH OF ' + n;
  if (!inp || inp.value.trim() !== expected) {
    if (inp) { inp.setCustomValidity('Type exactly: ' + expected); inp.reportValidity(); inp.addEventListener('input', function () { inp.setCustomValidity(''); }, {once: true}); }
    return;
  }
  document.getElementById('bulkForm').requestSubmit();
}

// ── Bulk schedule panel ────────────────────────────────────────────────────
function openBulkSchedule() {
  var checked = getChecked();
  var n = checked.length;
  if (n === 0) return;

  // Inject hidden draft_id inputs
  var container = document.getElementById('bulkSchedIdsContainer');
  container.innerHTML = '';
  checked.forEach(function(cb) {
    var inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = 'draft_ids';
    inp.value = cb.value;
    container.appendChild(inp);
  });

  // Update submit button and phrase
  document.getElementById('bulkSchedSubmitBtn').textContent = 'Schedule ' + n + ' sends';
  var phrase = 'SCHEDULE LIVE FOR ' + n;
  var phraseEl = document.getElementById('bulkSchedPhrase');
  if (phraseEl) phraseEl.textContent = phrase;
  var confirmInp = document.getElementById('bulkSchedConfirmInput');
  if (confirmInp) confirmInp.placeholder = phrase;

  setTomorrowDefault(document.getElementById('bsd'), document.getElementById('bst'));

  updateBulkSchedEta();
  document.getElementById('bulkScheduleOverlay').style.display = 'flex';
  document.getElementById('bsd').focus();
}

function updateBulkSchedEta() {
  var checked = getChecked();
  var n = checked.length || 1;
  var minS = n * 2, maxS = n * 32, avgS = n * 17;
  var d = document.getElementById('bsd').value;
  var t = document.getElementById('bst').value;
  var etaEl = document.getElementById('bulkSchedEta');
  var liveEl = document.getElementById('bulkSchedLiveConfirm');
  var modeEl = document.getElementById('bsm');

  if (liveEl) {
    var isLive = modeEl && modeEl.value === 'live';
    liveEl.hidden = !isLive;
    liveEl.style.display = isLive ? 'block' : 'none';
    var confirmation = liveEl.querySelector('input');
    if (confirmation) { confirmation.required = !!isLive; confirmation.disabled = !isLive; }
  }
  if (!d || !t) { if (etaEl) etaEl.style.display = 'none'; return; }
  var start = new Date(d + 'T' + t);
  var compMax = new Date(start.getTime() + maxS * 1000);
  var h = compMax.getHours(), mi = compMax.getMinutes();
  var ap = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  var compStr = h + ':' + String(mi).padStart(2,'0') + ' ' + ap;
  if (etaEl) {
    etaEl.innerHTML = '<strong>Estimated:</strong> ' + fmtDur(minS) + ' - ' + fmtDur(maxS) + ' (avg ' + fmtDur(avgS) + ').<br><strong>Completes by ~' + compStr + '</strong>';
    etaEl.style.display = '';
  }
}

var bsdEl = document.getElementById('bsd');
var bstEl = document.getElementById('bst');
var bsmEl = document.getElementById('bsm');
if (bsdEl) bsdEl.addEventListener('change', updateBulkSchedEta);
if (bstEl) bstEl.addEventListener('change', updateBulkSchedEta);
if (bsmEl) bsmEl.addEventListener('change', updateBulkSchedEta);

// ── Client-side search filter ───────────────────────────────────────────────
var searchInput = document.getElementById('partnerSearch');
var tableRows = document.querySelectorAll('#partnersTable tbody tr[data-name]');
function rowMatchesVerification(row) {
  var params = new URLSearchParams(window.location.search);
  var verification = params.getAll('verif');
  return !verification.length || verification.includes(row.dataset.verif);
}
tableRows.forEach(function (row) { if (!rowMatchesVerification(row)) row.style.display = 'none'; });
if (searchInput) {
  searchInput.addEventListener('input', function() {
    var q = this.value.toLowerCase().trim();
    tableRows.forEach(function(tr) {
      var name = tr.getAttribute('data-name') || '';
      var firm = tr.getAttribute('data-firm') || '';
      var notes = tr.getAttribute('data-notes') || '';
      tr.style.display = ((!q || name.includes(q) || firm.includes(q) || notes.includes(q)) && rowMatchesVerification(tr)) ? '' : 'none';
    });
    updateBulkBar();
  });
}

document.querySelectorAll('[data-action="partners_list-0"]').forEach(function (element) { element.addEventListener('click', function (event) { clearFilter('tier'); }); });
document.querySelectorAll('[data-action="partners_list-1"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleSingle('tier',this.dataset.arg0); }); });
document.querySelectorAll('[data-action="partners_list-2"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleSingle('status',this.dataset.arg0); }); });
document.querySelectorAll('[data-action="partners_list-3"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleFilter('verif',this.dataset.arg0); }); });
document.querySelectorAll('[data-action="partners_list-5"]').forEach(function (element) { element.addEventListener('click', function (event) { clearAllFilters(); }); });
document.querySelectorAll('[data-action="partners_list-6"]').forEach(function (element) { element.addEventListener('click', function (event) { document.getElementById('bulkLiveConfirm').style.display='flex'; }); });
document.querySelectorAll('[data-action="partners_list-7"]').forEach(function (element) { element.addEventListener('click', function (event) { submitBulkLive(); }); });
document.querySelectorAll('[data-action="partners_list-8"]').forEach(function (element) { element.addEventListener('click', function (event) { openBulkSchedule(); }); });
document.querySelectorAll('[data-action="partners_list-9"]').forEach(function (element) { element.addEventListener('click', function (event) { clearBulkChecks(); }); });
document.querySelectorAll('[data-action="partners_list-10"]').forEach(function (element) { element.addEventListener('change', function (event) { toggleAll(this); }); });
document.querySelectorAll('[data-action="partners_list-11"]').forEach(function (element) { element.addEventListener('change', function (event) { updateBulkBar(); }); });
document.querySelectorAll('[data-action="partners_list-12"]').forEach(function (element) { element.addEventListener('click', function (event) { document.getElementById('bulkScheduleOverlay').style.display='none'; }); });
}

  });
})();

// Test the currently entered SearXNG URL without saving it. Bound after the DOM exists, whatever the script load order.
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindSearxngTest);
else bindSearxngTest();
function bindSearxngTest() {
  document.querySelectorAll('[data-searxng-form]').forEach(function (form) {
    var button = form.querySelector('[data-searxng-test]');
    var article = form.closest('article');
    var note = article && article.querySelector('[data-searxng-note]');
    if (!button || !note) return;
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    button.addEventListener('click', function () {
      var data = new FormData(form);
      data.set('url', data.get('SEARXNG_URL'));
      button.disabled = true;
      note.textContent = 'Checking the connection...';
      fetch('/api/searxng/test', {method: 'POST', body: data, credentials: 'same-origin', headers: csrfMeta ? {'X-CSRF-Token': csrfMeta.content} : {}})
        .then(function (response) { return response.json(); })
        .then(function (payload) {
          note.textContent = payload.message || (payload.ok ? 'SearXNG is reachable.' : 'Could not reach SearXNG.');
          if (payload.ok && typeof window.setServiceStatus === 'function') window.setServiceStatus('searxng', 'ok');
        })
        .catch(function () { note.textContent = 'Could not reach the local connection check.'; })
        .finally(function () { button.disabled = false; });
    });
  });
}
