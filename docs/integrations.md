# Integrations

Everything on this page is optional. The drafting engine, the sample and the TEST
mode of the browser app work without any of it. Each integration uses your own
account and your own key, runs only when you click the matching button (or, for
sends, after the typed confirmations described in the [README](../README.md#test-mode-and-live-mode)),
and sends data to the service named in its section. You are responsible for the
terms of every service you connect and for the cost it incurs. Nothing here is legal
or financial advice, and prices change, so check each provider's current pricing
page before you run a batch.

## Where settings live

- Keys and connection settings go in **Settings**, on each service card (Connections) or under **More settings**.
  That form writes to the `.env` file in your workspace folder (mode 0600), only
  for the allowlisted names below, and never shows a saved value again.
- You can also edit `.env` in the workspace by hand (lines like `NAME=value`).
  In the browser app, a value in the workspace `.env` wins over the same name in
  your shell environment.
- Some optional install steps are needed. The Mac installer installs `.[web,gmail]`.
  Gemini, Claude and PitchBook need the extra `integrations`, which you add with:

  ```
  pip install -e ".[integrations]"
  ```

  Run it from the app folder with the app's virtual environment active (on a Mac
  install, `~/vcoutreach/.venv/bin/python -m pip install -e "$HOME/vcoutreach[integrations]"`).
  If the extra is missing, the app tells you in one line which one to install.

## Gmail

**Needs.** The Gmail extra (`pip install -e ".[gmail]"`, already part of the Mac
installer), and a Gmail account to connect. The recommended way is Google sign-in
(OAuth) with a Desktop app OAuth client from your own Google Cloud project with the
Gmail API enabled. The client file is yours; none ships here.

**What it does.** Sends your messages (test sends, live sends, scheduled sends),
reads replies when you click "Poll replies" or refresh the inbox, and checks the
Sent folder after a send. Gmail is the first setup step and it is required for a
test send.

**Connect with Google sign-in (recommended).** In Settings, click **Choose OAuth
client file** and pick the Desktop client JSON, then click **Connect Gmail**. Google
opens a sign-in in your browser and this app never sees your password. The client is
saved as `data/.gmail_credentials.json` and the token as `data/.gmail_token.json`
in your workspace, both with mode 0600. The scope is `gmail.modify`, so the app can
send, read replies and check Sent. Consent expires locally after three minutes if
left unfinished.

**Alternative: a Gmail app password.** Only if you cannot use an OAuth client file.
In Settings, open the collapsed "Alternative: use a Gmail app password instead"
section, enter your Gmail address and a 16 character app password, and click
**Connect with app password**. Google requires 2-Step Verification for this, and a
Workspace admin must allow app passwords. The password is stored in
`data/.gmail_app_password.json` in your workspace with mode 0600 and is never shown
or logged.

**The command line.** The command line uses a separate, send-only setup, because it
asks for a narrower permission:

| Used by | Client file | Token file | Scope |
| --- | --- | --- | --- |
| The command line: `vcoutreach auth gmail`, `vcoutreach send --execute` | `GMAIL_CLIENT_SECRETS_PATH` (default `~/.config/vcoutreach/client_secret.json`) | `GMAIL_TOKEN_PATH` (default `~/.config/vcoutreach/token.json`) | `gmail.send` only; it never reads mail |
| The browser app | `data/.gmail_credentials.json` in your workspace | `data/.gmail_token.json` in your workspace | `gmail.modify` |

The command line refuses a token that has any scope other than `gmail.send`, so the
two token files cannot be shared. `vcoutreach auth gmail-web --workspace DIR` runs
the browser app's authorization from a terminal, after the client JSON is saved in
that workspace's data folder. The Google sign-in flow has not been exercised against
live Google in this repository's automated tests, so try it with a throwaway test
mailbox first. Without a connection, sends and reply checks stop with a one-line
"Gmail credentials are missing" message and nothing is sent. Set
`TEST_RECIPIENT_EMAIL` in Settings, on the Test recipient card.

**Cost and terms.** The Gmail API itself is free within Google's quotas. Your
Google account's sending limits and Google's terms and policies apply, and
unsolicited bulk email can get an account suspended. Follow the anti-spam and
privacy law that applies to you and to your recipients.

## Enrow and Apollo (email verification and lookup)

**Needs.** Your own API key from each service: `ENROW_API_KEY` and/or
`APOLLO_API_KEY`. No extra install.

**What it does.** The Verification page, per contact and only when you click Run:

- **Enrow** checks your account and verifies a single work email through Enrow's
  API (`api.enrow.io`).
- **Apollo** matches one person by name plus company domain or LinkedIn URL through
  Apollo's people match API (`api.apollo.io`). The app asks Apollo not to reveal
  personal emails or phone numbers.
- Each result is held as a candidate. You apply or dismiss it; nothing overwrites a
  contact by itself. The key test button reports connected, key rejected, plan
  restricted, rate limited or unreachable.

**Cost and terms.** Both services bill or meter by credit, and what an API call
consumes depends on your plan; Apollo's API access depends on the plan too. Run a
few contacts first and watch your balance in the provider's dashboard. Your
subscription terms govern how you may use the data, including email-finding and
outreach restrictions. The app does not check them for you. On macOS the app can
also read a key stored in your Keychain under an `outreach-signal.*` item if none is
in `.env` or the environment.

## PitchBook

**Needs.** Your own PitchBook subscription, `pip install -e ".[integrations]"`,
Google Chrome in `/Applications` (the launcher is macOS only), and
`PITCHBOOK_ENABLED=1` in Settings.

**What it does.** In Settings, "Open PitchBook sign-in" starts a separate Chrome
window with its own profile under your workspace `data/` folder and a debugging port
on `127.0.0.1` only (9222 by default; `PITCHBOOK_CDP_PORT` changes it). You sign in
yourself. "Check session" confirms it. A helper then refreshes only your PitchBook
cookies from that window. The "PitchBook enrich" button on a partner's page (open one from Partners) then looks up
one contact and stores facts such as deals. The Candidates page lists co-investor
candidates found the same way, which you review and import. Lookups are throttled
(8 to 15 seconds between page loads) and capped per day by
`PITCHBOOK_MAX_LOOKUPS_PER_DAY` (default 2000). Your session stays on your machine.
It is never uploaded or shared, and no session file ships with the package.

**Cost and terms.** There is no extra charge beyond your subscription, but
PitchBook's terms of use decide whether automated lookups, the number of lookups,
or storing the data are allowed under your plan. Read your agreement before you
enable this. The app may be blocked, challenged or logged out by PitchBook; it stops
on a login or challenge page and does not try to get around one.

## Gemini and Claude (hook and subject writing)

**Needs.** `pip install -e ".[integrations]"` and a key: `GEMINI_API_KEY` (or
`GOOGLE_API_KEY`) for Google Gemini, and/or `ANTHROPIC_API_KEY` for Claude.

**What it does.** The "Generate hook" button on a partner's page (open one from Partners) sends the contact's
enrichment facts plus your sender profile and approved claims to the model and asks
for a short opener and subject. Enrich the contact first. If no draft exists,
Generate hook creates a base draft automatically. The saved hook contains only
the opener, with 1 to 2 sentences. Review its grounding badge before approval;
the last generation error stays visible on the partner page until generation succeeds.
With the default `auto` choice the app tries Gemini first (model
`GEMINI_MODEL`, default `gemini-2.5-flash-lite`), then Claude Haiku, then Claude
Sonnet; `ANTHROPIC_MODEL`, if set, replaces the two Claude model names.
`VERTEX_STRICT=1` limits it to Gemini on Vertex AI. Vertex needs
`GOOGLE_GENAI_USE_VERTEXAI=1`, `GOOGLE_CLOUD_PROJECT`, `GCP_LOCATION` and a service
account file (`GCP_SA_PATH`, default `data/.gcp_service_account.json` in the
workspace); these are advanced, set by editing `.env`. Gemini has a test
button that makes one small request. The "Write drafts" button on the Drafts page
uses the engine providers instead (`auto`, `template`, `publik`, `openai`,
`anthropic`, configured as in the README).

**Cost and terms.** Both are pay-per-use APIs (Gemini may have a free tier with its
own limits). A hook takes up to three attempts, and each attempt can try more than one model, so one click can mean several billed requests. The facts you
send leave your machine for that provider, so use only facts you are allowed to
share. The provider's terms apply.

## Cloudflare tracking Worker

**Needs.** Your own Cloudflare account, Node 24 or later with Wrangler, and the
`tracking_worker/` folder from this repository. Follow
[`tracking_worker/README.md`](../tracking_worker/README.md): create a D1 database,
set your own HTTPS origin and destinations, set four random secrets, run the
migrations and deploy. No account or database IDs ship in the package.

**What it does.** The Worker serves signed redirect, open-pixel, deck and
unsubscribe links, and answers authenticated summary requests. In the app, enter
the Worker's HTTPS origin as `TRACKING_WORKER_URL` (an origin only: no path, query
or credentials) and its `DASHBOARD_ACCESS_SECRET` as `TRACKING_WORKER_TOKEN` in
Settings. The Tracking page and the Analytics "sync tracking" button then read
aggregate counts from your Worker, only when you click. Connecting a Worker does not
by itself add tracking links to your emails, and the counts are request
classifications, not proof that a person read a message. Many mail clients preload
images and links, so treat opens as a weak signal.

**Cost and terms.** Cloudflare Workers and D1 can incur charges beyond their free
allowances. Open and click tracking has legal and consent implications in some
places (for example under GDPR and ePrivacy rules); include an unsubscribe path and
decide for yourself whether to use it.

## Other optional settings

- **Web search enrichment.** The "Enrich" button on a partner's page (open one from Partners) searches the web for facts
  about a contact. It uses a SearXNG server at `SEARXNG_URL` (default
  `http://localhost:8080`). Start one with
  `docker compose -f packaging/searxng/docker-compose.yml up -d` (binds to
  127.0.0.1 only; needs Docker). Some upstream engines rate-limit or show a
  CAPTCHA now and then, and SearXNG uses the others. The app can also fall back to the Brave
  Search API when `ENABLE_BRAVE_FALLBACK=1` and `BRAVE_API_KEY` are set. Brave
  charges per its plan.
- **Other allowlisted names.** The Settings form also accepts `OPENROUTER_API_KEY`,
  `EMAILABLE_API_KEY`, `PUBLIK_API_KEY`, `OPENAI_API_KEY`,
  `OPENAI_BASE_URL` and `OPENAI_MODEL`. Add one only if you use the feature that
  reads it.
