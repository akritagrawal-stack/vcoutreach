/* Publikreach crm lane script: page-specific behaviour only. */
(function () {
  document.querySelectorAll('select[data-crm-autosubmit]').forEach(function (select) {
    select.addEventListener('change', function () {
      if (select.form) { select.form.submit(); }
    });
  });
  /* A link or button with data-open-details="<id>" opens that closed <details> block, then lets the jump happen. */
  document.querySelectorAll('[data-open-details]').forEach(function (trigger) {
    trigger.addEventListener('click', function () {
      var target = document.getElementById(trigger.getAttribute('data-open-details'));
      if (target && target.tagName === 'DETAILS') { target.open = true; }
    });
  });
  if (window.location.hash === '#crmImport') {
    var imp = document.getElementById('crmImport');
    if (imp && imp.tagName === 'DETAILS') { imp.open = true; }
  }
})();
