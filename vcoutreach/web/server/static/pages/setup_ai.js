/* "Set up with AI" page and the Publik API card. Plain JS, no inline code (CSP: script-src 'self'). Safe to load twice. */
(function () {
  'use strict';
  // Glyph-safe label swap: keeps the icon span and replaces only the text.
  function ensureEyeGlyph(button) {
    if (button.querySelector('.icon')) return;
    var holder = document.createElement('span');
    holder.className = 'icon';
    holder.setAttribute('aria-hidden', 'true');
    holder.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" focusable="false"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/></svg>';
    button.insertBefore(holder, button.firstChild);
  }
  function setBtnLabel(button, text) {
    var nodes = Array.prototype.slice.call(button.childNodes);
    nodes.forEach(function (node) { if (node.nodeType === 3) button.removeChild(node); });
    button.appendChild(document.createTextNode(text));
  }
  if (window.__publikreachSetupAi) return;
  window.__publikreachSetupAi = true;

  var CHIP_WORDS = { connected: 'Connected', pending: 'Not connected', skipped: 'Skipped' };
  var FAIL_NET = 'Could not reach the app. Check that it is still running, then try again.';

  function csrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  function post(url, body) {
    var opts = {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'X-CSRF-Token': csrf() }
    };
    if (body) opts.body = body;
    return fetch(url, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        return { status: res.status, ok: res.ok, data: data || {} };
      });
    });
  }

  function say(el, text, tone) {
    if (!el) return;
    el.textContent = text || '';
    if (tone) el.setAttribute('data-tone', tone); else el.removeAttribute('data-tone');
  }

  /* ---------- copy buttons ---------- */
  function selectNode(node) {
    try {
      var range = document.createRange();
      range.selectNodeContents(node);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) { /* selection is a courtesy only */ }
  }

  function copyText(text, node) {
    return new Promise(function (resolve) {
      function legacy() {
        var done = false;
        try {
          var ta = document.createElement('textarea');
          ta.value = text;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.top = '0';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.select();
          done = document.execCommand('copy');
          document.body.removeChild(ta);
        } catch (e) { done = false; }
        if (!done && node) selectNode(node);
        resolve(done);
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () { resolve(true); }, legacy);
      } else {
        legacy();
      }
    });
  }

  function noteFor(btn) {
    var holder = btn.closest('.ai-snippet, .ai-restart');
    return holder ? holder.querySelector('[data-copy-note]') : null;
  }

  function wireCopy() {
    var timers = new WeakMap();
    function finish(btn, ok, node) {
      var note = noteFor(btn);
      say(note, ok ? 'Copied.' : 'Copy is blocked here. The text is selected: press Ctrl+C or Cmd+C.', ok ? 'ok' : '');
      var prior = timers.get(btn);
      if (prior) clearTimeout(prior);
      timers.set(btn, setTimeout(function () { say(note, ''); }, 6000));
      if (!ok && node) { try { node.focus(); } catch (e) { /* ignore */ } }
    }
    Array.prototype.forEach.call(document.querySelectorAll('[data-copy]'), function (btn) {
      btn.hidden = false;
      btn.addEventListener('click', function () {
        var node = document.getElementById(btn.getAttribute('data-copy'));
        if (!node) return;
        var snippet = btn.getAttribute('data-snippet');
        if (!snippet) {
          copyText(node.textContent, node).then(function (ok) { finish(btn, ok, node); });
          return;
        }
        /* The page shows a home-folder placeholder; the real command comes from the app on demand. */
        post('/api/mcp/snippet', new URLSearchParams({ key: snippet })).then(function (r) {
          var real = r.data && r.data.ok && typeof r.data.text === 'string' ? r.data.text : '';
          return copyText(real || node.textContent, node);
        }, function () {
          return copyText(node.textContent, node);
        }).then(function (ok) { finish(btn, ok, node); });
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-copy-text]'), function (btn) {
      btn.hidden = false;
      btn.addEventListener('click', function () {
        var text = btn.getAttribute('data-copy-text') || '';
        var src = btn.closest('.ai-restart');
        var quote = src ? src.querySelector('[data-copy-source]') : null;
        copyText(text, quote).then(function (ok) { finish(btn, ok, quote); });
      });
    });
  }

  /* ---------- Test MCP ---------- */
  function wireSelfTest() {
    var card = document.querySelector('[data-ai-test-card]');
    var btn = document.querySelector('[data-ai-test]');
    if (!card || !btn) return;
    btn.hidden = false;
    var chip = card.querySelector('[data-ai-test-chip]');
    var msg = card.querySelector('[data-ai-test-msg]');
    var busy = card.querySelector('[data-ai-busy]');
    var running = false;
    btn.addEventListener('click', function () {
      if (running) return;
      running = true;
      btn.disabled = true;
      if (busy) busy.hidden = false;
      say(msg, '');
      post('/api/mcp/self-test').then(function (r) {
        var ok = !!(r.data && r.data.ok);
        card.setAttribute('data-state', ok ? 'ok' : 'fail');
        if (chip) chip.textContent = ok ? 'Passed' : 'Failed';
        say(msg, (r.data && r.data.message) || (ok ? 'The MCP server started and listed its tools.' : 'The test could not run.'), ok ? 'ok' : 'fail');
      }, function () {
        card.setAttribute('data-state', 'fail');
        if (chip) chip.textContent = 'Failed';
        say(msg, FAIL_NET, 'fail');
      }).then(function () {
        running = false;
        btn.disabled = false;
        if (busy) busy.hidden = true;
      });
    });
  }

  /* ---------- Publik card ---------- */
  function wirePublik() {
    var card = document.querySelector('[data-pk-card]');
    if (!card) return;
    var form = card.querySelector('[data-pk-form]');
    var keyInput = card.querySelector('[data-pk-key]');
    var saveBtn = card.querySelector('[data-pk-save]');
    var testBtn = card.querySelector('[data-pk-test]');
    var removeBtn = card.querySelector('[data-pk-remove]');
    var chip = card.querySelector('[data-pk-chip]');
    var busy = card.querySelector('[data-pk-busy]');
    var busyText = card.querySelector('[data-pk-busy-text]');
    var note = card.querySelector('[data-pk-note]');
    var undo = card.querySelector('[data-pk-undo]');
    var skip = card.querySelector('[data-pk-skip]');
    var working = false;

    /* Show/Hide for the key field. */
    var wrap = card.querySelector('.pk-secret');
    if (wrap && keyInput) {
      var toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'svc-btn pk-toggle';
      ensureEyeGlyph(toggle);
      setBtnLabel(toggle, 'Show');
      toggle.setAttribute('aria-controls', keyInput.id);
      toggle.setAttribute('aria-pressed', 'false');
      toggle.setAttribute('aria-label', 'Show Publik API key');
      toggle.addEventListener('click', function () {
        var show = keyInput.type === 'password';
        keyInput.type = show ? 'text' : 'password';
        setBtnLabel(toggle, show ? 'Hide' : 'Show');
        toggle.setAttribute('aria-label', (show ? 'Hide' : 'Show') + ' Publik API key');
        toggle.setAttribute('aria-pressed', show ? 'true' : 'false');
      });
      wrap.appendChild(toggle);
    }

    function setBusy(on, text) {
      working = on;
      [saveBtn, testBtn, removeBtn].forEach(function (b) { if (b) b.disabled = on; });
      if (busy) busy.hidden = !on;
      if (busyText && text) busyText.textContent = text;
    }

    function chipState(state) {
      return state === 'connected' ? 'ok' : 'unset';
    }

    function apply(state) {
      card.setAttribute('data-pk-state', state);
      card.setAttribute('data-state', chipState(state));
      if (removeBtn) removeBtn.hidden = state !== 'connected';
      if (skip) skip.hidden = state !== 'pending';
      if (undo) undo.hidden = state !== 'skipped';
      /* Keep the shared Connections tile and count in step, then restore our own wording. */
      if (typeof window.setServiceStatus === 'function') {
        try { window.setServiceStatus('publik', chipState(state), CHIP_WORDS[state]); } catch (e) { /* tile may not exist */ }
      }
      if (chip) chip.textContent = CHIP_WORDS[state];
      var keyState = card.querySelector('[data-pk-keystate]');
      if (keyState) keyState.textContent = state === 'connected' ? 'Key saved' : 'Not set';
    }

    function currentState() { return card.getAttribute('data-pk-state') || 'pending'; }

    if (form) {
      form.addEventListener('submit', function (event) {
        if (working) { event.preventDefault(); return; }
        if (!window.fetch || !window.FormData) return;
        var submitter = event.submitter || null;
        var removing = !!(submitter && submitter.hasAttribute('data-pk-remove'));
        if (!removing && keyInput && !keyInput.value.trim()) {
          event.preventDefault();
          say(note, 'Paste a key first, or use Get key to create one.', 'fail');
          keyInput.focus();
          return;
        }
        event.preventDefault();
        var data = new FormData(form);
        if (removing) data.set('action', 'remove'); else data.delete('action');
        setBusy(true, removing ? 'Removing...' : 'Saving...');
        say(note, '');
        post(form.getAttribute('action'), data).then(function (r) {
          var d = r.data || {};
          if (!r.ok || d.ok === false) {
            say(note, d.error || d.message || 'The key was not saved.', 'fail');
            return;
          }
          if (d.status === 'configured') {
            if (keyInput) keyInput.value = '';
            apply('connected');
            say(note, 'Key saved. Use Test to confirm Publik accepts it (that is one real, billed call).', 'ok');
          } else if (d.status === 'unset') {
            if (keyInput) keyInput.value = '';
            apply('pending');
            say(note, 'Key removed.', 'ok');
          } else {
            say(note, d.message || 'No key entered; saved key unchanged.', '');
          }
        }, function () {
          /* Network failure: fall back to a normal submit so the page still works. */
          say(note, FAIL_NET, 'fail');
        }).then(function () { setBusy(false); });
      });
    }

    if (testBtn) {
      testBtn.addEventListener('click', function () {
        if (working) return;
        if (keyInput && keyInput.value.trim()) {
          say(note, 'Save the key first, then press Test.', 'fail');
          return;
        }
        setBusy(true, 'Testing with one small request...');
        say(note, '');
        post('/api/publik/test').then(function (r) {
          var d = r.data || {};
          var ok = !!d.ok;
          if (ok) {
            card.setAttribute('data-state', 'ok');
            if (chip) chip.textContent = CHIP_WORDS.connected;
          }
          say(note, d.message || d.error || (ok ? 'Connected.' : 'The test did not pass.'), ok ? 'ok' : 'fail');
        }, function () {
          say(note, FAIL_NET, 'fail');
        }).then(function () { setBusy(false); });
      });
    }

    /* Skip and Undo post the recommendation state; no page reload. */
    Array.prototype.forEach.call(card.querySelectorAll('[data-pk-reco]'), function (f) {
      f.addEventListener('submit', function (event) {
        if (!window.fetch || !window.FormData) return;
        event.preventDefault();
        if (working) return;
        var data = new FormData(f);
        var wanted = data.get('state') === 'skipped' ? 'skipped' : 'pending';
        setBusy(true, 'Saving...');
        post(f.getAttribute('action'), data).then(function (r) {
          var d = r.data || {};
          if (!r.ok || d.ok === false) {
            say(note, d.error || d.message || 'Could not save that choice.', 'fail');
            return;
          }
          /* A saved key always wins over the recommendation state. */
          if (currentState() !== 'connected') apply(wanted);
          say(note, wanted === 'skipped' ? 'Skipped. You can undo this any time.' : '', wanted === 'skipped' ? 'ok' : '');
        }, function () {
          say(note, FAIL_NET, 'fail');
        }).then(function () { setBusy(false); });
      });
    });
  }

  function init() {
    wireCopy();
    wireSelfTest();
    wirePublik();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
