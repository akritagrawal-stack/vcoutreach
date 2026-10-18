/* Publikreach interactions. Page data stays in escaped HTML attributes. */
(function () {
  'use strict';
  try {
    var theme = localStorage.getItem('publik-signal-theme');
    document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
  } catch (error) { document.documentElement.dataset.theme = 'light'; }
  document.addEventListener('DOMContentLoaded', function () {
    var page = document.body.dataset.page;
    var csrf = document.querySelector('meta[name="csrf-token"]').content;
    var themeButton = document.getElementById('themeToggle');
    themeButton.addEventListener('click', function () {
      var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      themeButton.setAttribute('aria-pressed', String(next === 'dark'));
      try { localStorage.setItem('publik-signal-theme', next); } catch (error) {}
    });
    themeButton.setAttribute('aria-pressed', String(document.documentElement.dataset.theme === 'dark'));
    document.querySelectorAll('[data-pitchbook-launch], [data-pitchbook-check]').forEach(function (button) {
      button.addEventListener('click', function () {
        var message = document.querySelector('[data-pitchbook-message]');
        var launching = button.hasAttribute('data-pitchbook-launch');
        var data = new FormData();
        data.append('csrf_token', csrf);
        button.disabled = true;
        message.textContent = launching ? 'Opening the dedicated PitchBook browser...' : 'Checking your browser session...';
        fetch(launching ? '/demo/pitchbook-visible' : '/demo/pitchbook-status', {
          method: launching ? 'POST' : 'GET',
          body: launching ? data : undefined,
          credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}
        }).then(function (response) {
          return response.json().then(function (payload) {
            message.textContent = payload.message || (response.ok ? String(payload.status || 'Session checked').replaceAll('_', ' ') : 'PitchBook connection unavailable.');
          });
        }).catch(function () { message.textContent = 'PitchBook connection unavailable. Check the local setup instructions.'; })
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

var _sortDir = {};  // col index -> 'asc'|'desc'

function sortTable(colIdx) {
  var table = document.getElementById('enrichTable');
  var tbody = table.querySelector('tbody');
  var rows = Array.from(tbody.querySelectorAll('tr[data-total]'));

  var dir = _sortDir[colIdx] === 'asc' ? 'desc' : 'asc';
  _sortDir[colIdx] = dir;
  table.querySelectorAll('thead th').forEach(function (cell, index) { cell.setAttribute('aria-sort', index === colIdx ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'); });

  rows.sort(function(a, b) {
    var av = a.cells[colIdx] ? a.cells[colIdx].textContent.trim() : '';
    var bv = b.cells[colIdx] ? b.cells[colIdx].textContent.trim() : '';

    // Numeric columns: Facts (10), Tier (1), source cols (3-9)
    var numericCols = [1, 3, 4, 5, 6, 7, 8, 9, 10];
    if (numericCols.indexOf(colIdx) !== -1) {
      // Use data attributes for source cols (3-9) for accuracy
      var srcAttrs = ['data-pb-api','data-pb','data-linkedin','data-web','data-web2','data-fw','data-eg'];
      if (colIdx >= 3 && colIdx <= 9) {
        var attr = srcAttrs[colIdx - 3];
        av = parseInt(a.getAttribute(attr) || '0', 10);
        bv = parseInt(b.getAttribute(attr) || '0', 10);
      } else if (colIdx === 10) {
        av = parseInt(a.getAttribute('data-total') || '0', 10);
        bv = parseInt(b.getAttribute('data-total') || '0', 10);
      } else if (colIdx === 1) {
        av = parseInt(a.getAttribute('data-tier') || '0', 10);
        bv = parseInt(b.getAttribute('data-tier') || '0', 10);
      }
      return dir === 'asc' ? av - bv : bv - av;
    }

    // Hook quality sort (11)
    if (colIdx === 11) {
      var order = {'cite_specific': 0, 'generic': 1, 'unknown': 2, 'no_enrichment': 3, 'no_hook': 4};
      av = order[a.getAttribute('data-hook-quality')] !== undefined ? order[a.getAttribute('data-hook-quality')] : 5;
      bv = order[b.getAttribute('data-hook-quality')] !== undefined ? order[b.getAttribute('data-hook-quality')] : 5;
      return dir === 'asc' ? av - bv : bv - av;
    }

    // String sort
    if (av < bv) return dir === 'asc' ? -1 : 1;
    if (av > bv) return dir === 'asc' ? 1 : -1;
    return 0;
  });

  rows.forEach(function(r) { tbody.appendChild(r); });
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
document.querySelectorAll('[data-action="enrichment_status-9"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(0); }); });
document.querySelectorAll('[data-action="enrichment_status-10"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(1); }); });
document.querySelectorAll('[data-action="enrichment_status-11"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(3); }); });
document.querySelectorAll('[data-action="enrichment_status-12"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(4); }); });
document.querySelectorAll('[data-action="enrichment_status-13"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(5); }); });
document.querySelectorAll('[data-action="enrichment_status-14"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(6); }); });
document.querySelectorAll('[data-action="enrichment_status-15"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(7); }); });
document.querySelectorAll('[data-action="enrichment_status-16"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(8); }); });
document.querySelectorAll('[data-action="enrichment_status-17"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(9); }); });
document.querySelectorAll('[data-action="enrichment_status-18"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(10); }); });
document.querySelectorAll('[data-action="enrichment_status-19"]').forEach(function (element) { element.addEventListener('click', function (event) { sortTable(11); }); });
}

if (page === 'partner_detail') {

function showText() {
  document.getElementById('preview-text').style.display = '';
  document.getElementById('preview-html').style.display = 'none';
  document.getElementById('tab-text').classList.add('active');
  document.getElementById('tab-html').classList.remove('active');
}
function showHtml() {
  document.getElementById('preview-text').style.display = 'none';
  document.getElementById('preview-html').style.display = '';
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

  // Default to tomorrow at 09:00
  var tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  var yyyy = tomorrow.getFullYear();
  var mm = String(tomorrow.getMonth() + 1).padStart(2, '0');
  var dd = String(tomorrow.getDate()).padStart(2, '0');
  dateEl.value = yyyy + '-' + mm + '-' + dd;
  timeEl.value = '09:00';

  function fmtDur(secs) {
    if (secs < 60) return secs + ' sec';
    if (secs < 3600) {
      var m = Math.floor(secs / 60), s = secs % 60;
      return s ? m + ' min ' + s + ' sec' : m + ' min';
    }
    var h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
    return m ? h + ' hr ' + m + ' min' : h + ' hr';
  }

  function updateEta() {
    var n = 1; // single partner
    var minS = n * 2, maxS = n * 32, avgS = n * 17;
    var d = dateEl.value, t = timeEl.value;
    if (!d || !t) { etaEl.style.display = 'none'; return; }
    var start = new Date(d + 'T' + t);
    var compMax = new Date(start.getTime() + maxS * 1000);
    var h = compMax.getHours(), mi = compMax.getMinutes();
    var ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    var compStr = h + ':' + String(mi).padStart(2,'0') + ' ' + ap;
    etaEl.innerHTML = '<strong>Estimated:</strong> ' + fmtDur(minS) + ' - ' + fmtDur(maxS) + ' (avg ' + fmtDur(avgS) + '). <strong>Completes by ~' + compStr + '</strong>';
    etaEl.style.display = '';
    if (liveEl) liveEl.style.display = (modeEl && modeEl.value === 'live') ? 'block' : 'none';
  }

  dateEl.addEventListener('change', updateEta);
  timeEl.addEventListener('change', updateEta);
  if (modeEl) modeEl.addEventListener('change', updateEta);
  updateEta();
})();

document.querySelectorAll('[data-action="partner_detail-0"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleReject(); }); });
document.querySelectorAll('[data-action="partner_detail-1"]').forEach(function (element) { element.addEventListener('click', function (event) { document.getElementById('liveSendPanel').style.display=document.getElementById('liveSendPanel').style.display==='none'?'block':'none'; }); });
document.querySelectorAll('[data-action="partner_detail-2"]').forEach(function (element) { element.addEventListener('click', function (event) { document.getElementById('schedulePanel1').style.display=document.getElementById('schedulePanel1').style.display==='none'?'block':'none'; }); });
document.querySelectorAll('[data-action="partner_detail-3"]').forEach(function (element) { element.addEventListener('click', function (event) { showText(); }); });
document.querySelectorAll('[data-action="partner_detail-4"]').forEach(function (element) { element.addEventListener('click', function (event) { showHtml(); }); });
}

if (page === 'dashboard') {

(function() {
  var launch = document.getElementById('launchVisibleRun');
  var mirror = document.getElementById('runCommandMirror');
  var chip = document.getElementById('runStateChip');
  var message = document.getElementById('runMessage');
  var browserLine = document.getElementById('runBrowserLine');
  var loginLine = document.getElementById('runLoginLine');
  var researchLine = document.getElementById('runResearchLine');
  var privacyToggle = document.getElementById('relationshipPrivacyToggle');
  var relationshipDesk = document.getElementById('relationshipDesk');
  var outreachStart = document.getElementById('startOutreachRun');
  var outreachStop = document.getElementById('stopOutreachRun');
  var outreachChip = document.getElementById('outreachStateChip');
  var outreachMessage = document.getElementById('outreachRunMessage');
  var outreachPollTimer = null;
  var pollTimer = null;
  var pollCount = 0;

  function stopPolling() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = null;
  }

  function setButtons(state) {
    var busy = state === 'browser_open' || state === 'refreshing_session';
    launch.disabled = busy;
    mirror.disabled = busy;
    if (state === 'session_ready') {
      launch.lastChild.textContent = ' Open PitchBook';
      mirror.textContent = 'OPEN / PITCHBOOK / READY';
    } else if (state === 'waiting_for_sign_in') {
      launch.lastChild.textContent = ' Bring PitchBook forward';
      mirror.textContent = 'OPEN / PITCHBOOK / SIGN IN';
    } else if (state === 'attention_required') {
      launch.lastChild.textContent = ' Try PitchBook again';
      mirror.textContent = 'RETRY / PITCHBOOK';
    } else {
      launch.lastChild.textContent = ' Launch visible run';
      mirror.textContent = 'RUN / PITCHBOOK / VISIBLE';
    }
  }

  function setState(state, copy) {
    setButtons(state);
    chip.className = 'status-chip ' + (state === 'session_ready' ? 'active' : state === 'attention_required' ? 'error' : 'working');
    chip.textContent = state.replaceAll('_', ' ').toUpperCase();
    message.textContent = copy;
    if (state === 'browser_open' || state === 'waiting_for_sign_in' || state === 'refreshing_session' || state === 'session_ready') {
      browserLine.classList.add('complete');
      browserLine.querySelector('em').textContent = 'visible';
    }
    if (state === 'waiting_for_sign_in') {
      loginLine.classList.add('working');
      loginLine.querySelector('em').textContent = 'in browser';
    }
    if (state === 'refreshing_session') {
      loginLine.classList.remove('working');
      loginLine.classList.add('complete');
      loginLine.querySelector('em').textContent = 'confirmed';
      researchLine.classList.add('working');
      researchLine.querySelector('em').textContent = 'refreshing';
    }
    if (state === 'session_ready') {
      loginLine.classList.remove('working');
      loginLine.classList.add('complete');
      loginLine.querySelector('em').textContent = 'confirmed';
      researchLine.classList.add('working');
      researchLine.querySelector('em').textContent = 'ready';
      stopPolling();
    }
    if (state === 'attention_required') {
      stopPolling();
    }
  }

  function pollStatus() {
    fetch('/demo/pitchbook-status', {credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function(response) {
        return response.json().then(function(data) {
          if (!response.ok) throw new Error(data.message || 'PitchBook status check failed.');
          return data;
        });
      })
      .then(function(data) {
        pollCount += 1;
        if (pollCount > 100 && data.status !== 'session_ready') {
          setState('attention_required', 'PitchBook is taking too long. Bring its Chrome window forward, finish sign-in, then try again.');
          return;
        }
        setState(data.status, data.message);
      })
      .catch(function() { setState('attention_required', 'Could not read the local run status.'); });
  }

  function startVisibleRun() {
    pollCount = 0;
    setState('browser_open', 'Opening the dedicated installed Chrome profile...');
    var form = new FormData();
    form.append('csrf_token', csrf);
    fetch('/demo/pitchbook-visible', {method: 'POST', body: form, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function(response) { return response.json().then(function(data) { return {ok: response.ok, data: data}; }); })
      .then(function(result) {
        if (!result.ok) throw new Error(result.data.message || 'Launch failed');
        setState(result.data.status, result.data.message);
        if (result.data.status !== 'session_ready') {
          stopPolling();
          pollTimer = window.setInterval(pollStatus, 1800);
          pollStatus();
        }
      })
      .catch(function(error) {
        setState('attention_required', error.message);
      });
  }

  function renderOutreachState(state) {
    var status = state.status || 'idle';
    outreachChip.className = 'status-chip ' + (status === 'complete' ? 'active' : status === 'running' ? 'working' : status === 'attention_required' ? 'error' : 'idle');
    outreachChip.textContent = status.replaceAll('_', ' ').toUpperCase();
    document.querySelectorAll('[data-outreach-step]').forEach(function(node) {
      var step = node.getAttribute('data-outreach-step');
      node.classList.toggle('complete', (state.completed_steps || []).includes(step));
      node.classList.toggle('working', status === 'running' && state.current_step === step);
      var value = state.counts && state.counts[step] ? state.counts[step] : 'waiting';
      node.querySelector('em').textContent = value;
    });
    outreachStart.disabled = status === 'running';
    outreachStop.disabled = status !== 'running';
    outreachMessage.textContent = status === 'complete'
      ? 'Preparation complete. Review artifacts remain local.'
      : status === 'stopped'
        ? 'Preparation stopped. No provider or Gmail action occurred.'
        : status === 'running'
          ? 'Local CLI is preparing review-only records.'
          : status === 'attention_required'
            ? 'The local run needs attention. Check the private run log.'
            : 'Ready in test mode. No delivery authority.';
    if (status !== 'running' && outreachPollTimer) {
      window.clearInterval(outreachPollTimer);
      outreachPollTimer = null;
    }
  }

  function readOutreachState() {
    fetch('/demo/outreach/status', {credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function(response) { return response.json(); })
      .then(renderOutreachState)
      .catch(function() { renderOutreachState({status: 'attention_required'}); });
  }

  function mutateOutreach(path) {
    var form = new FormData();
    form.append('csrf_token', csrf);
    return fetch(path, {method: 'POST', body: form, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function(response) {
        return response.json().then(function(data) {
          if (!response.ok) throw new Error(data.message || 'Local outreach control failed.');
          return data;
        });
      });
  }

  function startOutreach() {
    outreachStart.disabled = true;
    mutateOutreach('/demo/outreach/start')
      .then(function(state) {
        renderOutreachState(state);
        if (!outreachPollTimer) outreachPollTimer = window.setInterval(readOutreachState, 700);
        readOutreachState();
      })
      .catch(function(error) {
        renderOutreachState({status: 'attention_required'});
        outreachMessage.textContent = error.message;
      });
  }

  function stopOutreach() {
    outreachStop.disabled = true;
    mutateOutreach('/demo/outreach/stop')
      .then(renderOutreachState)
      .catch(function(error) {
        renderOutreachState({status: 'attention_required'});
        outreachMessage.textContent = error.message;
      });
  }

  if (launch) launch.addEventListener('click', startVisibleRun);
  if (mirror) mirror.addEventListener('click', startVisibleRun);
  if (outreachStart && outreachStop) {
    outreachStart.addEventListener('click', startOutreach);
    outreachStop.addEventListener('click', stopOutreach);
    readOutreachState();
  }
  if (privacyToggle && relationshipDesk) {
    privacyToggle.addEventListener('click', function() {
      var hidden = relationshipDesk.classList.toggle('privacy-on');
      privacyToggle.textContent = hidden ? 'REVEAL LOCAL DETAILS' : 'MASK LOCAL DETAILS';
    });
  }
})();


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
  var gmailChoose = document.querySelector('[data-gmail-choose]');
  var gmailConnect = document.querySelector('[data-gmail-connect]');
  var gmailRemove = document.querySelector('[data-gmail-remove]');
  var gmailFile = document.querySelector('[data-gmail-file]');
  var gmailMessage = document.querySelector('[data-gmail-message]');
  var gmailStates = document.querySelectorAll('[data-gmail-state]');
  var gmailPoll;
  function updateGmailState(payload) {
    if (typeof payload.state !== 'string') return;
    gmailStates.forEach(function (element) { element.textContent = payload.state; });
    document.querySelectorAll('[data-gmail-light]').forEach(function (element) {
      element.classList.toggle('ready', payload.state === 'connected');
      element.classList.toggle('guarded', payload.state !== 'connected');
    });
  }
  function gmailBusy(busy) {
    gmailChoose.disabled = gmailConnect.disabled = gmailRemove.disabled = busy;
  }
  function gmailStatus() {
    clearTimeout(gmailPoll);
    gmailPoll = null;
    return fetch('/api/gmail/status', {credentials: 'same-origin'}).then(function (response) {
      if (!response.ok) throw new Error('status');
      return response.json();
    }).then(function (payload) {
      updateGmailState(payload);
      gmailBusy(payload.running);
      if (payload.running) {
        gmailMessage.textContent = 'Complete consent in your system browser. This expires after three minutes.';
        gmailPoll = setTimeout(gmailStatus, 1500);
      } else if (payload.failed) {
        gmailMessage.textContent = 'Gmail authorization did not finish. Check your Desktop client and try Connect Gmail again.';
      } else if (payload.state === 'connected') {
        gmailMessage.textContent = 'Gmail connected. A private refresh token is saved for this workspace. TEST mode still applies.';
      }
    }).catch(function () {
      gmailMessage.textContent = 'Could not reach the local Gmail setup service. Reload Settings to retry.';
      gmailBusy(false);
    });
  }
  function gmailPost(path, data, successMessage) {
    return fetch(path, {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (!payload.ok) {
          gmailMessage.textContent = payload.error || 'Gmail setup is unavailable.';
          return;
        }
        updateGmailState(payload);
        gmailMessage.textContent = successMessage || 'OAuth client saved privately. Choose Connect Gmail to continue.';
        clearTimeout(gmailPoll);
        return gmailStatus();
      }).catch(function () { gmailMessage.textContent = 'Gmail setup is unavailable. Reload Settings to retry.'; });
  }
  if (gmailChoose) {
    gmailChoose.addEventListener('click', function () {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_oauth_client) {
        window.pywebview.api.choose_oauth_client(csrf).then(function (payload) {
          if (payload.cancelled) return;
          if (!payload.ok) { gmailMessage.textContent = payload.error; return; }
          updateGmailState(payload);
          gmailMessage.textContent = 'OAuth client saved privately. Choose Connect Gmail to continue.';
          return gmailStatus();
        }).catch(function () { gmailMessage.textContent = 'Could not choose the OAuth client. Try again.'; });
      } else {
        gmailFile.click();
      }
    });
    gmailFile.addEventListener('change', function () {
      if (!gmailFile.files.length) return;
      var upload = new FormData(document.querySelector('[data-gmail-upload]'));
      gmailFile.value = '';
      gmailPost('/api/gmail/client', upload);
    });
    gmailConnect.addEventListener('click', function () {
      gmailBusy(true);
      var data = new FormData();
      data.append('csrf_token', csrf);
      gmailPost('/api/gmail/connect', data, 'Starting Gmail consent in your system browser.').finally(function () {
        if (!gmailPoll) gmailBusy(false);
      });
    });
    gmailRemove.addEventListener('click', function () {
      if (!window.confirm('Remove the Gmail OAuth client and saved token from this workspace? You will need to import a client and connect again.')) return;
      gmailBusy(true);
      var data = new FormData();
      data.append('csrf_token', csrf);
      gmailPost('/api/gmail/disconnect', data, 'Gmail client and token removed. Import a client to connect again.').finally(function () {
        if (!gmailPoll) gmailBusy(false);
      });
    });
    gmailStatus();
  }

(function() {
  var form = document.getElementById('workspaceSettingsForm');
  if (!form) return;
  var overlay = document.getElementById('fieldGuideOverlay');
  var dismissButton = document.getElementById('fieldGuideDismiss');

  var guide = {
    product_name: ['Name the product exactly as customers should remember it.', ['Use the public product name.', 'Keep capitalization consistent.', 'Do not add a category tagline here.']],
    one_liner: ['Explain the product in one sentence using the buyer, the outcome, and the mechanism.', ['Names the product and the transformation.', 'Explains how the outcome is created.', 'Avoids a generic AI category label.']],
    company_background: ['Tell the shortest credible story from the original insight to the product that exists now.', ['Shows the change that created the company.', 'Connects distribution to product demand.', 'Leaves out chronology that does not change the thesis.']],
    problem_statement: ['State the costly behavior customers are stuck with before introducing the solution.', ['Names who experiences the problem.', 'Makes the cost or friction observable.', 'Does not smuggle the product into the problem.']],
    solution_statement: ['Describe what the product does, how it works, and what changes for the customer.', ['Connects mechanism to outcome.', 'Explains the product clearly.', 'Uses concrete workflows instead of broad AI claims.']],
    target_customer: ['Define the first buyer narrowly enough to find, qualify, and sell to repeatedly.', ['Names company stage or size.', 'Includes an accountable buyer.', 'Explains why the need is urgent now.']],
    business_model: ['Record current pricing separately from future pricing hypotheses and usage costs.', ['Distinguishes current revenue from tests.', 'Names what is included.', 'Makes variable API costs explicit.']],
    competitive_advantage: ['Describe why the product becomes harder to replace after real use.', ['Names the embedded customer context.', 'Avoids claiming an unproven network effect.', 'Links defensibility to retention or faster deployment.']],
    founder_background: ['Summarize complementary founder capabilities without exposing personal identity in the demo.', ['Connects experience to this company.', 'Uses evidence instead of prestige labels.', 'Keeps names and personal details out of this preview.']],
    pitch_deck_path: ['Point to the single current email-safe deck used by the outreach system.', ['Uses the current approved version.', 'Separates local path from shared URL.', 'Never exposes the local path in this preview.']],
    outreach_blurb_template: ['Define the reusable email structure while leaving recipient evidence as a required input.', ['Personalization comes before the company pitch.', 'Claims match the proof library.', 'The ask and attachment are explicit.']],
    proof_library: ['Store only claims that can be repeated in outreach with a known source and current date.', ['Uses exact numbers and qualifiers.', 'Separates founder-provided from independently verified.', 'Removes stale proof when the source changes.']],
    claim_guardrails: ['Write the statements the drafting engine must never infer or overstate.', ['Blocks predictable hallucinations.', 'Separates provider acceptance from delivery.', 'Distinguishes hypotheses from current facts.']],
    company_thesis: ['State the non-obvious belief that makes this company worth building now.', ['Explains why existing software economics can change.', 'Introduces the product after the problem.', 'Makes the final personalization layer concrete.']],
    traction_line: ['Use the smallest set of current numbers that proves demand, distribution, and paid behavior.', ['Includes a date or current qualifier.', 'Avoids combining incompatible snapshots.', 'Separates audience, users, community, and revenue.']],
    ask_line: ['State exactly what is being raised or requested and on what terms.', ['Includes the amount.', 'Includes the instrument and cap when approved.', 'Does not imply the round is closed.']],
    call_to_action: ['Ask for one easy next step that matches the recipient and the email.', ['Uses one action.', 'Keeps the time request small.', 'Avoids vague feedback requests.']]
  };

  var privateExamples = {
    operator_name: 'Company operator',
    operator_role: 'Founder or campaign owner',
    founder_background: 'Describe the team experience relevant to your product.',
    founder_line: 'Describe how your team came to build the product.',
    pitch_deck_path: 'Current compressed investor deck.pdf'
  };

  var defaultGuide = ['Answer this field with the exact information another operator or drafting system needs.', ['Use current, sourceable information.', 'Prefer concrete language over category labels.', 'Remove anything that does not change a decision.']];
  var titleNode = document.getElementById('fieldGuideTitle');
  var sectionNode = document.getElementById('fieldGuideSection');
  var promptNode = document.getElementById('fieldGuidePrompt');
  var exampleNode = document.getElementById('fieldGuideExample');
  var checksNode = document.getElementById('fieldGuideChecks');
  var countNode = document.getElementById('fieldGuideCount');
  var currentField = null;

  function fieldValue(field) {
    if (privateExamples[field.name]) return privateExamples[field.name];
    if (field.tagName === 'SELECT') return field.options[field.selectedIndex] ? field.options[field.selectedIndex].text : '';
    return field.value.trim();
  }

  function activateField(field) {
    if (!field || field.type === 'hidden') return;
    currentField = field;
    overlay.classList.remove('is-idle');
    var label = field.closest('label');
    var section = field.closest('.settings-section');
    var title = label && label.querySelector(':scope > span') ? label.querySelector(':scope > span').textContent.trim() : field.name.replaceAll('_', ' ');
    var sectionTitle = section && section.querySelector('.settings-section-heading h2') ? section.querySelector('.settings-section-heading h2').textContent.trim() : 'Workspace configuration';
    var guidance = guide[field.name] || defaultGuide;
    var value = fieldValue(field) || 'Add a concise response here.';
    var count = value.split(/\s+/).filter(Boolean).length;

    form.querySelectorAll('label.is-guide-active').forEach(function(node) {
      node.classList.remove('is-guide-active');
    });
    if (label) label.classList.add('is-guide-active');

    titleNode.textContent = title;
    sectionNode.textContent = sectionTitle;
    promptNode.textContent = guidance[0];
    exampleNode.textContent = value;
    checksNode.replaceChildren();
    guidance[1].forEach(function(item) {
      var li = document.createElement('li');
      li.textContent = item;
      checksNode.appendChild(li);
    });
    countNode.textContent = count + (count === 1 ? ' WORD' : ' WORDS');
  }

  function dismissGuide() {
    currentField = null;
    form.querySelectorAll('label.is-guide-active').forEach(function(node) {
      node.classList.remove('is-guide-active');
    });
    overlay.classList.add('is-idle');
  }

  form.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(function(field) {
    field.addEventListener('focus', function() { activateField(field); });
    field.addEventListener('click', function() { activateField(field); });
    field.addEventListener('input', function() { if (currentField === field) activateField(field); });
    field.addEventListener('change', function() { if (currentField === field) activateField(field); });
  });

  if (dismissButton) dismissButton.addEventListener('click', dismissGuide);
  document.addEventListener('pointerdown', function(event) {
    if (!currentField) return;
    if (event.target.closest('#fieldGuideOverlay')) return;
    if (event.target.closest('#workspaceSettingsForm input, #workspaceSettingsForm textarea, #workspaceSettingsForm select')) return;
    dismissGuide();
  });
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') dismissGuide();
  });

  var sampleState = document.getElementById('sampleEmailState');
  var sampleSubject = document.getElementById('sampleEmailSubject');
  var sampleAttachment = document.getElementById('sampleEmailAttachment');
  var sampleCopy = document.getElementById('sampleEmailCopy');
  var sampleTimer = null;

  function renderSampleEmail(payload) {
    sampleSubject.textContent = payload.subject;
    sampleAttachment.textContent = payload.attachment;
    sampleCopy.replaceChildren();
    (payload.paragraphs || []).forEach(function(text, index) {
      var paragraph = document.createElement('p');
      paragraph.textContent = text;
      if (index === 1) paragraph.className = 'email-hook';
      sampleCopy.appendChild(paragraph);
    });
    sampleState.textContent = 'CURRENT FORM';
    sampleState.className = 'status-chip active';
  }

  function refreshSampleEmail() {
    var data = new FormData(form);
    fetch('/demo/sample-email', {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function(response) {
        if (!response.ok) throw new Error('Sample preview failed.');
        return response.json();
      })
      .then(renderSampleEmail)
      .catch(function() {
        sampleState.textContent = 'PREVIEW ERROR';
        sampleState.className = 'status-chip error';
      });
  }

  form.addEventListener('input', function() {
    if (sampleTimer) window.clearTimeout(sampleTimer);
    sampleTimer = window.setTimeout(refreshSampleEmail, 180);
  });
  form.addEventListener('change', refreshSampleEmail);

  var geminiChip = document.getElementById('geminiStatusChip');
  var geminiButton = document.getElementById('testGemini');
  var geminiMessage = document.getElementById('geminiMessage');
  var geminiLatency = document.getElementById('geminiLatency');
  var geminiQuality = document.getElementById('geminiQuality');
  var geminiModel = document.getElementById('geminiModel');

  function renderGemini(payload) {
    var ok = Boolean(payload.ok);
    var configured = Boolean(payload.configured || payload.status === 'configured' || ok);
    geminiChip.textContent = ok ? 'TESTED' : configured ? 'CONFIGURED' : 'ATTENTION';
    geminiChip.className = 'status-chip ' + (ok ? 'active' : configured ? 'working' : 'error');
    if (payload.model) geminiModel.textContent = payload.model;
    if (payload.tested || ok) {
      geminiLatency.textContent = String(payload.latency_ms || 0) + ' ms';
      geminiQuality.textContent = String(payload.quality_score || 0) + ' / 100';
    }
    geminiMessage.textContent = ok
      ? 'Probe passed on 3 anonymized campaign emails. No raw content was stored.'
      : payload.error_code
        ? 'Probe stopped: ' + payload.error_code.replaceAll('_', ' ') + '.'
        : 'Key stored in the workspace settings. Run the probe when ready.';
  }

  document.querySelectorAll('.provider-key-form').forEach(function(keyForm) {
    keyForm.addEventListener('submit', function(event) {
      event.preventDefault();
      var keyInput = keyForm.querySelector('input[name="api_key"]');
      var status = keyForm.querySelector('[data-provider-status]');
      var message = keyForm.querySelector('[data-provider-message]');
      var data = new FormData(keyForm);
      status.textContent = 'STORING';
      status.className = 'status-chip working';
      fetch('/api/model-provider/key', {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
        .then(function(response) { return response.json().then(function(payload) { return {ok: response.ok, payload: payload}; }); })
        .then(function(result) {
          keyInput.value = '';
          status.textContent = result.ok ? 'READY' : 'ATTENTION';
          status.className = 'status-chip ' + (result.ok ? 'active' : 'error');
          message.textContent = result.ok
            ? 'Stored in the workspace settings. Available to the dashboard and CLI.'
            : 'Could not store key: ' + String(result.payload.error_code || 'settings unavailable').replaceAll('_', ' ') + '.';
          if (keyForm.dataset.provider === 'gemini' && result.ok) {
            renderGemini({configured: true, status: 'configured'});
          }
        })
        .catch(function() {
          status.textContent = 'ATTENTION';
          status.className = 'status-chip error';
          message.textContent = 'Could not reach the local settings service.';
        });
    });
  });

  document.querySelectorAll('.data-provider-key-form').forEach(function(keyForm) {
    var status = keyForm.querySelector('[data-data-provider-status]');
    var message = keyForm.querySelector('[data-data-provider-message]');
    var keyInput = keyForm.querySelector('input[name="api_key"]');
    var testButton = keyForm.querySelector('[data-test-data-provider]');
    keyForm.addEventListener('submit', function(event) {
      event.preventDefault();
      status.textContent = 'STORING';
      status.className = 'status-chip working';
      fetch('/api/data-provider/key', {method: 'POST', body: new FormData(keyForm), credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
        .then(function(response) { return response.json().then(function(payload) { return {ok: response.ok, payload: payload}; }); })
        .then(function(result) {
          keyInput.value = '';
          status.textContent = result.ok ? 'KEY READY' : 'ATTENTION';
          status.className = 'status-chip ' + (result.ok ? 'working' : 'error');
          message.textContent = result.ok
            ? 'Stored in the workspace settings. Test the connection before using credits.'
            : 'Could not store key: ' + String(result.payload.error_code || 'settings unavailable').replaceAll('_', ' ') + '.';
        })
        .catch(function() {
          status.textContent = 'ATTENTION';
          status.className = 'status-chip error';
          message.textContent = 'Could not reach the local settings service.';
        });
    });
    testButton.addEventListener('click', function() {
      var data = new FormData();
      data.append('csrf_token', csrf);
      data.append('provider', keyForm.dataset.provider);
      testButton.disabled = true;
      status.textContent = 'TESTING';
      status.className = 'status-chip working';
      fetch('/api/data-provider/test', {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
        .then(function(response) { return response.json().then(function(payload) { return {ok: response.ok, payload: payload}; }); })
        .then(function(result) {
          status.textContent = result.ok ? 'CONNECTED' : 'ATTENTION';
          status.className = 'status-chip ' + (result.ok ? 'active' : 'error');
          message.textContent = result.ok
            ? 'Connection verified without consuming credits' + (result.payload.credits_remaining != null ? '. Credits: ' + result.payload.credits_remaining + '.' : '.')
            : 'Connection check stopped: ' + String(result.payload.error_code || result.payload.status || 'provider unavailable').replaceAll('_', ' ') + '.';
        })
        .catch(function() {
          status.textContent = 'ATTENTION';
          status.className = 'status-chip error';
          message.textContent = 'Could not reach the provider health endpoint.';
        })
        .finally(function() { testButton.disabled = false; });
    });
  });

  var cliProvider = document.getElementById('modelCliProvider');
  var cliCommand = document.getElementById('modelCliCommand');
  var copyCli = document.getElementById('copyModelCli');
  function updateCliCommand() {
    cliCommand.textContent = 'vcoutreach --help';
  }
  if (cliProvider && cliCommand && copyCli) { cliProvider.addEventListener('change', updateCliCommand);
  copyCli.addEventListener('click', function() {
    navigator.clipboard.writeText(cliCommand.textContent).then(function() {
      copyCli.textContent = 'COPIED';
      window.setTimeout(function() { copyCli.textContent = 'COPY COMMAND'; }, 1000);
    });
  });

  }
  geminiButton.addEventListener('click', function() {
    geminiButton.disabled = true;
    geminiMessage.textContent = 'Running anonymized quality and latency probe...';
    var data = new FormData();
    data.append('csrf_token', csrf);
    fetch('/api/gemini/test', {method: 'POST', body: data, credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
      .then(function(response) { return response.json(); })
      .then(renderGemini)
      .catch(function() { renderGemini({ok: false, error_code: 'gemini_request_failed'}); })
      .finally(function() { geminiButton.disabled = false; });
  });
})();


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

function clearChecks() {
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
function fmtDurBulk(secs) {
  if (secs < 60) return secs + ' sec';
  if (secs < 3600) {
    var m = Math.floor(secs / 60), s = secs % 60;
    return s ? m + ' min ' + s + ' sec' : m + ' min';
  }
  var h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  return m ? h + ' hr ' + m + ' min' : h + ' hr';
}

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

  // Default date/time
  var tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  var yyyy = tomorrow.getFullYear();
  var mm = String(tomorrow.getMonth() + 1).padStart(2, '0');
  var dd = String(tomorrow.getDate()).padStart(2, '0');
  document.getElementById('bsd').value = yyyy + '-' + mm + '-' + dd;
  document.getElementById('bst').value = '09:00';

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

  if (!d || !t) { if (etaEl) etaEl.style.display = 'none'; return; }
  var start = new Date(d + 'T' + t);
  var compMax = new Date(start.getTime() + maxS * 1000);
  var h = compMax.getHours(), mi = compMax.getMinutes();
  var ap = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  var compStr = h + ':' + String(mi).padStart(2,'0') + ' ' + ap;
  if (etaEl) {
    etaEl.innerHTML = '<strong>Estimated:</strong> ' + fmtDurBulk(minS) + ' - ' + fmtDurBulk(maxS) + ' (avg ' + fmtDurBulk(avgS) + ').<br><strong>Completes by ~' + compStr + '</strong>';
    etaEl.style.display = '';
  }
  if (liveEl) liveEl.style.display = (modeEl && modeEl.value === 'live') ? 'block' : 'none';
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
  var channel = params.get('channel');
  return (!verification.length || verification.includes(row.dataset.verif)) && (!channel || row.dataset.channel === channel);
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
document.querySelectorAll('[data-action="partners_list-4"]').forEach(function (element) { element.addEventListener('click', function (event) { toggleSingle('channel','twitter'); }); });
document.querySelectorAll('[data-action="partners_list-5"]').forEach(function (element) { element.addEventListener('click', function (event) { clearAllFilters(); }); });
document.querySelectorAll('[data-action="partners_list-6"]').forEach(function (element) { element.addEventListener('click', function (event) { document.getElementById('bulkLiveConfirm').style.display='flex'; }); });
document.querySelectorAll('[data-action="partners_list-7"]').forEach(function (element) { element.addEventListener('click', function (event) { submitBulkLive(); }); });
document.querySelectorAll('[data-action="partners_list-8"]').forEach(function (element) { element.addEventListener('click', function (event) { openBulkSchedule(); }); });
document.querySelectorAll('[data-action="partners_list-9"]').forEach(function (element) { element.addEventListener('click', function (event) { clearChecks(); }); });
document.querySelectorAll('[data-action="partners_list-10"]').forEach(function (element) { element.addEventListener('change', function (event) { toggleAll(this); }); });
document.querySelectorAll('[data-action="partners_list-11"]').forEach(function (element) { element.addEventListener('change', function (event) { updateBulkBar(); }); });
document.querySelectorAll('[data-action="partners_list-12"]').forEach(function (element) { element.addEventListener('click', function (event) { document.getElementById('bulkScheduleOverlay').style.display='none'; }); });
}

  });
})();
