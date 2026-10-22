# Core and templates contract

Binding: SPEC_V3. Keep Publik branding. Use plain hyphens only. Existing route endpoint names and existing template contexts are preserved unless noted below. All templates receive csrf_token, g_send_mode, g_test_recipient, g_workspace, unread_replies, unread_interested, unread_by_label, sent_to_map, candidates_unreviewed, pending_schedules, pitchbook_enabled, pitchbook_lookups_used, pitchbook_lookups_cap. All POST forms must include csrf_token. No inline scripts or external resources.

## Additions

- GET /crm/drafts endpoint crm_drafts renders crm_drafts.html with drafts (list of dicts), draft_count, draft_counts (all, ok, blocked), type_filter, status_filter, query, providers (auto, template, publik, openai, anthropic), provider_status. Each draft has slug, contact_id, partner_id (nullable), recipient_name, recipient_email, firm, subject, body, status (ok/blocked), issues (list of code/message/severity dicts), word_count, lane, editor_url, review_state (stored approval), approval_state (pending/approved/rejected/blocked), blocked_reason, grounding. Blocking checks take precedence over grounding and approval for display. This replaces manifest review.
- POST /crm/drafts/write endpoint crm_drafts_write: provider; slugs (repeated or comma separated), optional slug, overwrite=1. Redirect with flash.
- GET/POST /crm/drafts/<slug> endpoint crm_draft_editor renders crm_draft.html with draft (same dict), contact (dict or empty), issues, drafts (same list), active_page='drafts'. POST accepts subject, body, lane, in_reply_to, references, action=review or reviewed=1. Saves and re-lints.
- GET /crm/send-plan endpoint crm_send_plan renders crm_send_plan.html with plan (engine gate result dict), rows (list of per-draft dicts), summary (counts), error, active_page='send_plan'. Read only. Say 'Nothing is sent from this screen.'
- GET/POST /crm/new endpoint crm_new renders crm_form.html with contact (dict), errors (list), stages, active_page='crm'. Fields: name, email, org, role, facts (pipe separated), source_urls (pipe separated), lane, thread_subject, in_reply_to, references, first_name, stage, tags, notes, next_action. Engine validation.
- POST /crm/import endpoint crm_import: multipart file named file or csv_file, or csv_text. Validate whole CSV before writing. Duplicate email rows are skipped and counted, including duplicates already in the workspace. Malformed rows reject the whole import with original row numbers. Redirect to crm_dashboard. GET /crm/export.csv endpoint crm_export.
- POST /crm/<int:partner_id>/delete endpoint crm_contact_delete: confirm=yes or confirm_text equal to contact slug. Deletes the contact, its SQL drafts and dependent CRM rows, and archives draft files in drafts/removed.
- POST /sample/load endpoint sample_load: empty workspaces only, refuses existing contacts/sender/drafts. Loads packaged sender.json, contacts.sample.csv, replies.json. Redirect to crm_dashboard.
- POST /settings/sender endpoint save_sender_profile: engine sender fields, including first_touch_subject; list fields newline separated; approved_claims JSON. POST /settings/keys endpoint save_keys: key name and value or allowlisted fields. Blank API keys keep saved values; action=remove or clear=<key name> explicitly removes them. Empty TEST_RECIPIENT_EMAIL clears the test recipient. SEARXNG_URL accepts http/https URLs. Settings mutations redirect to /settings for HTML forms. Never render key values.
- Settings context additions: sender (dict with all SenderProfile fields), sender_fields, key_status (key name to bool), key_names. No key values. Existing settings context preserved.
- Existing /partners renders partners_list.html with partners, status_filter, tier_filter, search, counts, active_page='partners'. Replies renders replies_list.html with replies, label_filter, labels, active_page='replies'. Generic rows include legacy column aliases.

## Sending

