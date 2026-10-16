# The browser app: workspace and settings

`vcoutreach ui` runs a local web app against one **workspace folder**. This page
describes what is in that folder, which settings the app reads, and how the pages
fit together. The quick tour, the TEST and LIVE modes and the safety list are in the
[README](../README.md#the-browser-app). Services you can connect are in
[integrations.md](integrations.md).

## Start it

```
vcoutreach ui [--workspace DIR] [--port N] [--no-browser]
```

On a Mac, the installer's `Outreach.command` runs this for you with the workspace
`~/Documents/vcoutreach-workspace`. The app needs the `web` extra
(`pip install -e ".[web]"`); without Flask it prints that one line and exits with
code 2. It prints `Open this link: http://127.0.0.1:<port>/?token=...`. The link
works once.

## What is in the workspace

| Path | What it holds |
| --- | --- |
| `.env` | Settings and keys, mode 0600. Created on first start with a generated `FLASK_SECRET_KEY` and `SEND_MODE=test`. Never commit it. |
| `sender.json` | Your sender profile (the Settings form edits it). |
| `facts.json` | Optional (the sample loader writes an empty one). Extra approved claims and forbidden phrases used when writing hooks, merged with `sender.json`. |
| `drafts/` | One Markdown draft per contact, in the engine's format. `drafts/blocked/` holds blocked drafts. `drafts/removed/` holds drafts archived when you delete a contact. |
| `contacts.csv` | Not required. Contacts live in the database; import a CSV from Overview and export one at any time. |
| `data/outreach.db` | The local SQLite database: contacts, review state, replies, enrichment results, the send journal and scheduled sends. |
| `data/.gmail_credentials.json`, `data/.gmail_token.json` | The browser app's Gmail OAuth client and token (see integrations.md). |
| `outputs/` | Reports and tracking journals the app writes. |
| `.vcoutreach/journal.jsonl` | The duplicate-send journal shared with the command line. Keep it: deleting it removes the protection. |

The app refuses to read or write outside this folder, and refuses symbolic links
that point out of it. Back the folder up like any other document folder.

## Settings it reads

Set these in Settings (the form writes `.env`) or edit `.env` by hand. In the
browser app a workspace `.env` value wins over your shell environment.

| Name | Default | Meaning |
| --- | --- | --- |
| `SEND_MODE` | `test` | Reset to `test` on every start. Switching to LIVE needs the typed phrase `GO LIVE`. |
| `TEST_RECIPIENT_EMAIL` | none | Your own test inbox. Every TEST send goes here. If it is empty, a send is refused. |
| `SENDER_EMAIL` | none | The From address. |
| `DAILY_SEND_CAP` | `20` | Send attempts per day (1 to 10000), shared by TEST and LIVE. |
| `OUTREACH_SCHEDULER` | off | Set to `1` to let scheduled sends run. Off means scheduled rows only wait. |
| `OUTREACH_HOME` | the `--workspace` folder | The workspace root when the app is created without `--workspace`. |
| `PITCHBOOK_ENABLED` | `0` | Turns on the PitchBook enrichment buttons. |
| `TRACKING_WORKER_URL`, `TRACKING_WORKER_TOKEN` | none | Your Cloudflare Worker (see integrations.md). |

Key names (API keys and the like) are in integrations.md. Only allowlisted names
can be saved from the form; values with spaces, quotes or shell characters are
refused so they cannot add extra lines to `.env`.

## How a message gets sent

1. Write or import contacts, then write drafts (Drafts, "Write drafts"). Each draft
   is linted and grounded by the engine, and blocked drafts never send.
2. Open Partners. Review, edit the hook, then approve or reject.
3. Read the Send plan page. It shows who would be sent and who is held, and why.
4. Connect Gmail with `vcoutreach auth gmail-web --workspace DIR` and set
   `TEST_RECIPIENT_EMAIL` in Settings, under Keys and connection settings. Missing
   authorization refuses the send before any journal row is reserved. In TEST
   mode, send a test to that inbox and read it.
5. Only then switch to LIVE (`GO LIVE`) and send one contact (`SEND TO <email>`) or a
   batch (`SEND BATCH OF <n>`). Each attempt is recorded in the journal before it
   leaves, and the daily cap applies.

An uncertain send remains blocked. Check Gmail Sent first. If the message did not
go out, open its contact page and click "I checked Sent, it did not go out, allow
retry". This records an activity and keeps the attempt in history as retryable.
A successful send receipt still blocks duplicates.

## Page notes

- **Overview** lists contacts by stage. "Add contact", CSV import (the whole file is
  checked before anything is written) and `export.csv` use the engine's contact
  rules, the same as the command line.
- **Drafts** shows issue chips from the linter (`em_dash`, `ungrounded_claim` and so
  on, the codes in the README). Saving an edit re-lints it. A blocked draft stays
  blocked until its issues are gone.
- **Replies** keeps replies from Gmail polling, plus any you add by hand. The label
  on polled replies is a suggestion from a simple classifier; change it if it is
  wrong. Replies added by hand start unlabeled for your review.
- **Analytics** counts only your own records. Open and click numbers come from your
  tracking Worker if you connected one, and are request classifications, not proof
  of reading.
- **Enrichment** and **Verification** show what each source returned and what is
  waiting for your decision. Nothing is applied without a click.

## The sample

"Load the sample" on an empty Overview loads a sender profile, five contacts, and one
reply, drafts locally with the offline template provider, and returns to Overview.
The five contacts are fictional addresses at `example.com`. It refuses if the
workspace already has contacts, a sender profile or drafts. Replace everything in it
with your own verified details before you send. Every name, number and claim in a
real email must be yours and checked by you.

## Troubleshooting

- **`The web app needs Flask`**: run `pip install -e ".[web]"` with the virtual
  environment active.
- **Link refused or an access page**: the token is one-time. Stop the app and start
  it again to get a fresh link.
- **A send is refused with a one-line message**: check `TEST_RECIPIENT_EMAIL`,
  Gmail credentials, the daily cap, and whether the draft is blocked or already in
  the journal. The Send plan page shows the reason for each held draft.
- **Port in use**: pass `--port N`, or let the app pick the next free port from 8765.
