# Publik tracking Worker

Deploy this optional Worker to your own Cloudflare account. It serves signed redirects, open pixels, deck links, unsubscribe links, and authenticated tracking summaries. Cloudflare Workers and D1 usage can incur charges. The local web app works without tracking configured.

## Deploy to your account

1. Install Node 24 or later and Wrangler. From this directory run `npx wrangler login` and confirm the account is yours.
2. Copy `wrangler.toml.example` to `wrangler.toml`. Choose a Worker name and run `npx wrangler d1 create YOUR_DATABASE_NAME`. Put the returned database ID and name in your local config. No account or database IDs ship in this package.
3. Set `PUBLIC_ORIGIN` to the exact HTTPS origin of your Worker. Set `DESTINATION_ORIGINS` to a comma-separated list of allowed HTTPS origins. Set `DESTINATION_MAP_JSON` to your own destinations. The sample uses `example.com` placeholders. Destination URLs cannot carry credentials, query strings, or fragments.
4. Generate four independent random secrets, each at least 32 characters. Store each with `npx wrangler secret put NAME`: `TRACKING_SIGNING_KEY`, `INTERNAL_API_KEY`, `ACTIVATION_SIGNING_KEY`, and `DASHBOARD_ACCESS_SECRET`. Keep them in your own secret store. Never put them in source, URLs, or screenshots.
5. Run `npx wrangler d1 migrations apply YOUR_DATABASE_NAME --remote`, then `npx wrangler deploy`. The hourly scheduled handler removes old telemetry.
6. In the local app's Settings, enter your HTTPS Worker origin and the dashboard secret. Alternatively set `TRACKING_WORKER_URL` and `TRACKING_WORKER_TOKEN` in your workspace `.env` (mode 0600) or environment. The token is `DASHBOARD_ACCESS_SECRET`, not your Cloudflare account API token.
7. Confirm an authenticated `/api/dashboard` request works. Invalid credentials should fail. Keep live links and telemetry separate from any synthetic canary deployment.

Cloudflare Access is an alternative to the dashboard secret for browser access. Create your own Access application, then configure `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, and pinned `ACCESS_JWKS_JSON`. The local Python adapter uses the dashboard bearer secret.

## Link creation and activation

The authenticated `POST /internal/links` endpoint creates an opaque link after checking its signed link hash. `POST /internal/activate` requires an HMAC over a successful provider receipt using the activation secret. Creating a link alone cannot collect events. The compatible receipt formats and authority checks live in `src/logic.mjs`; a sending integration must produce these receipts and keep an activation journal under the workspace `outputs/` directory. This adapter reads existing journals. Connecting a Worker in Settings does not automatically instrument emails or create receipt journals.

Public routes are `/r/<link_id>` (redirect), `/o/<link_id>` (pixel), `/d/<link_id>` (deck), and `/u/<link_id>` (unsubscribe). Per-action summaries and events use `/api/actions/<action_id>` and `/api/actions/<action_id>/events`; aggregate counts use `/api/dashboard`. All URLs are signed. Tracking records request classifications, which do not prove a person read an email. The Worker stores opaque action IDs, not recipient addresses; local journals provide the email-hash join.

## Your deck

By default the `deck` destination redirects to your configured HTTPS deck URL. To serve your own PDF privately through the signed route, run `node scripts/stage_deck_asset.mjs /path/to/your.pdf EXPECTED_SHA256`, then enable the commented `[assets]` section in your local Wrangler config and redeploy. The file is staged as `deck_assets/deck.pdf`. Never publish another user's deck or commit generated deck assets.

## Offline verification

`node --check src/logic.mjs` checks JavaScript syntax. `node --test test/*.test.mjs` uses fake D1 and asset bindings without a Cloudflare account. The existing tests are protocol checks; actual Cloudflare deployment, Access setup, billing and delivery require your own account and credentials.

`scripts/run_staging_canary_session.sh` runs an explicit remote synthetic probe against an already configured isolated Worker using user-supplied environment variables. It does not deploy or rotate secrets. Keep local Wrangler config, `.dev.vars`, receipt outputs, generated assets and credentials private.
