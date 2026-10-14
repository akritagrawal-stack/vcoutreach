"""MIME message construction for drafts.

Plain text only, no attachments. Standard library only.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid

from vcoutreach.types import Draft, Lane, OutreachError

__all__ = ["build_message", "unsafe_header_value"]


def unsafe_header_value(value: str) -> bool:
    """True when a value could break out of a header line (CR, LF or NUL)."""
    return any(ch in value for ch in ("\r", "\n", "\x00"))


def _checked(name: str, value: str) -> str:
    if unsafe_header_value(value):
        raise OutreachError(f"Refusing to build a message: {name} contains a line break.")
    return value.strip()


def _single_address(role: str, value: str) -> str:
    """The header value, only when it names exactly one mailbox (no group syntax)."""
    message = f"Refusing to build a message: {role} must be exactly one address."
    if ";" in value:
        raise OutreachError(message)
    addresses = [addr for _name, addr in getaddresses([value]) if addr]
    if len(addresses) != 1 or "@" not in addresses[0] or any(ch.isspace() for ch in addresses[0]):
        raise OutreachError(message)
    return value


def _lane_value(lane: object) -> str:
    return str(getattr(lane, "value", lane))


def build_message(
    draft: Draft,
    sender_name: str,
    sender_email: str,
    to_override: str | None = None,
    subject_override: str | None = None,
) -> EmailMessage:
    """Build a plain text EmailMessage for a draft.

    Sets From, To, Subject and a generated Message-ID. Follow up drafts also get
    In-Reply-To (the draft's in_reply_to) and References (the draft's references,
    or in_reply_to when references is empty).
    """
    from_name = _checked("sender name", sender_name or "")
    from_addr = _checked("sender email", sender_email or "")
    if not from_addr or "@" not in from_addr:
        raise OutreachError("Refusing to build a message: sender email is missing or malformed.")
    from_addr = _single_address("sender", from_addr)

    to_addr = _checked("recipient", draft.to if to_override is None else to_override)
    if not to_addr:
        raise OutreachError("Refusing to build a message: recipient is empty.")
    to_addr = _single_address("recipient", to_addr)
    subject = _checked("subject", draft.subject if subject_override is None else subject_override)

    message = EmailMessage()
    message["From"] = formataddr((from_name, from_addr))
    message["To"] = to_addr
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=from_addr.rsplit("@", 1)[1] or None)

    if _lane_value(draft.lane) == Lane.FOLLOW_UP.value:
        in_reply_to = _checked("in_reply_to", draft.in_reply_to or "")
        if not in_reply_to:
            raise OutreachError("Refusing to build a follow up without in_reply_to (threading is mandatory).")
        references = _checked("references", draft.references or "") or in_reply_to
        message["In-Reply-To"] = in_reply_to
        message["References"] = references

    message.set_content(draft.body)
    return message
