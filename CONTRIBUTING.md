# Contributing

Thanks for helping. This project drafts emails and optionally sends them through
guarded paths, so a few boundaries are firm.

## What will not be accepted

- Real contact data of any kind: names, email addresses, lists, CSV exports,
  generated drafts, or investor or customer records.
- Credentials of any kind: API keys, OAuth tokens or client files, cookies,
  browser profiles, `.env` files.
- Scraping, contact enrichment, or lead-list features.
- Anything that sends mail outside the existing gate or weakens it: bulk
  auto-send, removing the plan-only default, the test-inbox default, the dedupe
  journal, the daily cap, or the four live conditions, or any path that reads a
  mailbox.
- Anything that logs, prints, or stores an API key or token.

## Guidelines

1. Use only fictional fixtures and reserved domains such as `example.com`,
   `example.org`, and `.example`.
2. The core stays standard library only. Optional integrations (Gmail) must
   import their libraries lazily and fail with an install hint.
3. Keep new behavior deterministic and testable offline. Tests must not touch
   the network, except tests marked `local_http`, which may talk to `127.0.0.1`
   only.
4. Do not use em dashes or en dashes anywhere (code, comments, docs, tests,
   commit messages). The draft linter enforces the same rule on emails.
5. Hooks must stay grounded in supplied facts. Do not weaken the lint or
   grounding checks to make a draft pass.
6. Every CLI flag needs a help string and an entry in the README. When you change
   a user-facing message, warning, or exit code, update the README in the same
   pull request.

## Set up and verify

You need Python 3.10 or newer (`python3 --version`). The Python that ships with
macOS is 3.9, which is too old.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -U pip
pip install -e ".[dev]"
SEND_MODE=test python -m pytest -q
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. In a new
terminal, re-activate the environment before running anything.

The optional live smoke test is marked `live` and is skipped unless
`PUBLIK_LIVE_SMOKE=1` and `PUBLIK_API_KEY` are both set. It makes a real, paid
request, so leave it off unless you mean it.

Run a secret scanner before committing. Review the complete Git history, not
only the working tree, before publishing a fork.

## Pull requests

Keep changes small and focused, explain the behavior you changed, and add or
update tests that describe what the code is supposed to do. By contributing, you
agree that your contribution is licensed under the MIT License in this
repository.
