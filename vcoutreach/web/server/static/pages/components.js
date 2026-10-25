/* Publikreach shared helpers: relative local time for <time datetime data-rel>. No dependencies. */
(function () {
  'use strict';
  var UNITS = [['year', 31536000], ['month', 2592000], ['day', 86400], ['hour', 3600], ['minute', 60]];
  function rel(date, now) {
    var secs = Math.round((date.getTime() - now) / 1000), abs = Math.abs(secs);
    if (abs < 45) return 'just now';
    var fmt = typeof Intl !== 'undefined' && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }) : null;
    for (var i = 0; i < UNITS.length; i++) {
      if (abs >= UNITS[i][1] || i === UNITS.length - 1) {
        var n = Math.round(secs / UNITS[i][1]);
        if (fmt) return fmt.format(n, UNITS[i][0]);
        var k = Math.abs(n) + ' ' + UNITS[i][0] + (Math.abs(n) === 1 ? '' : 's');
        return n < 0 ? k + ' ago' : 'in ' + k;
      }
    }
    return '';
  }
  function exact(date) {
    try { return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }); } catch (e) { return date.toString(); }
  }
  function run(root) {
    var els = (root || document).querySelectorAll('time[data-rel][datetime]');
    var now = Date.now();
    for (var i = 0; i < els.length; i++) {
      var d = new Date(els[i].getAttribute('datetime'));
      if (isNaN(d.getTime())) continue;
      els[i].textContent = rel(d, now);
      els[i].title = exact(d);
    }
  }
  window.pkRelativeTimes = run;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { run(); });
  else run();
})();
