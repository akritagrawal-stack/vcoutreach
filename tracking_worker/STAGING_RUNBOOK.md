# Isolated tracking checks

Follow [README.md](README.md) to create a separate Worker and D1 database in your own Cloudflare account. Use fresh secrets and destinations you control. Run the offline protocol checks first.

For an explicit remote probe, set `TRACKING_WORKER_URL`, `STAGING_TRACKING_SIGNING_KEY`, `STAGING_INTERNAL_API_KEY`, `STAGING_ACTIVATION_SIGNING_KEY`, and `STAGING_DASHBOARD_ACCESS_SECRET` for that isolated deployment. Invoke `scripts/run_staging_canary_session.sh`. The probe creates synthetic opaque link and event records, performs no email delivery, and reports status without printing secrets. It does not deploy, rotate credentials, or touch a production configuration.

On macOS, `scripts/dashboard_keychain.sh init` optionally creates a dashboard secret under the generic service `outreach-signal.tracking`. `copy` copies it to your clipboard; `open` opens the login page at your `TRACKING_WORKER_URL`; `delete` removes that Keychain entry. Upload the same value to your own Worker secret store if you use this helper.
