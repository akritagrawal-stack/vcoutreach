"""Send gate: plans sends and enforces test, live and dedupe rules.

Safety model (every rule fails closed):

* Plan only is the default. Nothing is sent, and the journal is untouched, unless
  ``execute`` is true and a transport is supplied.
* Test mode (SEND_MODE=test) reroutes every message to TEST_RECIPIENT_EMAIL and
  prefixes the subject with ``[TEST to <original recipient>] ``. A plan (execute false)
  does not need TEST_RECIPIENT_EMAIL; executing does.
* Live mode needs ``settings.send_mode == "live"`` AND ``live`` AND ``execute`` AND
  ``confirm_live == "CONFIRM_LIVE_SEND"``. Any one missing raises SendGateError.
  Asking for live while SEND_MODE=test is also an error, never a silent downgrade.
* Each successful send is journaled immediately, before the next draft is looked at.
  If the transport raises, the run stops and the error propagates (already journaled
  sends stay journaled, so a rerun is safe).
"""
from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterator, Protocol, Sequence

from vcoutreach.config import Settings
from vcoutreach.journal import Journal
from vcoutreach.lint import lint_draft
from vcoutreach.send.mime import build_message, unsafe_header_value
from vcoutreach.types import Draft, Lane, OutreachError, SenderProfile

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "CONFIRM_LIVE_TOKEN",
    "MailTransport",
    "PlannedSend",
    "SendGateError",
    "SendReceipt",
    "SendReport",
    "SkippedSend",
    "plan_sends",
]

CONFIRM_LIVE_TOKEN = "CONFIRM_LIVE_SEND"
# Shown as PlannedSend.to in a plan when TEST_RECIPIENT_EMAIL is unset. Executing never uses it.
_NO_TEST_RECIPIENT = "<TEST_RECIPIENT_EMAIL not set>"

# One mailbox only, matched with fullmatch on the raw value: no whitespace, control
# characters, angle brackets, commas, semicolons or quotes, so a draft "to" can never
# expand into several recipients.
_ADDRESS_RE = re.compile(r'[^@\s\x00-\x1f\x7f<>,;"]+@[^@\s\x00-\x1f\x7f<>,;"]+\.[^@\s\x00-\x1f\x7f<>,;"]+')


class SendGateError(OutreachError):
    """A send safeguard refused the request. Nothing was sent."""


@dataclass(frozen=True)
class SendReceipt:
    message_id: str
    thread_id: str = ""


class MailTransport(Protocol):
    def send(self, message: EmailMessage) -> SendReceipt: ...


@dataclass(frozen=True)
class PlannedSend:
    draft: Draft
    to: str          # final recipient after test rerouting
    subject: str     # final subject after test prefixing
    mode: str        # "test" or "live"


@dataclass(frozen=True)
class SkippedSend:
    draft: Draft
    reason: str
    # reasons: blocked, already_sent, missing_thread, invalid_header, lint, daily_cap, limit
    detail: str = ""  # short human explanation; for "lint" the error issue codes joined by ", "


@dataclass(frozen=True)
class SendReport:
    planned: tuple[PlannedSend, ...]
    sent: tuple[PlannedSend, ...]
    skipped: tuple[SkippedSend, ...]


def _lane_value(lane: object) -> str:
    return str(getattr(lane, "value", lane))


def _is_address(value: str) -> bool:
    return bool(_ADDRESS_RE.fullmatch(value))