Mode is reset to TEST on app startup and persisted as SEND_MODE in workspace .env. POST /toggle-send-mode requires confirm_text='GO LIVE' to enter LIVE. POST /partners/<id>/send in LIVE requires 'SEND TO <email>'. Send batches in either mode require approved, eligible drafts; rejected, pending and already Contacted rows are held with per-contact reasons. POST /push and /send-batch in LIVE require 'SEND BATCH OF n' where n is the number of selected approved drafts. POST /schedule-send requires 'SCHEDULE LIVE FOR n' only in LIVE. TEST scheduling does not require a LIVE phrase. send-test always targets TEST_RECIPIENT_EMAIL; unset is refused. The default daily send cap is 20, overridden by DAILY_SEND_CAP in the workspace .env. LIVE sends require verified email; POST /partners/<id>/verify-email-manually records a local attestation with status valid, method manual and an activity. It does not waive approval or typed confirmation. All forms use confirm_text. No redirects to untrusted Referer. Scheduler startup, polling and single-row execution each require OUTREACH_SCHEDULER=1. The executor checks before claiming a row or recording started_at. There is no manual run-now route; explicit Send test and Send in test mode buttons use the journaled send path independently of the scheduler switch.

- POST /crm/<id>/profile accepts name, org, role and email in addition to relationship fields. Email must be valid and unique. Name, org and role are capped at 200 characters, notes at 5000, and reject reasons at 500. Contact text strips control and bidi override characters, preserving newline and tab in notes.
- POST /api/searxng/test checks the entered url or saved SEARXNG_URL at /search?q=test&format=json, with a five second timeout and JSON parsing. It does not save the URL.
- /crm/, /drafts/ and /crm/drafts/ resolve. Out-of-range integer ids and missing partner/contact records return 404. Pagination beyond the last page redirects to the last page with a note.
- Flashes are scoped to the redirect destination, consumed on display, and consecutive identical messages collapse. Error pages discard stale flashes.

## Security

Every route except healthz requires the one-time startup token exchange and signed authenticated session. Tests may obtain app.config['ACCESS_TOKEN'] then GET /?token=...; testing does not bypass security. Session CSRF keys csrf and csrf_token are provided for compatibility. StrictUndefined is supported. Core errors are one line, sanitized, with no provider payloads, credentials or local paths.

## Integration lane API

Core config exports get_workspace(), configure_workspace(value), Workspace(root), WorkspacePath(*parts). Workspace has root, path(*parts) with symlink confinement, db_path, drafts_dir, env_path, env(), value(name, default=''), read_json(name, default=None). vendor.workspace_root and setting_value should delegate to get_workspace().root and get_workspace().value so concurrent Flask app instances stay isolated. Do not rely on process-global OUTREACH_HOME. The owner facts module was deleted; generic loader is workspace_facts.py (load_facts, approved_claims, forbidden_phrases). Update LLM call sites to this module, or the new facts.py if you provide it. Core owns workspace_facts.py only.

Final facts entrypoint: vcoutreach.web.facts.load_facts(workspace=None), approved_claims(), forbidden_phrases(). workspace_facts.py re-exports these. Both read sender.json and facts.json without a process-global cache. Sample sender uses an example identity and example.com. Sample loading generates four ok drafts and one blocked draft locally, with no provider call.

## Final schema and operation notes

The database lives at workspace/data/outreach.db, in DELETE journal mode with foreign keys enabled. schema.sql is a consolidated, empty baseline of the retained legacy review and integration tables. partners remains the canonical contact table so integrations keep their existing ids and queries. It now includes engine slug, role, facts, source_urls, lane and thread fields, tags and next_action. Engine drafts are canonical Markdown files; SQL drafts mirror review state and the full engine body for legacy pages. Editing either UI updates the canonical draft. batches/contact_batches record CSV cohorts. web_send_journal reserves attempts before transport; a partial unique live index and the shared .vcoutreach/journal.jsonl prevent duplicates. Uncertain attempts remain blocked. Existing private databases are not imported automatically. The old incremental migrations were consolidated into schema.sql; migrations/001_workspace.sql reserves the new migration sequence.

