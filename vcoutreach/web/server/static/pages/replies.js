/* Publikreach replies lane script: page-specific behaviour only (Replies list). */
(function () {
  'use strict';

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  ready(function () {
    var table = document.getElementById('replyTable');
    var bar = document.getElementById('bulkForm');
    var kebab = document.getElementById('addReply');

    if (window.pkRelativeTimes) window.pkRelativeTimes(document);

    // Close the add-a-reply menu on outside click or Escape.
    if (kebab) {
      document.addEventListener('click', function (e) {
        if (kebab.open && !kebab.contains(e.target)) kebab.open = false;
      });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && kebab.open) { kebab.open = false; var s = kebab.querySelector('summary'); if (s) s.focus(); }
      });
    }

    if (!table) return;

    var boxes = function () { return Array.prototype.slice.call(table.querySelectorAll('input[name="reply_id"]')); };
    var selectAll = document.getElementById('replySelectAll');

    function refreshBar() {
      var checked = boxes().filter(function (b) { return b.checked; });
      boxes().forEach(function (b) {
        var tr = b.closest('tr');
        if (tr) tr.classList.toggle('is-selected', b.checked);
      });
      if (bar) {
        bar.hidden = checked.length === 0;
        var count = bar.querySelector('[data-bulk-count]');
        if (count) count.textContent = String(checked.length);
      }
      if (selectAll) {
        var all = boxes();
        selectAll.checked = all.length > 0 && checked.length === all.length;
        selectAll.indeterminate = checked.length > 0 && checked.length < all.length;
      }
    }

    table.addEventListener('change', function (e) {
      var t = e.target;
      if (t === selectAll) {
        boxes().forEach(function (b) { b.checked = selectAll.checked; });
        refreshBar();
      } else if (t.name === 'reply_id') {
        refreshBar();
      } else if (t.classList && t.classList.contains('reply-pill-select')) {
        saveLabel(t);
      }
    });

    // Whole-row navigation, except when the click was on something interactive.
    table.addEventListener('click', function (e) {
      if (e.target.closest('a, button, input, select, label, summary, form, textarea')) return;
      var tr = e.target.closest('tr[data-href]');
      if (tr && !(window.getSelection && String(window.getSelection()))) window.location.href = tr.getAttribute('data-href');
    });

    function setCounts(data) {
      if (!data || !data.counts) return;
      Object.keys(data.counts).forEach(function (key) {
        var el = document.querySelector('[data-count-for="' + key + '"]');
        if (el) el.textContent = String(data.counts[key]);
      });
    }

    function saveLabel(select) {
      var form = select.form;
      var tr = select.closest('tr');
      var previous = select.getAttribute('data-initial') || '';
      if (!select.value) { select.value = previous; return; }
      var body = new URLSearchParams();
      body.set('user_label', select.value);
      body.set('csrf_token', csrfToken() || (form.querySelector('input[name="csrf_token"]') || {}).value || '');
      select.disabled = true;
      fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Accept': 'application/json',
          'X-Requested-With': 'fetch',
          'X-CSRF-Token': csrfToken(),
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        body: body.toString()
      }).then(function (res) {
        return res.json().then(function (data) { return { ok: res.ok, data: data }; });
      }).then(function (out) {
        select.disabled = false;
        if (!out.ok || !out.data || !out.data.ok) throw new Error((out.data && out.data.error) || 'Could not save the label');
        var d = out.data;
        select.setAttribute('data-initial', d.label);
        select.className = 'pk-pill pk-pill--' + d.tone + ' reply-pill-select';
        if (tr) tr.setAttribute('data-bucket', d.bucket);
        setCounts(d);
        var active = document.querySelector('.reply-tabs a.is-active');
        var activeKey = active ? active.getAttribute('data-tab') : 'all';
        if (tr && activeKey && activeKey !== 'all' && d.bucket !== activeKey) {
          tr.remove();
          refreshBar();
          if (!table.querySelector('tbody tr')) window.location.reload();
        }
      }).catch(function (err) {
        select.disabled = false;
        select.value = previous;
        var note = document.createElement('span');
        note.className = 'pk-note reply-error';
        note.setAttribute('role', 'alert');
        note.textContent = err.message || 'Could not save the label';
        if (!form.querySelector('.reply-error')) form.appendChild(note);
        setTimeout(function () { if (note.parentNode) note.parentNode.removeChild(note); }, 4000);
      });
    }

    refreshBar();
  });
})();
