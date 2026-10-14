# Security Policy

## Supported version

Security fixes are applied to the latest commit on the default branch.

## Reporting a vulnerability

Use GitHub private security advisories: open the repository's Security tab and
choose "Report a vulnerability". Do not open a public issue with exploit
details, keys, contact records, or personal information. If private reporting is
not available on the repository, open a minimal public issue that says you have
a security report and asks for a private channel, with no details in it.

Useful reports include:

- a path that sends mail without the plan, test-mode and live-mode safeguards
  (for example, live sending without all four live conditions)
- a way to route a test-mode message to a real recipient
- a way around the dedupe journal or the daily send cap
- an API key or OAuth token appearing in a log, error message, warning, `repr`,
  draft file, or index (this includes the value of a malformed `.env` line,
  which warnings must never show)
- a lint or grounding bypass that lets a fabricated claim through
- a contact id or other imported field that writes outside the output directory
- unsafe handling of untrusted CSV, JSON, or provider response data

## Operational guidance

- Never commit `.env`, OAuth tokens or client files, or the `.vcoutreach/`
  journal. If a key leaks, revoke it with its issuer first, then clean history.
- Keep `SEND_MODE=test` until you have reviewed drafts and completed a
  test-mode run.
- Treat contact files, sender files, and model output as untrusted input.
- Only email people you are allowed to contact, and follow the anti-spam and
  privacy law that applies to you and your recipients.
- Prefer offline mode (`--offline`) when you cannot send recipient details to a
  third-party provider. If `--provider auto` finds no key it falls back to the
  offline template provider and prints a warning to stderr; it never contacts a
  provider without a key.
