"""Subject and body template rendering."""
from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from typing import Dict

from .types import Contact, Lane, OutreachError, SenderProfile
from .email_copy import clean_opener, safe_greeting_name, unique_paragraphs

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n\s*")
DEFAULT_SUBJECT = "a note for {first_name}"


@lru_cache(maxsize=None)
def _template_text(name: str) -> str:
    try:
        text = (resources.files("vcoutreach") / "data" / "templates" / name).read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError) as exc:
        raise OutreachError("template %s is missing from the package data" % name) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _fill(template: str, values: Dict[str, str]) -> str:
    """Single pass placeholder substitution: inserted text is never re-expanded."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _signature_block(sender: SenderProfile) -> str:
    signature = sender.signature.replace("\r\n", "\n").replace("\r", "\n").strip()
    if signature:
        return signature
    lines = [sender.name.strip()]
    role = ", ".join(part for part in (sender.title.strip(), sender.org.strip()) if part)
    if role:
        lines.append(role)
    return "\n".join(lines)


def render_subject(contact: Contact, sender: SenderProfile) -> str:
    """Follow ups reuse the original thread subject; first touch uses the template."""
    if contact.lane == Lane.FOLLOW_UP and contact.thread_subject.strip():
        return contact.thread_subject.strip()
    if contact.lane == Lane.FIRST_TOUCH:
        explicit = getattr(contact, "subject", "").strip()
        if explicit:
            return explicit
        if sender.first_touch_subject:
            subject = " ".join(sender.first_touch_subject.format(first_name=safe_greeting_name(contact.first_name)).split())
            if subject:
                return subject
    template = getattr(sender, "subject_template", "") or DEFAULT_SUBJECT
    values = {"first_name": safe_greeting_name(contact.first_name), "org": contact.org}
    subject = " ".join(_fill(template, values).split())
    if not subject:
        subject = " ".join(_fill(DEFAULT_SUBJECT, values).split())
    return subject


def render_body(contact: Contact, sender: SenderProfile, hook_text: str) -> str:
    """Assemble the email body: one line per paragraph, signature as the last block."""
    template_name = "follow_up.txt" if contact.lane == Lane.FOLLOW_UP else "first_touch.txt"
    template = _template_text(template_name).strip("\n")
    values = {
        "first_name": safe_greeting_name(contact.first_name),
        "hook": clean_opener(hook_text, contact.first_name),
        "product_line": sender.product_lines[0].strip() if sender.product_lines else "",
        "traction_line": sender.traction_lines[0].strip() if sender.traction_lines else "",
        "ask_line": sender.ask_line.strip(),
        "signature": _signature_block(sender),
    }
    paragraphs = []
    for paragraph in _PARAGRAPH_BREAK.split(template):
        filled = _fill(paragraph, values).strip()
        if filled:
            paragraphs.append(filled)
    return "\n\n".join(unique_paragraphs(paragraphs))
