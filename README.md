# vcoutreach

vcoutreach turns a CSV of contacts and a sender profile into personalized,
reviewable email drafts. For each contact it asks a personalization provider for
one or two sentences that use only the facts you supplied, drops them into a
plain-text template, and runs a linter and a grounding check. Any draft that
fails a check is set aside in `drafts/blocked` with the reasons in its `issues:`
header line; the rest are written to `drafts/` as Markdown files for you to read.
It is a drafting tool: nothing is sent unless you opt in to the optional Gmail
step. The core needs Python 3.10 or newer and has no runtime dependencies. The
Gmail extra, which is optional, adds the Google client libraries. Providers are
pluggable: the Publik API, your own OpenAI-compatible or Anthropic key, or a free
offline template provider that never touches the network.

Built by the team behind Publik. The Publik API (https://publikhq.com) is the default personalization provider; you can swap in your own OpenAI-compatible or Anthropic key, or use the offline template with no account at all.

Version 0.1.0. MIT licensed.

## Scope

What it does:

- Reads a contacts CSV and a sender profile (JSON).
- Writes one draft per contact to `drafts/`, and `drafts/index.jsonl` with one JSON object per draft.
- Blocks drafts that fail the checks and puts them in `drafts/blocked/` with the reasons.
- Optionally sends drafts through your own Gmail account, with a plan-only default and a test-inbox default.

What it does not do, on purpose:

- No contact enrichment, scraping, or lead lists. You bring the contacts and the facts.
- No CRM, no reply reading, no reply classification.
- No bulk auto-send. Sending is optional, narrow, and off the quickstart path.
- No verification that your facts are true. The tool checks that a hook only uses
  facts you gave it. Verify each fact yourself and keep a source URL for it.
- No real contact data ships with this repository. The samples are fictional.

## Quickstart

You need Python 3.10 or newer. Check first:

```
python3 --version
```

If that prints 3.9 or older, stop and install a newer Python before continuing.
macOS ships Python 3.9, which is too old, so on a Mac install a newer one (for
example from python.org or with Homebrew). If your new interpreter has a
versioned name such as `python3.12`, use that name in place of `python3` below.

Replace `<repo-url>` with this repository's clone URL. The folder name
`vcoutreach` is given explicitly, so the `cd` works whatever the repository is
called.

```
git clone <repo-url> vcoutreach && cd vcoutreach
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -U pip
pip install -e .
vcoutreach init
vcoutreach draft --contacts contacts.sample.csv --sender sender.sample.json --offline
cat drafts/ines-marlowe.md
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`
instead of `. .venv/bin/activate`.

The virtual environment only lasts for that terminal. In a new terminal, go back
to the folder and re-activate it before using `vcoutreach`:

```
cd vcoutreach
. .venv/bin/activate
```

(PowerShell: `.venv\Scripts\Activate.ps1`.)

`vcoutreach init` copies `contacts.sample.csv`, `sender.sample.json` and
`.env.example` into the current directory. It never overwrites a file that
already exists: for each file it reports `created` or `skipped`. The repository
already contains a `.env.example`, so in a fresh clone you will see `skipped` for
that one. That is expected and harmless; the two sample files are still created.
`--offline` forces the template provider, makes no network calls, and costs
nothing.

The draft command first prints a line like
`wrote drafts/ (<n> drafts, <m> blocked, index.jsonl)` to stderr. `<n>` is the
number of ok drafts written to the folder, `<m>` is the number of blocked drafts
written to `drafts/blocked/`, and `index.jsonl` is the index file. For the sample
that is `wrote drafts/ (4 drafts, 1 blocked, index.jsonl)`.

Then it prints the summary line, followed by one line for each blocked draft with
its reason codes:

```
drafted=4 blocked=1 provider=template
blocked <contact_id>: <code>, <code>
```

In the sample, the one blocked draft is `rhea-castellane`, blocked with the code
`insufficient_facts` because that row has no facts on purpose.

The last quickstart command shows one finished draft. The same drafts are also in
`drafts/index.jsonl`, the machine-readable output: one JSON object per draft.

The sample contacts file has five fictional people at `example.com` and
`example.org` addresses:

| Row | Id | Lane | Facts | Result |
| --- | --- | --- | --- | --- |
| 1 | `ines-marlowe` | first_touch | two | `drafts/ines-marlowe.md` |
| 2 | `tobias-wren` | first_touch | one | `drafts/tobias-wren.md` |
| 3 | `priya-ostrander` | first_touch | one | `drafts/priya-ostrander.md` |
| 4 | `odalys-brandt` | follow_up, threaded to `<sample-thread-001@example.com>` | one | `drafts/odalys-brandt.md` |
| 5 | `rhea-castellane` | first_touch | none, on purpose | blocked: `drafts/blocked/rhea-castellane.md` |

### Using your own contacts

Do not type real people into `contacts.sample.csv`. It is the shipped example and
git does not ignore it, so real names and addresses put there could be committed.
Make a copy named `contacts.csv` first. Files named `contacts*.csv` are
git-ignored.

```
cp contacts.sample.csv contacts.csv
```

Then edit `contacts.csv` and point `--contacts` at it. You can copy
`sender.sample.json` to `sender.json` the same way and put your own details in it.

### What a draft file looks like

Each draft is a Markdown file: a header block of `key: value` lines, one blank
line, then the email body exactly as it would be sent. The header keys, in
order, are `to`, `subject`, `status`, `lane`, `provider`, `in_reply_to`,
`references` and `issues`. An empty value is written as the key with nothing
after it.

`issues` is the reason line. It lists the codes of every issue found, errors and
warnings, each code once in the order it was found, joined by `; `, and is empty
when there are none. A blocked draft in
`drafts/blocked/` always has at least one error code there. To find out why a
draft was blocked, use any of these:

- the `blocked <contact_id>: <code>, <code>` lines the draft command prints, each
  followed on stderr by `note: <id> blocked: <first issue message>` when the
  issue has a message;
- the `issues:` line at the top of the file in `drafts/blocked/`;
- the `issues` list in `drafts/index.jsonl`, which also has each issue's
  message and severity.

If any ok draft has warnings, the command prints
`note: <n> draft(s) have warnings (<codes>). See the issues: line at the top of each draft, or drafts/index.jsonl.`
to stderr. Read the `issues:` line in the draft itself. `vcoutreach lint` cannot
reproduce every warning, because it only re-checks what can be seen from the
saved subject and body (it cannot repeat checks that need the contact, such as
`odd_greeting`). The codes are explained in [How a draft is checked](#how-a-draft-is-checked).

Re-running overwrites the files for the same contact id. Copy a draft elsewhere
before you hand-edit it if you want to keep the edit, because the next run
replaces it.

Stale drafts: if you remove a contact from the CSV and run `draft` again with the
same `--out` folder, that contact's old draft is deleted on the next successful
run, and `removed stale draft <id> (no longer in the contacts file)` is printed
to stderr. Only files for ids listed in that folder's previous `index.jsonl` are
ever deleted, so any other file you keep there is left alone. If that index is
damaged (for example cut off mid-line), nothing is deleted. A run that stops on
a provider error deletes nothing (see the known limitation under Troubleshooting).

Offline hooks are intentionally plain. The offline template provider uses only the
contact's first fact and writes the sentence below (the fact's trailing period is dropped).

`I came across <fact>, and I found it interesting.`

A leading `Your` in the fact is lower-cased to `your` so the sentence reads
properly. Use offline mode to try the pipeline and check your CSV. For outreach you plan to
send, use a real provider so the hook reads like a person wrote it.

## Input files

### Contacts CSV

Header, in any order (extra columns are ignored):

```
id,name,email,org,role,facts,source_urls,lane,thread_subject,in_reply_to,references
```

You may also add an optional `first_name` column.

Every field is trimmed of leading and trailing spaces when loaded, and emails are
lower-cased.

| Column | Notes |
| --- | --- |
| `id` | Used as the draft file name. If blank, a slug of the email is used. Must be unique, ignoring case. |
| `name`, `email` | Required. The email must be a bare address like `local@domain.tld`. A value such as `Some Name <addr@host.tld>` is rejected with a hint to use the bare address, without a display name. |
| `first_name` | Optional. The name used in the greeting. If filled, it is used exactly (after trimming spaces). Use it for any unusual name. |
| `org`, `role` | Optional context. |
| `facts` | Verified statements about the recipient, separated by exactly ` | ` (space, pipe, space). This is the only material a hook may use. See below. |
| `source_urls` | Where each fact came from, same separator. |
| `lane` | `first_touch` (default) or `follow_up`. |
| `thread_subject` | Follow-up only, required: the original subject. A follow-up reuses it as its subject. |
| `in_reply_to` | Follow-up only, required: the Message-ID of the original email, written as `<...@...>` (angle brackets and an at sign). |
| `references` | Follow-up only: the References header, if you have one. |

Facts:

- Separate facts with exactly ` | `. A bare `|` with no spaces does not split.
  A line break inside a quoted fact becomes a space.
- Write each fact as a noun phrase that completes the words "I came across":
  `your 2024 essay on X`, `the interview where you described Y`. That is how the
  offline template reads it, and short specific phrases also give real providers
  the best material.
- A fact written as a sentence ("You wrote a great essay on X") is not a noun
  phrase. A fact that begins with you, I, we, they, he, she or it counts as a
  sentence, and so does one that begins with a contraction such as you're,
  you've, I'm, we're, they've, he's, she's or it's. The offline template blocks
  that contact with this message:

  `the offline template needs facts written as noun phrases (for example "your 2024 essay on X"), not sentences`

- Only state things you have checked, and keep the source in `source_urls`.

Greeting name: when `first_name` is blank, it is worked out from `name`. A
"Last, First" form uses the part after the comma, except that a suffix after the
comma (PhD, MD, MBA, Jr, Sr, II, III, IV, CPA, CFA, Esq, JD, DDS) is not a first
name, so `Alice Brown, PhD` greets `Alice`. Leading titles (dr, mr, mrs, ms, mx,
prof, professor, sir, madam) are dropped, the first remaining word is used, and a
word that is entirely upper case or entirely lower case is capitalized. If the
result is empty, shorter than 2 letters, contains anything other than letters,
apostrophes and hyphens, or is a word such as the, team, staff, info, sales,
hello, hi, dear, or a suffix like phd or jr, the draft gets an `odd_greeting`
warning. For example, a name such as `The Smith Team` drafts with an
`odd_greeting` warning, which shows up in the draft-time note and on the `issues:`
line. For unusual names, and to fix that warning, fill `first_name`.

Follow-ups: a `follow_up` row must have a non-empty `thread_subject` and an
`in_reply_to` in the form `<...@...>`. A follow-up row without a valid
`in_reply_to` is rejected when you run `draft` (exit code 2), so an unthreaded
follow-up is never drafted. The first sentence of the hook paragraph is added for
you: `Following up in case you missed my previous email.` Do not write it into
your facts.

Ids: the `id` becomes the draft's file name, so it must be unique, ignoring case
(`Ines` and `ines` collide, because each draft is a file). Use letters,
digits, dot, dash and underscore, starting with a letter or digit. If you leave
`id` blank, one is made from the email address (lower-cased, with each run of
other characters turned into a dash, so `ines.marlowe@example.com` becomes
`ines-marlowe-example-com`). A duplicate id, or an id with any other character,
is reported with its row number. If you rename an id only by changing its case
between runs (`ines-marlowe` to `Ines-Marlowe`), the next run replaces the old
file, so exactly one file remains, named with the new spelling. A case-only
collision inside one CSV is reported as a duplicate
and ends with `(ids that differ only by case count as duplicates, because each
draft is a file)`.

Validation: the whole file is checked in one pass and every problem is reported
together (up to 25), one problem per line, each naming its row. The header is
row 1. For example, a row with the wrong number of fields is reported as
`row N has X fields but the header has Y (is there an unquoted comma?)`. Other
problems named this way: a malformed email, a duplicate email (case
insensitive), a duplicate or invalid id, and a follow-up missing its thread
fields. All of these exit with code 2.

Two different messages cover files with nothing to read:

- A zero-byte file gives `<file> is empty: expected a header row`.
- A file with a header but no data rows gives `no contacts found in <file>`.

Two other common mistakes get their own message: a file that starts with `{` or
`[` is reported as looking like JSON, and a file whose header is a single column
containing `;` or a tab is reported as probably separated by semicolons or tabs
(save it as a comma-separated UTF-8 CSV).

Keep your real list out of git. This repository's `.gitignore` ignores `.env`,
`drafts/`, `.vcoutreach/` (the send journal and its lock file), token and client
secret files, and files named `contacts*.csv` only. It does not know any other
name. Name your real list `contacts.csv`, or add your own ignore line for
whatever you call it.

### Sender profile JSON

`name` is required. Everything else is optional.

| Key | Meaning |
| --- | --- |
| `name`, `title`, `org`, `website` | Who you are. |
| `signature` | The full signature block, multiple lines allowed. |
| `product_lines` | Approved one-sentence descriptions of what you do. The first entry is used. |
| `traction_lines` | Approved traction sentences. The first entry is used. |
| `ask_line` | The closing ask. May contain a calendar link. |
| `approved_claims` | Object of claims whose numbers a hook may repeat. |
| `forbidden_phrases` | Phrases that block a draft if they appear. |
| `subject_template` | Optional. Subject for first touches, with `{first_name}` and `{org}`. The default is `a note for {first_name}`. |

The product description appears once, in its own paragraph. The hook paragraph
never describes your product.

A sender file is rejected (exit code 2) if any text value in it, including items
in the lists and values inside nested objects, contains an em dash or en dash.
The message names the field by its path, such as `approved_claims.metric`. Use a
comma, colon, parentheses or "to" instead.

## Personalize with the Publik API

The Publik API is the built-in default provider. You do not need it: offline mode
and bring-your-own-key work without it.

1. Open https://publikhq.com/dashboard/api and follow the page to get an API key.
   The Publik documentation does not describe the signup steps or whether a
   payment card is required, so this README does not either. Follow what the
   page asks of you.
2. Copy `.env.example` to `.env` and set your key. `.env` is git-ignored. Never
   commit it.

   ```
   cp .env.example .env
   ```

   ```
   PUBLIK_API_KEY=your-key-here
   ```

3. Run the same command without `--offline`:

   ```
   vcoutreach draft --contacts contacts.sample.csv --sender sender.sample.json
   ```

   With a key present, the default `--provider auto` picks Publik, and the
   summary line ends with `provider=publik`. This writes to `drafts/` again, so
   it replaces the offline drafts from the quickstart (same ids).

Cost: Publik API calls cost real money per the pricing information on
https://publikhq.com. Offline mode is free. Each contact that has facts triggers
one request, plus automatic retries on transient failures. A contact with no
facts is blocked locally and does not trigger a request. Start with a handful of
rows, and with the sample file expect at most four requests.

Choose a model with `PUBLIK_MODEL`: `publik-fast`, `publik-balanced` (default),
`publik-smart`, or `publik-default`. Check the Publik pricing information before
choosing.

What leaves your machine: for each contact, the recipient's name, org, role and
facts, and your name and org. Use `--offline` if that is not acceptable. Requests
go only to the host you configured: redirects are not followed, so your key is
never sent to a second host.

## Bring your own key

Use your own model account instead of Publik.

OpenAI-compatible (also works with any server that offers an OpenAI-style
`/chat/completions` endpoint):

```
OPENAI_API_KEY=your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=the-model-you-want
```

Anthropic:

```
ANTHROPIC_API_KEY=your-key-here
ANTHROPIC_MODEL=the-model-you-want
```

`OPENAI_MODEL` and `ANTHROPIC_MODEL` have no default, so each is required
whenever its key is set. If a key is set without its model, the run stops with
exit code 3 and the message `OPENAI_MODEL is not set. Set it to the model name you
want to use (see your provider's model list).` (or the `ANTHROPIC_MODEL`
equivalent). `--provider auto` does not quietly fall back to the offline template
in that case: it shows this error.

Then pick one explicitly, or let `auto` choose:

```
vcoutreach draft --contacts contacts.csv --sender sender.json --provider openai
vcoutreach draft --contacts contacts.csv --sender sender.json --provider anthropic
```

`--provider auto` (the default) uses Publik if `PUBLIK_API_KEY` is set, else
OpenAI-compatible if `OPENAI_API_KEY` is set, else Anthropic if
`ANTHROPIC_API_KEY` is set, else the offline template provider. Your provider
bills you directly. Run `vcoutreach providers` to see which are configured (it
never prints keys). A provider whose key is set but whose model is missing shows
`configured=no` together with the name of the missing variable.

When `auto` finds no key and falls back to the offline template provider, it
says so on stderr and still exits 0:

```
warning: no provider key found (set PUBLIK_API_KEY, OPENAI_API_KEY or ANTHROPIC_API_KEY); using the offline template provider, which writes placeholder hooks.
```

`--offline` and an explicit `--provider template` choose the template provider on
purpose, so they print no warning.

## How a draft is checked

Every draft passes through the same checks, whichever provider wrote the hook.
A draft with any error-level issue is `blocked` and written to
`drafts/blocked/`. Warnings are reported but do not block. The checks never
rewrite your text.

| Code | Level | Triggers when |
| --- | --- | --- |
| `em_dash` | error | An em dash or en dash character appears in the subject or body. |
| `hard_wrap` | error | A paragraph contains a line break. The final paragraph is exempt only if it looks like a signature: it matches your `signature`, or it has at most 4 lines, every line is at most 60 characters, and no line ends with `.`, `?` or `!`. Otherwise it is checked like any other paragraph. |
| `filler` | error | The draft contains stock filler: "i hope this finds you well", "just circling back", "bumping this", "excited to share", "quick question", "touch base", "leverage", "delve" (case-insensitive). |
| `exclamation` | error | The body contains `!`. |
| `forbidden_phrase` | error | The draft contains any of your `forbidden_phrases`. |
| `false_parallel` | error | The hook claims the recipient's work is, or matches, what you do (see below). |
| `unapproved_number` | error | The hook contains a number that is not in the recipient's facts or your `approved_claims`. |
| `empty_hook` | error | The hook is blank. |
| `word_count` | warning | The body before the signature is outside 50 to 130 words. |
| `odd_greeting` | warning | The greeting name is empty, shorter than 2 letters, has characters other than letters, apostrophes and hyphens, or is a word such as team, staff, info, sales, hello, dear or a suffix like phd. Fill the `first_name` column. |
| `ungrounded_claim` | error | The hook names a proper noun or number that is not in the contact's name, org, role or facts, or in your name, org or title. |
| `no_facts` | error | The contact has no facts and a real provider was selected. No request is made. |
| `insufficient_facts` | error | No usable fact. Either none were given, or the provider could not use them: it replied `NO_HOOK` (the facts do not support a sincere sentence), or the offline provider found its first fact is a sentence instead of a noun phrase. The `note: <id> blocked:` line says which. |

Grounding: every capitalized word that is not at the start of a sentence, and
every token containing a digit, must occur in the contact's name, org, role or
facts, or in your own name, org or title. A small allowlist covers days, months
and "I". Numbers are compared with thousands separators removed, so `1,200,000`
matches `1200000`. This is what stops a model from inventing a company or a number.

The prompt sent to the model requires: use only the facts provided; one to two
sentences, at most 45 words, first person; the first sentence states one
specific fact, the second is an honest personal reaction; no em dashes, no
exclamation marks, no numbers that are not in the facts; and if the facts do not
support a sincere sentence, answer exactly `NO_HOOK`.

### Why a hook may never claim the recipient's work is what you do

Automated personalization has a typical failure: it flatters by saying the
recipient's work is the same as yours. It is usually overstated or untrue, it
turns the opening line into a pitch, and a recipient who really works on the
topic notices at once. So the prompt forbids it, the linter blocks the common
phrasings (`false_parallel`), and your product is described once, in its own
paragraph, never in the hook.

A generic illustration (invented, not tool output):

```
Fact:      your guide to scheduling field crews has a section on weather delays
Blocked:   Your guide to scheduling field crews is exactly what we are building.
Accepted:  I read your guide to scheduling field crews, and the section on weather delays was the part I found most useful.
```

The blocked line asserts a similarity nobody checked. The accepted line states a
fact the sender actually supplied and reacts to it honestly.

## Limitations

- **The checks are heuristics.** Grounding and the false-parallel check reduce
  invented or false claims. They do not eliminate them: a hook can pass every
  check and still be wrong, awkward, or misleading. A human must read every draft
  before it is sent.
- **Your facts are taken on trust.** The tool cannot tell whether a fact you
  supplied is true or current.
- **Retries can repeat a metered request.** After a timeout or dropped
  connection the tool cannot know whether the provider already processed (and
  charged) the request, so an automatic retry can bill you for the same contact
  more than once. Lower `PUBLIK_MAX_RETRIES` if that matters to you.
- **`--limit N` counts planned sends after skips.** Drafts that are skipped first
  (for example `already_sent` or `blocked`) do not use up the limit, so it caps
  messages planned, not drafts read.
- **The dedupe journal is local.** It protects only the machine and working
  directory that hold it. It does not protect against a second machine, a deleted
  journal, or the same person at a different address.
- **No consent tooling.** There is no suppression list, unsubscribe handling, or
  deliverability checking. Offline hooks are placeholders, not finished copy.

## Optional: sending with Gmail

Sending is off the quickstart path and guarded. You can ignore this section and
copy drafts into your mail client yourself.

**Only email people you are allowed to contact, and follow the anti-spam and
privacy law that applies to you and to them** (for example CAN-SPAM, GDPR and
ePrivacy rules, CASL). You are responsible for consent, honest sender
identification, and an opt-out path. This tool does not manage consent,
suppression lists or unsubscribes. This is not legal advice.

Safeguards:

- Plan only by default. `vcoutreach send --drafts drafts/` prints what would be
  sent where and sends nothing. Plan mode needs no settings beyond the sender
  file (`--sender`, which is itself optional): no keys, no Gmail setup, and no
  `TEST_RECIPIENT_EMAIL`.
- Only drafts with status `ok` are eligible. Blocked drafts are never sent, and
  `drafts/blocked/` is not read. If it holds files, the command says
  `blocked drafts not considered: <n> (see <drafts>/blocked)`. If the folder has
  no top-level `.md` drafts at all, `send` exits 2 with `no drafts found in <dir>`
  (and, when `<dir>/blocked` holds files, adds how many blocked drafts are there).
- Every draft is linted again at send time, and an error blocks the send.
  Skipped drafts are listed as `skipped <contact_id>: <reason> (<detail>)`. For
  the reason `lint`, the detail lists the issue codes; an empty subject or body is
  skipped as `lint` too. A line break, CR, LF or NUL in the subject, `in_reply_to`
  or `references`, or a recipient that is not one plain address, is skipped as
  `invalid_header`.
- Test inbox mode is the default (`SEND_MODE=test`). With `--execute`, every
  message goes to `TEST_RECIPIENT_EMAIL` instead of the real recipient, and the
  subject is prefixed `[TEST to <original recipient>] `.
- `TEST_RECIPIENT_EMAIL` is needed only with `--execute`. A plan-only run works
  without it and shows `<TEST_RECIPIENT_EMAIL not set>` where the test address
  would go. If it is set, it must be one valid address even in plan mode, so a
  typo is caught before any send.
- Follow-ups carry In-Reply-To and References headers. `draft` already rejects
  a follow-up row without a valid `in_reply_to`, so the `missing_thread` skip at
  send time only protects drafts you edited by hand.
- If a draft was written by the offline template provider, `send` prints
  `note: <n> draft(s) were written by the offline template provider (placeholder hooks); read them before sending.`
  to stderr. This applies to plans and to `--execute`, and it never blocks the
  run.
- A dedupe journal at `<WORKDIR>/.vcoutreach/journal.jsonl` records each
  successful send (key: hash of the lowercase recipient plus the lane). A
  recipient and lane already in the journal is skipped as `already_sent`, and so
  is a second draft in the same folder for a recipient and lane that already has
  one, so nobody is planned twice in a run. Test
  sends are journaled separately, so a test never blocks a later live send, and a
  live entry blocks sending live again. The journal and its lock file live in
  `.vcoutreach/`, which is git-ignored. Keep the journal: deleting it removes the
  protection.
- Live sends honor `DAILY_SEND_CAP` (default 20 per UTC day). Drafts past the cap
  are reported as `daily_cap`. `--limit N` caps one run.
- The Gmail integration requests the single `gmail.send` scope and never reads
  your mailbox. The token file is created with owner-only permissions and is
  never logged.

Set up, with the virtual environment active. Install the optional Google
libraries first. Run either command from the repository folder, because both use
files that live there:

```
pip install -r requirements-gmail.txt
```

```
pip install -e ".[gmail]"
```

Then authorize:

```
vcoutreach auth gmail
```

`vcoutreach auth gmail` runs a local OAuth flow. It needs an OAuth client file
(desktop app type) from your own Google Cloud project, saved at
`GMAIL_CLIENT_SECRETS_PATH`. Set `SENDER_EMAIL` to the account you authorize. If
the Google libraries are missing, `auth gmail` and `send --execute` stop with
exit code 3 and the message `Gmail sending needs optional Google libraries that are not installed.`
followed by the two install commands above, to be run from the repository folder.

Order of checks: `send --execute` checks the safeguards first (the settings,
`TEST_RECIPIENT_EMAIL`, the live conditions, and `SEND_MODE` against `--live`).
Only when they all pass does it check for the optional Google libraries. So with
the libraries missing you can still see a safeguard message first, which exits
with code 2, and a missing-libraries message exits with code 3. Nothing is sent
in either case.

Then, in order:

```
vcoutreach send --drafts drafts/ --sender sender.sample.json            # plan only, sends nothing
vcoutreach send --drafts drafts/ --sender sender.sample.json --execute  # test mode: goes to TEST_RECIPIENT_EMAIL
```

A live send needs all four of these at once, and any missing one stops the run
with nothing sent:

1. `SEND_MODE=live` in your settings
2. `--live`
3. `--execute`
4. `--confirm-live CONFIRM_LIVE_SEND` (that exact value)

```
SEND_MODE=live vcoutreach send --drafts drafts/ --sender sender.json --live --execute --confirm-live CONFIRM_LIVE_SEND --limit 5
```

Passing `--live` while `SEND_MODE=test` is an error, never a silent downgrade.
The reverse is refused too: with `SEND_MODE=live` set but `--live` not passed,
the run stops with nothing sent, and the message tells you to use
`SEND_MODE=test` (the default) to preview, or to pass `--live`, `--execute` and
`--confirm-live CONFIRM_LIVE_SEND` to send for real. Review the drafts and
complete a test-mode run first.

Flags for `send`:

| Flag | Meaning |
| --- | --- |
| `--drafts DIR` | Required. The folder of drafts written by `vcoutreach draft`. Only the `.md` files directly in it are read. |
| `--sender FILE` | Optional. Your sender profile JSON, used at send time to re-check your forbidden phrases. Without it the command prints `note: no --sender given; sender forbidden phrases are not checked at send time` to stderr and carries on, so pass it whenever you have the file. |
| `--execute` | Actually send. Without it, `send` only prints a plan. |
| `--live` | Ask for a live send. Needs the other three live conditions above. |
| `--confirm-live VALUE` | Must be exactly `CONFIRM_LIVE_SEND` for a live send. |
| `--limit N` | Stop after N planned sends. `--limit 0` plans nothing, and a negative number is an error (`must be zero or greater`, exit 2). See Limitations for how it counts. |

## CLI reference

You can also run `python -m vcoutreach`. Every flag has a help string:
`vcoutreach <command> --help`.

| Command | What it does |
| --- | --- |
| `vcoutreach init [--dir DIR]` | Copies the sample contacts, sample sender profile and `.env.example` into DIR (default `.`). Never overwrites an existing file; reports `created` or `skipped` for each. If DIR exists but is not a folder, or cannot be created, it exits 2 with `cannot use --dir <path>: <reason>` (for a file, `cannot use --dir <path>: it is a file, not a folder`). |
| `vcoutreach draft --contacts FILE --sender FILE [--provider NAME] [--out DIR] [--strict] [--offline]` | Writes drafts. |
| `vcoutreach lint --drafts DIR [--sender FILE]` | Re-lints the `.md` drafts directly inside DIR. DIR is a directory, not a single file. It does not look inside `DIR/blocked`. |
| `vcoutreach providers` | Lists providers and which are configured. Never prints keys. A provider with a key but no model shows `configured=no` and the missing variable name. |
| `vcoutreach send --drafts DIR [--sender FILE] [--execute] [--live --confirm-live VALUE] [--limit N]` | Plans sends by default. See the Gmail section. |
| `vcoutreach auth gmail` | Runs the local Gmail OAuth flow. |
| `vcoutreach --version` | Prints the version. |

Flags for `draft`:

| Flag | Meaning |
| --- | --- |
| `--contacts FILE` | Required. Your contacts CSV. |
| `--sender FILE` | Required. Your sender profile JSON. |
| `--provider NAME` | `auto` (default), `publik`, `openai`, `anthropic` or `template`. An unknown name exits 2. |
| `--out DIR` | Where drafts are written. Default `drafts`. If it cannot be created or written you get `cannot write to --out <path>: <reason>` and exit 2. |
| `--strict` | Exit 2 if any draft is blocked. Every draft is still written first, so the files are there to read. |
| `--offline` | Use the template provider with zero network calls. |

Flags for `lint`:

| Flag | Meaning |
| --- | --- |
| `--drafts DIR` | Required. A directory (not a single file), for example `drafts`. Only the `.md` drafts directly inside it are checked. Drafts in `DIR/blocked` are not. |
| `--sender FILE` | Optional. Your sender profile JSON, so your forbidden phrases are checked too. |

`lint` prints one line per issue, then `linted=<n> errors=<count> warnings=<count>`.
A folder with no top-level `.md` drafts exits 2 with `no drafts found in <dir>`;
if `<dir>/blocked` holds files, the message adds `; <n> blocked draft(s) are in <dir>/blocked`.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | OK. This includes `draft` finishing with some blocked drafts (unless `--strict`), and the fallback warning described above. |
| 1 | `lint` found at least one error. |
| 2 | A usage or input problem: an unknown provider name, a problem in the contacts or sender file (including `<file> is empty: expected a header row` and `no contacts found in <file>`), `no drafts found in <dir>` (from `lint` or `send`), `cannot write to --out <path>: <reason>`, `cannot use --dir <path>: <reason>`, `--drafts expects a directory, not a file: <path>` (for `lint` and `send`), a file in the drafts folder that is not a vcoutreach draft, a send safeguard refusing the run, or `--strict` with blocked drafts. |
| 3 | A provider error (missing or rejected key, a key without its model, empty balance, rate limit, network failure), printed as one clear line with no traceback. Also a mail transport error during `send --execute`, and missing Google libraries for `send --execute` or `auth gmail`, which `send --execute` reports only after its safeguards pass. |

An error with several problems (for example a contacts file with several bad
rows) prints the first line after `vcoutreach: ` and every further problem on its
own line below it.

Set `VCOUTREACH_DEBUG=1` in your shell to see full tracebacks.

## Configuration

Settings come from environment variables, after loading a `.env` file in the
working directory. A real environment variable wins over `.env`. The file format
is `KEY=VALUE`, `#` comments, optional quotes, and an optional leading `export `.
A `#` starts a comment at the start of a line, or after a space following an
unquoted value; a `#` with no space before it (such as in a URL fragment) is part
of the value. Putting comments on their own lines is the clearest. A malformed line is skipped with a warning like
`.env line 3 ignored: not KEY=VALUE`; the warning never shows the value. The
example file is [`.env.example`](.env.example).

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUBLIK_API_KEY` | empty | Publik API key. Never printed, logged, or written to drafts. |
| `PUBLIK_API_BASE_URL` | `https://publikhq.com/api/v1` | Publik API base URL. |
| `PUBLIK_MODEL` | `publik-balanced` | One of `publik-fast`, `publik-balanced`, `publik-smart`, `publik-default`. |
| `PUBLIK_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `PUBLIK_MAX_RETRIES` | `3` | Extra attempts after a timeout, connection error, 429 or 5xx. |
| `OPENAI_API_KEY` | empty | Key for an OpenAI-compatible provider. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible API. |
| `OPENAI_MODEL` | none (no default) | Model name to request. Required whenever `OPENAI_API_KEY` is set. |
| `ANTHROPIC_API_KEY` | empty | Anthropic key. |
| `ANTHROPIC_MODEL` | none (no default) | Model name to request. Required whenever `ANTHROPIC_API_KEY` is set. |
| `SEND_MODE` | `test` | `test` or `live`. Any other value is an error. |
| `TEST_RECIPIENT_EMAIL` | empty | Inbox that receives every message in test mode. Needed only to `--execute`; plan-only runs work without it, but if set it must be one valid address even in plan mode. |
| `SENDER_EMAIL` | empty | The From address for sends. In test mode, if it is unset, `--execute` uses `TEST_RECIPIENT_EMAIL` as the From address. |
| `DAILY_SEND_CAP` | `20` | Maximum live sends per UTC day. |
| `WORKDIR` | `.` | Base directory for local working files. The send journal and its lock file are in `.vcoutreach/` under it, which is git-ignored. |
| `GMAIL_TOKEN_PATH` | `~/.config/vcoutreach/token.json` | Where the Gmail OAuth token is stored. |
| `GMAIL_CLIENT_SECRETS_PATH` | `~/.config/vcoutreach/client_secret.json` | Your Google OAuth client file. |
| `VCOUTREACH_DEBUG` | unset | Set to `1` in the shell to show tracebacks. |

Keys are masked as `***` whenever settings are printed.

## Troubleshooting

**`vcoutreach: command not found`.** The virtual environment is not active in
this terminal. Go to the folder and run `. .venv/bin/activate` (PowerShell:
`.venv\Scripts\Activate.ps1`). `python3 -m vcoutreach` works too once it is
active.

**`pip install -e .` refuses because Python is too old.** You need Python 3.10 or
newer. Run `python3 --version`. On macOS the system Python is 3.9, so install a
newer one, then create the virtual environment again with that interpreter.

**`warning: no provider key found ...; using the offline template provider, which
writes placeholder hooks.`** This goes to stderr when `--provider auto` finds no
`PUBLIK_API_KEY`, `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. The run still
finishes with exit code 0 and the summary ends with `provider=template`, but the
hooks are placeholders, not finished copy. To fix it, set a key in `.env` in the
directory you ran the command from (or export it in your shell) and check
`vcoutreach providers`. If you want the offline provider, pass `--offline`
instead and the warning goes away.

**`OPENAI_MODEL is not set. Set it to the model name you want to use ...` (exit
code 3).** You set `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) without the matching
model. There is no default model. Set `OPENAI_MODEL` (or `ANTHROPIC_MODEL`) to a
model name from your provider's model list. `--provider auto` shows this error
instead of quietly using the offline template, and `vcoutreach providers` lists
that provider as `configured=no` with the missing variable.

**"PUBLIK_API_KEY" missing (exit code 3).** You chose `--provider publik` but no
key is set. The message names `PUBLIK_API_KEY`, links
https://publikhq.com/dashboard/api, and suggests `--provider template`. Put the
key in `.env` in the directory you run the command from, export it in your
shell, or run with `--offline`.

**402, empty balance (exit code 3).** Publik reports no credit left. The message
from Publik is printed, and if it includes a link to add credit or claim an
account, use it. The run stops at the first provider error. After fixing it,
re-run: files for the same contact ids are overwritten.

**401 or 403, key rejected (exit code 3).** The key is wrong, revoked, or not
allowed. Get a key from https://publikhq.com/dashboard/api and update `.env`.

**429, rate limited (exit code 3).** The provider is still limiting you after the
automatic retries. Wait and re-run, or raise `PUBLIK_MAX_RETRIES`.

**Timeouts or 5xx (exit code 3).** The tool retries `PUBLIK_MAX_RETRIES` times
and then stops. Check your connection, raise `PUBLIK_TIMEOUT_SECONDS`, or use
`--offline`.

**Blocked drafts.** The draft command prints `blocked <contact_id>: <code>, <code>`
for each one. The same codes are on the `issues:` line at the top of the file in
`drafts/blocked/`, and the `issues` list in `drafts/index.jsonl` adds a message
for each. Fix the cause and re-run:

- `no_facts` or `insufficient_facts`: add a verified fact to the contact's row.
- `ungrounded_claim` or `unapproved_number`: the hook named something not in the
  facts. Add the fact if it is true, otherwise leave it out.
- `false_parallel`: the hook claimed the recipient's work matches yours. Re-run
  or tighten the facts.
- `forbidden_phrase`, `filler`, `exclamation`, `em_dash`: edit your sender
  profile or facts, since these checks cover your own lines too.
- `odd_greeting` (a warning, not a block): fill the `first_name` column.

Use `--strict` in scripts if any blocked draft should fail the run.

**Contacts file errors (exit code 2).** Every problem is listed at once, one line
each, with the row number (the header is row 1). Fix them all and re-run. A
zero-byte file says it is empty and expects a header row, while a header with no
rows says `no contacts found in <file>`.

**A draft I expected is missing from `drafts/`.** If you removed that contact from
the CSV, the next successful `draft` run deletes its old draft and prints
`removed stale draft <id> ...` to stderr. A blocked draft is in `drafts/blocked/`,
not `drafts/`.

Known limitation: stale-draft cleanup runs only after a `draft` run completes. If
a run stops partway (for example on a provider error), the drafts finished so far
are kept (the command prints `kept <n> finished draft(s) in <out>/` before the
error) and the folder's `index.jsonl` lists only those, so a stale draft for a
contact you removed earlier can stay on disk. Finish one complete run. Before any
send, read the plan table (`vcoutreach send --drafts drafts/` prints every
recipient), or delete the drafts folder and run `draft` again.

**`draft <name> is not a vcoutreach draft (missing header line(s): ...)` (exit
code 2).** A `.md` file in the drafts folder does not have the draft header, so
`send` and `lint` stop rather than guess. Move the file out of the drafts folder
or fix its header. Keep notes and other Markdown files outside it.

**`--drafts expects a directory, not a file: <path>` (exit code 2).** `lint` and
`send` take the folder that holds the drafts, for example `drafts`, not one `.md`
file.

**A contact is blocked with `the offline template needs facts written as noun
phrases ...`.** The first fact is a sentence. Rewrite it as a noun phrase, for
example `your 2024 essay on X`.

**`cannot write to --out <path>: <reason>` (exit code 2).** The output folder
cannot be created or written. Pick another folder with `--out`.

## Contributing, security, license

- Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md). Real contact data,
  credentials, scraping, and anything that bypasses the send safeguards will not
  be accepted.
- Security reports: see [SECURITY.md](SECURITY.md).
- Third-party notices: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- License: [MIT](LICENSE).
