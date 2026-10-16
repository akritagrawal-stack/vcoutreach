#!/bin/zsh
# Start a dedicated local PitchBook browser using workspace data only.
set -euo pipefail
umask 077
chrome_app="/Applications/Google Chrome.app"
chrome_bin="${chrome_app}/Contents/MacOS/Google Chrome"
debug_port="${PITCHBOOK_CDP_PORT:-9222}"
workspace_dir="${OUTREACH_HOME:-$PWD}"
workspace_dir="${workspace_dir:A}"
profile_dir="${PITCHBOOK_CDP_PROFILE_DIR:-${workspace_dir}/data/.pitchbook_cdp_profile}"
profile_dir="${profile_dir:A}"
if [[ "${profile_dir}" != "${workspace_dir}/data/"* ]]; then
  print -u2 'PitchBook profile must be inside workspace data/'
  exit 1
fi
if [[ ! "${debug_port}" =~ '^[0-9]+$' ]] || (( debug_port < 1024 || debug_port > 65535 )); then
  print -u2 'PITCHBOOK_CDP_PORT must be between 1024 and 65535'
  exit 1
fi
if [[ ! -x "${chrome_bin}" ]]; then
  print -u2 'Install Google Chrome in /Applications first'
  exit 1
fi
if /usr/sbin/lsof -nP -iTCP:"${debug_port}" -sTCP:LISTEN >/dev/null 2>&1; then
  print -u2 'The requested CDP port is already in use'
  exit 1
fi
/bin/mkdir -p "${profile_dir}"
/usr/bin/open -na "${chrome_app}" --args \
  '--remote-debugging-address=127.0.0.1' \
  "--remote-debugging-port=${debug_port}" \
  "--user-data-dir=${profile_dir}" \
  'https://my.pitchbook.com'
print 'Sign in with your own PitchBook account, then refresh cookies from Settings.'