The primary Gmail connection is Settings > Gmail: Gmail address, 16-character app password (spaces stripped), Connect Gmail. 2-Step Verification must be on and the Workspace admin must allow app passwords. POST /api/gmail/connect with mode=app_password, address and password verifies IMAP SSL at imap.gmail.com:993 and SMTP SSL at smtp.gmail.com:465, both with a 15-second timeout, before atomically saving data/.gmail_app_password.json with mode 0600. No password is returned in routes, templates or errors. POST /api/gmail/disconnect deletes the secret and any saved OAuth client and token. GET /api/gmail/status reports state, mode and address only, plus OAuth running/failed flags. Local connection checks do not open sockets.

When the app-password file exists, web/gmail/api.py selects the stdlib IMAP/SMTP adapter. Its socket seam is web/gmail/app_password.py:connection_factory(protocol), where protocol is imap or smtp. The Gmail-shaped service subset supports profile, message and thread reads, searches, sends and drafts, including the send worker, CRM refresh and reply poller. Gmail X-GM-MSGID and X-GM-THRID decimal values become Gmail API hex IDs; X-GM-RAW performs Gmail searches. SMTP preserves the existing MIME builder output and reply headers, adding missing From, Date and Message-ID headers. After SMTP acceptance, an rfc822msgid: search in the Special-Use Sent folder returns Gmail message/thread IDs. If Sent indexing lags, the RFC Message-ID is the durable send receipt and resolves on a later poll. SMTP acceptance never triggers an automatic resend. Draft creation appends to the Special-Use Drafts folder. All network failures use fixed credential-free errors. SEND_MODE=test routing, live confirmations, caps and send journals remain enforced by existing callers.

Advanced: use a Google OAuth client instead retains gmail.modify under a closed Settings disclosure. Save your own Desktop app OAuth client JSON at data/.gmail_credentials.json, or import it there, then connect through Settings or run `vcoutreach auth gmail-web --workspace DIR` to save data/.gmail_token.json with mode 0600. Successful OAuth consent replaces app-password mode; otherwise the saved app password takes precedence. Without an app-password file, the OAuth path works as before. The standalone engine's `vcoutreach auth gmail` retains its own send-only token. All web paths resolve through the active workspace. Session and credential files are never packaged.

The scheduler uses a standard-library daemon thread and only starts with OUTREACH_SCHEDULER=1. Scheduled live rows retain their exact typed confirmation. Startup resets SEND_MODE to test, including after a restart; live execution still requires returning to live mode. The read-only send plan applies the engine gate and workspace suppression, verification, cap and journal checks.

## Core verification

Verified with the supplied Python environment, without binding sockets or sending external messages: compilation and imports; fresh empty database; empty and sample GET routes under Jinja StrictUndefined; sample offline drafting (four ok, one blocked); editor save and legacy editor synchronization; CSV whole-file validation and export; one-time authentication; loopback Host and port checks; signed session flags; CSRF and Origin rejection; 2 MB body limit; credential injection rejection and 0600 .env; two-app workspace isolation; fake-transport test rerouting, exact live confirmation, duplicate refusal, daily cap, blocked and hold refusal; typed deletion and draft archival; escaping draft-symlink refusal; default-off and opt-in scheduler lifecycle. GET pages were exercised with socket connections blocked.

Core-owned files have no matches against the private name, email or literal blocklists, and no literal em dash characters. The domain scan matches only the explicitly required placeholder Calendly URL. Firm-blocklist matches are common code words (character, benchmark, launch, plural), not firm identities.

Unverified: real Gmail OAuth and delivery; paid API results; PitchBook browser login and session refresh; Cloudflare deployment and real event sync; port binding/browser opening; macOS Keychain execution; visual browser QA; independently authored acceptance/mutation checks. No tests were created or changed. No git state-changing commands were used. Other lanes own their own scrub and acceptance checks.

## Callable compatibility

