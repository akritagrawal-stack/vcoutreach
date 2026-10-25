/* Copy button for the read-only "Example pitch" block (_example_pitch.html). Same-origin script, CSP safe. */
(function () {
  function bind() {
    var buttons = document.querySelectorAll('#examplePitch [data-copy-target]');
    Array.prototype.forEach.call(buttons, function (button) {
      if (button.dataset.bound) return;
      button.dataset.bound = '1';
      button.addEventListener('click', function () {
        var area = document.getElementById(button.dataset.copyTarget);
        var label = button.dataset.copyLabel || 'Copy';
        if (!area) return;
        function done(text) {
          button.textContent = text;
          window.setTimeout(function () { button.textContent = label; }, 1800);
        }
        function fallback() {
          try {
            area.focus();
            area.select();
            var ok = document.execCommand('copy');
            area.blur();
            done(ok ? 'Copied' : 'Select and copy manually');
          } catch (e) {
            done('Select and copy manually');
          }
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(area.value).then(function () { done('Copied'); }, fallback);
        } else {
          fallback();
        }
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