@contextlib.contextmanager
def _exclusive_send_lock(journal: Journal) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on <journal dir>/send.lock for a whole run.

    Uses fcntl.flock where available, otherwise an O_EXCL lock file. A second run that
    finds the lock held raises SendGateError instead of racing the first.
    """
    lock_path = journal.path.parent / "send.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    busy = SendGateError("another send run is in progress. Nothing was sent.")
    if fcntl is not None:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise busy from None
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock
        return
    try:  # pragma: no cover - Windows and other platforms without fcntl
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise busy from None
    try:  # pragma: no cover
        yield
    finally:  # pragma: no cover
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(str(lock_path))


def _live_gate_open(settings: Settings, live: bool, execute: bool, confirm_live: str) -> bool:
    return bool(
        settings.send_mode == "live"
        and live
        and execute
        and confirm_live == CONFIRM_LIVE_TOKEN
    )


def _resolve_mode(settings: Settings, live: bool, execute: bool, confirm_live: str) -> str:
    send_mode = settings.send_mode
    if send_mode not in ("test", "live"):
        raise SendGateError(f"SEND_MODE must be 'test' or 'live', got {send_mode!r}. Nothing was sent.")
    if send_mode == "test":
        if live:
            raise SendGateError(
                "Live sending was requested but SEND_MODE=test. Nothing was sent. "
                "Set SEND_MODE=live deliberately if you really mean it."
            )
        return "test"
    if not live:
        raise SendGateError(
            "SEND_MODE=live is set but --live was not passed. Nothing was sent. "
            "To preview without live risk use SEND_MODE=test (the default); "
            "to send for real you also need --live, --execute and "
            f"--confirm-live {CONFIRM_LIVE_TOKEN}."
        )
    missing = []
    if not execute:
        missing.append("--execute")
    if confirm_live != CONFIRM_LIVE_TOKEN:
        missing.append(f"--confirm-live {CONFIRM_LIVE_TOKEN}")
    if missing:
        raise SendGateError(
            "SEND_MODE=live needs every live safeguard. Missing: "
            + ", ".join(missing)
            + ". Nothing was sent. Set SEND_MODE=test to send test messages only."
        )
    return "live"


def _test_recipient(settings: Settings, required: bool) -> str:
    address = settings.test_recipient_email or ""
    if not address.strip():
        if not required:
            return _NO_TEST_RECIPIENT
        raise SendGateError(
            "TEST_RECIPIENT_EMAIL is required in test mode so no real recipient is ever mailed. "
            "Set it in .env. Nothing was sent."
        )
    # Strict on the raw value: no commas, spaces or line breaks anywhere.
    if not _is_address(address):
        raise SendGateError(
            "TEST_RECIPIENT_EMAIL must be a single valid email address with no commas, spaces or "
            "line breaks. Nothing was sent."
        )
    return address


def _header_problem(draft: Draft) -> str:
    """Why the draft's headers are unsafe, or "" when they are fine."""
    values = (draft.to or "", draft.subject or "", draft.in_reply_to or "", draft.references or "")
    if any(unsafe_header_value(v) for v in values):
        return "a header field contains a line break"
    if not _is_address((draft.to or "").strip()):
        return "recipient is not a single valid email address"
    return ""


def _lint_problem(draft: Draft, sender: SenderProfile) -> str:
    """Why the draft fails the send-time lint gate, or "" when it passes."""
    if not (draft.subject or "").strip():
        return "subject is empty"
    if not (draft.body or "").strip():
        return "body is empty"
    issues = lint_draft(draft.subject, draft.body, sender, None)
    codes = list(dict.fromkeys(issue.code for issue in issues if issue.severity == "error"))
    return ", ".join(codes)


def plan_sends(
    drafts: Sequence[Draft],
    settings: Settings,
    sender: SenderProfile,
    journal: Journal,
    *,
    live: bool = False,
    execute: bool = False,
    confirm_live: str = "",
    limit: int | None = None,
    transport: MailTransport | None = None,
) -> SendReport:
    """Plan (and, only when ``execute`` is true, perform) sends for ok drafts.

    See the module docstring for the safeguards. Drafts are handled in input order.
    """
    mode = _resolve_mode(settings, live, execute, confirm_live)
    if limit is not None and limit < 0:
        raise SendGateError("limit must not be negative.")
    test_to = _test_recipient(settings, required=execute) if mode == "test" else ""
    if mode == "test" and test_to != _NO_TEST_RECIPIENT:
        # A real recipient equal to the test address would receive the real message.
        for draft in drafts:
            if draft.status == "ok" and (draft.to or "").strip().lower() == test_to.lower():
                raise SendGateError(
                    f"Draft {draft.contact_id!r} is addressed to TEST_RECIPIENT_EMAIL, so test mode "
                    "would deliver the real message to its real recipient. Nothing was sent."
                )
    sender_email = ""
    if execute:
        if transport is None:
            raise SendGateError("execute was requested but no mail transport was supplied. Nothing was sent.")
        sender_email = (settings.sender_email or "").strip()
        if not sender_email and mode == "test":
            sender_email = test_to  # From falls back to the test recipient in test mode
        if not _is_address(sender_email):
            raise SendGateError("SENDER_EMAIL is required to send and must be a single valid address.")
        with _exclusive_send_lock(journal):
            return _run(
                drafts, settings, sender, journal, mode, test_to, sender_email,
                live=live, execute=execute, confirm_live=confirm_live, limit=limit, transport=transport,
            )
    return _run(
        drafts, settings, sender, journal, mode, test_to, sender_email,
        live=live, execute=execute, confirm_live=confirm_live, limit=limit, transport=transport,
    )