The obsolete prepared-update test-send module and POST route were removed, together with their update-manifest readers, CRM preview, prepared-updates filter and unused test-send table/index definitions. Existing databases are not migrated or dropped. Generic contact test sends still use the shared send journal. Follow-up and new-investor review readers remain separate.

All retained public callable names are preserved, including the legacy CSV seed entrypoints and generic campaign adapter. Changes: create_app now accepts workspace=None, testing=False, send_transport=None. Sender/account/source/MIME-domain defaults became generic. force parameters remain accepted but cannot bypass live duplicate refusal. The private scene generator was deleted after confirming there are no in-package callers. Personal facts load through facts.py and workspace_facts.py. The old seed-import function names accept CSV inputs and no longer parse a private document.

## Final core file inventory

- `vcoutreach/web/__init__.py`
- `vcoutreach/web/boilerplate_loader.py`
- `vcoutreach/web/config.py`
- `vcoutreach/web/crm_alternate_investors.py`
- `vcoutreach/web/crm_analytics.py`
- `vcoutreach/web/crm_live_sync.py`
- `vcoutreach/web/crm_store.py`
- `vcoutreach/web/db.py`
- `vcoutreach/web/dedup.py`
- `vcoutreach/web/drafts_service.py`
- `vcoutreach/web/email_assembler.py`
- `vcoutreach/web/facts.py`
- `vcoutreach/web/gmail/__init__.py`
- `vcoutreach/web/gmail/api.py`
- `vcoutreach/web/gmail/push_worker.py`
- `vcoutreach/web/gmail/scheduler.py`
- `vcoutreach/web/keys.py`
- `vcoutreach/web/outreach_mime.py`
- `vcoutreach/web/pipeline/__init__.py`
- `vcoutreach/web/pipeline/loader.py`
- `vcoutreach/web/pipeline/seed_importer.py`
- `vcoutreach/web/replies/__init__.py`
- `vcoutreach/web/replies/classifier.py`
- `vcoutreach/web/replies/loader.py`
- `vcoutreach/web/replies/poller.py`
- `vcoutreach/web/security.py`
- `vcoutreach/web/server/app.py`
- `vcoutreach/web/server/publik_campaign.py`
- `vcoutreach/web/server/sample_email.py`
- `vcoutreach/web/server/workspace_settings.py`
- `vcoutreach/web/sourcing/__init__.py`
- `vcoutreach/web/sourcing/contact_picker.py`
- `vcoutreach/web/sourcing/merge.py`
- `vcoutreach/web/sourcing/portfolio.py`
- `vcoutreach/web/sourcing/recent_funding.py`
- `vcoutreach/web/workspace_facts.py`
- `pyproject.toml`
- `vcoutreach/cli.py`
- `vcoutreach/web/schema.sql`
- `vcoutreach/web/migrations/001_workspace.sql`
- `vcoutreach/web/sample/contacts.sample.csv`
- `vcoutreach/web/sample/replies.json`
- `vcoutreach/web/sample/sender.json`

The legacy personal-facts module and private scene generator were removed. The old migrations were replaced by the baseline described above. Unchanged helpers are included in the module verification inventory.

## Final routes

