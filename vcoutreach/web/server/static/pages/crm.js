/* Publikreach crm lane script: page-specific behaviour only. */
(function () {
  document.querySelectorAll('select[data-crm-autosubmit]').forEach(function (select) {
    select.addEventListener('change', function () {
      if (select.form) { select.form.submit(); }
    });
  });
  document.querySelectorAll('[data-crm-switch-scope]').forEach(function (scope) {
    var select = scope.querySelector('select[data-crm-switch]');
    if (!select) { return; }
    var attr = select.getAttribute('data-crm-switch');
    var apply = function () {
      var value = select.value;
      scope.querySelectorAll('tr[data-' + attr + ']').forEach(function (row) {
        row.hidden = !!value && row.getAttribute('data-' + attr) !== value;
      });
      scope.querySelectorAll('[data-' + attr + '-note]').forEach(function (note) {
        note.hidden = !!value && note.getAttribute('data-' + attr + '-note') !== value;
      });
    };
    select.addEventListener('change', apply);
    apply();
  });
})();
