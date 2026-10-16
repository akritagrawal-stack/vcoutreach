#!/usr/bin/env zsh
set -euo pipefail
umask 077

# The staging dashboard credential has exactly two durable homes: this user's
# macOS login Keychain and the isolated Cloudflare Worker secret store. It is
# never printed, written to a file, placed in a URL, or committed.
service="outreach-signal.tracking"
account="dashboard-access-secret"
origin="${TRACKING_WORKER_URL:-}"
action="${1:-status}"

secret_exists() {
  /usr/bin/security find-generic-password -s "$service" -a "$account" -w >/dev/null 2>&1
}

case "$action" in
  init)
    if secret_exists; then
      print -r -- "dashboard keychain entry already exists"
      exit 0
    fi
    secret="$(/usr/bin/openssl rand -hex 48)"
    /usr/bin/security add-generic-password -s "$service" -a "$account" -w "$secret" >/dev/null
    unset secret
    print -r -- "dashboard keychain entry initialized"
    ;;
  status)
    if secret_exists; then print -r -- "dashboard keychain entry present"; else print -r -- "dashboard keychain entry absent"; fi
    ;;
  copy)
    secret_exists || { print -u2 -r -- "dashboard keychain entry is absent; run: $0 init"; exit 1; }
    /usr/bin/security find-generic-password -s "$service" -a "$account" -w | /usr/bin/pbcopy
    print -r -- "dashboard access secret copied to clipboard"
    ;;
  open)
    [[ "$origin" == https://* && "$origin" != *[$'\n\r\t ']* && "$origin" != *'@'* && "$origin" != *'?'* && "$origin" != *'#'* ]] || { print -u2 'Set TRACKING_WORKER_URL to your HTTPS Worker origin'; exit 1; }
    secret_exists || { print -u2 -r -- "dashboard keychain entry is absent; run: $0 init"; exit 1; }
    /usr/bin/open "$origin/dashboard/login"
    print -r -- "dashboard login opened; run $0 copy, then paste into the form"
    ;;
  delete)
    /usr/bin/security delete-generic-password -s "$service" -a "$account" >/dev/null 2>&1 || true
    print -r -- "dashboard keychain entry removed"
    ;;
  *)
    print -u2 -r -- "usage: $0 {init|status|copy|open|delete}"
    exit 64
    ;;
esac