| Methods | Route | Endpoint |
|---|---|---|
| GET | `/` | `dashboard` |
| POST | `/api/data-provider/key` | `store_dashboard_data_provider_key` |
| POST | `/api/data-provider/test` | `test_dashboard_data_provider` |
| POST | `/api/gemini/key` | `store_dashboard_gemini_key` |
| GET | `/api/gemini/status` | `dashboard_gemini_status` |
| POST | `/api/gemini/test` | `test_dashboard_gemini` |
| POST | `/api/model-provider/key` | `store_dashboard_model_provider_key` |
| POST | `/api/searxng/test` | `test_searxng_connection` |
| GET | `/candidates` | `candidates_list` |
| POST | `/candidates/import` | `candidates_import` |
| GET | `/crm` | `crm_dashboard` |
| GET | `/crm/<int:partner_id>` | `crm_contact` |
| POST | `/crm/<int:partner_id>/activity` | `crm_activity_add` |
| GET | `/crm/<int:partner_id>/alternate-investors` | `crm_alternate_investors` |
| POST | `/crm/<int:partner_id>/delete` | `crm_contact_delete` |
| POST | `/crm/<int:partner_id>/profile` | `crm_profile_update` |
| GET | `/crm/analytics` | `crm_analytics` |
| POST | `/crm/analytics/sync-tracking` | `crm_analytics_sync_tracking` |
| GET | `/crm/drafts` | `crm_drafts` |
| GET, POST | `/crm/drafts/<slug>` | `crm_draft_editor` |
| POST | `/crm/drafts/write` | `crm_drafts_write` |
| GET | `/crm/export.csv` | `crm_export` |
| POST | `/crm/import` | `crm_import` |
| GET, POST | `/crm/new` | `crm_new` |
| POST | `/crm/refresh-inbox` | `crm_refresh_inbox_route` |
| GET | `/crm/review-preview` | `crm_review_preview` |
| GET | `/crm/send-plan` | `crm_send_plan` |
| POST | `/crm/tasks/<int:task_id>/complete` | `crm_task_complete` |
| GET | `/crm/tracking` | `crm_tracking` |
| POST | `/demo/outreach/start` | `start_dashboard_outreach` |
| GET | `/demo/outreach/status` | `dashboard_outreach_status` |
| POST | `/demo/outreach/stop` | `stop_dashboard_outreach` |
| GET | `/demo/pitchbook-status` | `demo_pitchbook_status` |
| POST | `/demo/pitchbook-visible` | `demo_pitchbook_visible` |
| POST | `/demo/sample-email` | `demo_sample_email` |
| GET | `/email-verification` | `email_verification` |
| POST | `/email-verification/<int:partner_id>/run` | `run_contact_provider` |
| POST | `/email-verification/attempts/<int:attempt_id>/apply` | `apply_contact_provider_candidate` |
| POST | `/email-verification/attempts/<int:attempt_id>/dismiss` | `dismiss_contact_provider_candidate` |
| GET | `/enrichment` | `enrichment_status` |
| GET | `/healthz` | `healthz` |
| GET | `/knowledge` | `workspace_settings` |
| GET | `/partners` | `partners_list` |
| GET | `/partners/<int:partner_id>` | `partner_detail` |
| POST | `/partners/<int:partner_id>/approve` | `partner_approve` |
| POST | `/partners/<int:partner_id>/enrich` | `partner_enrich` |
| POST | `/partners/<int:partner_id>/generate-hook` | `partner_generate_hook` |
| POST | `/partners/<int:partner_id>/pitchbook-enrich` | `partner_pitchbook_enrich` |
| POST | `/partners/<int:partner_id>/reject` | `partner_reject` |
| POST | `/partners/<int:partner_id>/send` | `partner_send` |
| POST | `/partners/<int:partner_id>/send-test` | `partner_send_test` |
| POST | `/partners/<int:partner_id>/update` | `partner_update` |
| POST | `/poll-replies` | `poll_replies` |
| POST | `/push` | `push` |
| POST | `/relationships/<int:partner_id>/toggle-update` | `toggle_relationship_update` |
| GET | `/replies` | `replies_list` |
| GET | `/replies/<int:reply_id>` | `reply_detail` |
| POST | `/replies/<int:reply_id>/mark-reviewed` | `reply_mark_reviewed` |
| POST | `/replies/<int:reply_id>/update-label` | `reply_update_label` |
| POST | `/replies/add` | `reply_add` |
| POST | `/sample/load` | `sample_load` |
| POST | `/schedule-send` | `schedule_send` |
| GET | `/scheduled` | `scheduled_list` |
| POST | `/scheduled/<int:sched_id>/cancel` | `scheduled_cancel` |
| POST | `/send-batch` | `send_batch` |
| POST | `/settings` | `update_workspace_settings` |
| GET | `/settings` | `workspace_settings` |
| POST | `/settings/keys` | `save_keys` |
| POST | `/settings/reset` | `reset_workspace_settings` |
| POST | `/settings/sender` | `save_sender_profile` |
| GET | `/setup` | `workspace_settings` |
| GET | `/static/<path:filename>` | `static` |
| POST | `/toggle-send-mode` | `toggle_send_mode` |