def _run(
    drafts: Sequence[Draft],
    settings: Settings,
    sender: SenderProfile,
    journal: Journal,
    mode: str,
    test_to: str,
    sender_email: str,
    *,
    live: bool,
    execute: bool,
    confirm_live: str,
    limit: int | None,
    transport: MailTransport | None,
) -> SendReport:
    live_room = 0
    if mode == "live":
        live_room = max(0, int(settings.daily_send_cap) - journal.count_today("live"))

    planned: list[PlannedSend] = []
    sent: list[PlannedSend] = []
    skipped: list[SkippedSend] = []
    seen: set[str] = set()

    for draft in drafts:
        lane_value = _lane_value(draft.lane)
        recipient = (draft.to or "").strip()
        key = Journal.key(recipient, lane_value)

        header_problem = _header_problem(draft) if draft.status == "ok" else ""
        lint_problem = ""
        if draft.status == "ok" and not header_problem:
            lint_problem = _lint_problem(draft, sender)

        if draft.status != "ok":
            codes = ", ".join(dict.fromkeys(i.code for i in (draft.issues or ())))
            detail = "draft status is blocked" + (f" ({codes})" if codes else "")
            skipped.append(SkippedSend(draft, "blocked", detail))
        elif key in seen:
            skipped.append(SkippedSend(
                draft, "already_sent", "same recipient and lane appears earlier in this run"))
        elif journal.has(key, mode):
            skipped.append(SkippedSend(draft, "already_sent", f"already in the {mode} send journal"))
        elif lane_value == Lane.FOLLOW_UP.value and not (draft.in_reply_to or "").strip():
            skipped.append(SkippedSend(
                draft, "missing_thread", "follow_up draft has no in_reply_to, so it cannot be threaded"))
        elif header_problem:
            skipped.append(SkippedSend(draft, "invalid_header", header_problem))
        elif lint_problem:
            skipped.append(SkippedSend(draft, "lint", lint_problem))
        elif mode == "live" and len(planned) >= live_room:
            skipped.append(SkippedSend(
                draft, "daily_cap", f"daily cap of {int(settings.daily_send_cap)} live sends reached"))
        elif limit is not None and len(planned) >= limit:
            skipped.append(SkippedSend(draft, "limit", f"limit of {limit} sends reached"))
        else:
            if mode == "test":
                plan = PlannedSend(draft, test_to, f"[TEST to {recipient}] {draft.subject}", "test")
            else:
                plan = PlannedSend(draft, recipient, draft.subject, "live")
            planned.append(plan)
            seen.add(key)
            if execute:
                if transport is None:  # unreachable: checked above, kept to fail closed
                    raise SendGateError("No mail transport supplied. Nothing was sent.")
                if plan.to == _NO_TEST_RECIPIENT:  # unreachable: execute requires the address
                    raise SendGateError("TEST_RECIPIENT_EMAIL is required to send. Nothing was sent.")
                # Defense in depth: recheck the live gate immediately before every live send.
                if mode == "live" and not _live_gate_open(settings, live, execute, confirm_live):
                    raise SendGateError("Live send gate closed. Nothing was sent.")
                message = build_message(
                    draft,
                    sender.name,
                    sender_email,
                    to_override=plan.to,
                    subject_override=plan.subject,
                )
                receipt = transport.send(message)
                journal.record(
                    key,
                    mode,
                    contact_id=str(draft.contact_id),
                    lane=lane_value,
                    recipient=recipient,
                    routed_to=plan.to,
                    message_id=str(receipt.message_id),
                    thread_id=str(receipt.thread_id),
                    rfc_message_id=str(message["Message-ID"]),
                )
                sent.append(plan)

    return SendReport(planned=tuple(planned), sent=tuple(sent), skipped=tuple(skipped))
