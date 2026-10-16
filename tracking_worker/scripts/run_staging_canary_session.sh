#!/usr/bin/env zsh
# Explicit remote canary for an already configured isolated Worker.
# Does not rotate keys, create resources, or deploy.
set -euo pipefail
umask 077
script_dir="${0:A:h}"
: "${TRACKING_WORKER_URL:?Set your isolated HTTPS Worker origin}"
: "${STAGING_TRACKING_SIGNING_KEY:?Set the isolated Worker signing key}"
: "${STAGING_INTERNAL_API_KEY:?Set the isolated Worker internal key}"
: "${STAGING_ACTIVATION_SIGNING_KEY:?Set the isolated Worker activation key}"
: "${STAGING_DASHBOARD_ACCESS_SECRET:?Set the isolated Worker dashboard secret}"
node "$script_dir/run_staging_canaries.mjs"