## Changed versus copied source

Changed Python modules relative to the copied app:

- `vcoutreach/web/__init__.py`
- `vcoutreach/web/boilerplate_loader.py`
- `vcoutreach/web/config.py`
- `vcoutreach/web/crm_alternate_investors.py`
- `vcoutreach/web/crm_analytics.py`
- `vcoutreach/web/crm_live_sync.py`
- `vcoutreach/web/crm_store.py`
- `vcoutreach/web/db.py`
- `vcoutreach/web/dedup.py`
- `vcoutreach/web/email_assembler.py`
- `vcoutreach/web/gmail/api.py`
- `vcoutreach/web/gmail/push_worker.py`
- `vcoutreach/web/gmail/scheduler.py`
- `vcoutreach/web/outreach_mime.py`
- `vcoutreach/web/pipeline/loader.py`
- `vcoutreach/web/pipeline/seed_importer.py`
- `vcoutreach/web/replies/__init__.py`
- `vcoutreach/web/replies/classifier.py`
- `vcoutreach/web/replies/loader.py`
- `vcoutreach/web/replies/poller.py`
- `vcoutreach/web/server/app.py`
- `vcoutreach/web/server/publik_campaign.py`
- `vcoutreach/web/server/sample_email.py`
- `vcoutreach/web/server/workspace_settings.py`
- `vcoutreach/web/sourcing/merge.py`
- `vcoutreach/web/sourcing/portfolio.py`
- `vcoutreach/web/sourcing/recent_funding.py`

Created Python modules:

- `vcoutreach/web/drafts_service.py`
- `vcoutreach/web/facts.py`
- `vcoutreach/web/keys.py`
- `vcoutreach/web/security.py`
- `vcoutreach/web/workspace_facts.py`

Also changed: pyproject.toml, vcoutreach/cli.py, web/schema.sql and the migration baseline. Created: this contract, the three packaged sample inputs, and the baseline migration. Deleted: all old migration SQL files, the old personal-facts module (renamed to facts.py), and iris_personalized_plan.py. No in-package callers of the deleted scene generator exist.

## Set up with AI additions (MCP and Publik recommendation)

The `vcoutreach mcp` server (see `docs/mcp.md`) runs in its own process and never calls `create_app`. The routes below are added to the web app by `vcoutreach/web/server/mcp_routes.py` and follow every rule above: session and loopback guards, CSRF on each POST, no inline scripts, no external resources.

| Methods | Route | Purpose |
|---|---|---|
| GET | `/setup/ai` | Renders the "Set up with AI" page with the three connect snippets, resolved executable path and workspace, a Copy button for each, and the Publik card. Offline-safe: no network calls. |
| POST | `/api/mcp/self-test` | Runs the exact snippet command with an argument list (no shell), sends `initialize` and `tools/list`, and reports `{ok, message, tool_count}` only. Stores `mcp_last_self_test_ok` on success. 10 second timeout. Child stdout is never echoed. |
| POST | `/api/publik/test` | Runs the same minimal probe as the MCP `test_connection` for `publik_api` and returns `{ok, message}` only. The key value and response headers are never returned. |
| POST | `/settings/publik-recommendation` | Sets `state=skipped` or `state=pending` in `publik_recommendation`. Redirects back to `/settings`. |

Rule for the MCP surface (applies to every current and future MCP tool): no MCP tool accepts a secret or sends live. Tool input schemas use `additionalProperties: false`. No tool accepts a key, password, token, OAuth file or `SEND_MODE`, no tool writes `SEND_MODE`, and `send_test` addresses only the configured test recipient. Results and errors pass through the shared redaction layer. Adding a tool that breaks this rule requires a new spec and a contract update here first.
