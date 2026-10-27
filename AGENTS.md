# AGENTS.md

## publik API

- App slug: `publikreach`. Base URL: https://publikhq.com/api/v1. Contract: https://publikhq.com/developers.
- Module: `vcoutreach/publik_api.py` (stdlib only). Provider: `vcoutreach/providers/publik.py`.
- `publik-app-token.txt` is committed by design (it can only mint capped keys attributed to this app). It is read from the PyInstaller bundle root (`sys._MEIPASS`), the module folder or the nearest parent; `PUBLIK_APP_TOKEN` overrides it. Never put a user key in it.
- Key resolution order: settings `publik_api_key` (the user's own key), then `PUBLIK_API_KEY` env, then the credential file (`~/Library/Application Support/publik/apps/publikreach.json`, mode 0600). Set `PUBLIKREACH_NO_CREDENTIAL_FILE=1` to skip the file lookup (tests).
- Routes: `GET /api/publik/status`, `POST /api/publik/provision`. They never return the key.
- Self-test (provisions for real, run on the built app): `PUBLIK_SELF_TEST=1 dist/Publikreach.app/Contents/MacOS/Publikreach`. It prints one line and exits before any window or server starts.
- Tests: `tests/test_publik_wiring.py` (fake transports, no network).
