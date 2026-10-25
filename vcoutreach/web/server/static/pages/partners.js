// Send queue (partners_list): the filter selects reload the page with the chosen filter.
// Search and row selection are handled by app.js. Guarded so other pages that load this file are unaffected.
(function () {
  var selects = document.querySelectorAll('select[data-pl-filter]');
  if (!selects.length) return;
  var QUALITY = {verified: ['verified', 'verified_strong'], risky: ['weak'], unknown: ['unverified']};
  selects.forEach(function (select) {
    select.addEventListener('change', function () {
      var params = new URLSearchParams(window.location.search);
      var key = select.getAttribute('data-pl-filter');
      var value = select.value;
      params.delete(key);
      if (value) {
        if (key === 'verif') {
          (QUALITY[value] || []).forEach(function (v) { params.append('verif', v); });
        } else {
          params.set(key, value);
        }
      }
      var query = params.toString();
      window.location.search = query;
    });
  });
})();
