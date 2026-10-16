"""Contacts CSV loader."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .types import Contact, ContactsError, Lane


@dataclass(frozen=True)
class ContactWithSubject(Contact):
    """Keep an explicit CSV subject without changing the base Contact contract."""

    subject: str = ""


MAX_PROBLEMS = 25

_EMAIL_RE = re.compile(
    r"^[^@\s<>(),;:\\\"]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z0-9-]{2,}$"
)
# "Some Name <addr@host.tld>": a display name followed by an angle-bracketed address.
_DISPLAY_NAME_RE = re.compile(r"^[^<>]*\S[^<>]*<\s*[^<>\s@]+@[^<>\s]+\s*>$")
_DISPLAY_NAME_HINT = " (use the bare address, without a display name)"
_MESSAGE_ID_RE = re.compile(r"^<[^<>@\s]+@[^<>@\s]+>$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUIRED_COLUMNS = ("name", "email")
_FACT_SEPARATOR = " | "
_CASE_NOTE = (
    " (ids that differ only by case count as duplicates, because each draft is a file)"
)


def _clean(value: str) -> str:
    """Collapse all runs of whitespace (including newlines) to single spaces."""
    return " ".join(value.split())


def _split_facts(value: str) -> Tuple[str, ...]:
    """Split on the exact separator ' | ' only. Line breaks inside a fact become spaces."""
    flat = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    items = (_clean(part) for part in flat.split(_FACT_SEPARATOR))
    return tuple(item for item in items if item)


def _split_urls(value: str) -> Tuple[str, ...]:
    items = (_clean(part) for part in value.split("|"))
    return tuple(item for item in items if item)


def _slug(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-")


def _header_name(raw: str) -> str:
    return re.sub(r"[\s-]+", "_", raw.strip().lower())


def load_contacts(path: Path) -> List[Contact]:
    """Parse a contacts CSV into Contact objects, validating every row.

    All row problems are collected in one pass (at most MAX_PROBLEMS) and raised as a
    single ContactsError with one line per problem. Row numbers count the header as
    row 1. Fully blank rows are skipped but still counted so the numbers match what a
    spreadsheet shows.
    """
    path = Path(path)
    name = path.name
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            text = handle.read()
        if text.lstrip()[:1] in ("{", "["):
            raise ContactsError("%s looks like JSON, expected a CSV file with a header row" % name)
        records = list(csv.reader(io.StringIO(text, newline="")))
    except FileNotFoundError:
        raise ContactsError("contacts file not found: %s" % path)
    except IsADirectoryError:
        raise ContactsError("contacts path is a directory, expected a CSV file: %s" % path)
    except UnicodeDecodeError:
        raise ContactsError("%s is not valid UTF-8 text" % name)
    except csv.Error as exc:
        raise ContactsError("%s is not valid CSV: %s" % (name, exc))
    except OSError as exc:
        raise ContactsError("cannot read %s: %s" % (path, exc.strerror or exc.__class__.__name__))

    header_index = None
    header: List[str] = []
    for index, cells in enumerate(records):
        if any(cell.strip() for cell in cells):
            header_index = index
            header = [_header_name(cell) for cell in cells]
            if len(cells) == 1 and (";" in cells[0] or "\t" in cells[0]):
                raise ContactsError(
                    "%s has a single column; is it separated by semicolons or tabs? "
                    "Save it as a comma-separated UTF-8 CSV" % name
                )
            break
    if header_index is None:
        raise ContactsError("%s is empty: expected a header row" % name)

    missing = [col for col in _REQUIRED_COLUMNS if col not in header]
    if missing:
        raise ContactsError(
            "%s is missing required column(s): %s (found: %s)"
            % (name, ", ".join(missing), ", ".join(h for h in header if h) or "none")
        )

    contacts: List[Contact] = []
    problems: List[str] = []
    seen_email: Dict[str, int] = {}
    seen_id: Dict[str, Tuple[int, str]] = {}

    for index in range(header_index + 1, len(records)):
        cells = records[index]
        if not any(cell.strip() for cell in cells):
            continue
        row = index + 1
        if len(cells) != len(header):
            problems.append(
                "%s: row %d has %d fields but the header has %d (is there an unquoted comma?)"
                % (name, row, len(cells), len(header))
            )
        else:
            values: Dict[str, str] = {}
            for col, cell in zip(header, cells):
                if col:
                    values[col] = cell
            contact, row_problems = _build_contact(name, row, values)
            problems.extend(row_problems)
            if contact is not None:
                email_key = contact.email
                if email_key in seen_email:
                    problems.append(
                        "%s: duplicate email %r in rows %d and %d"
                        % (name, contact.email, seen_email[email_key], row)
                    )
                    contact = None
                elif contact.id.casefold() in seen_id:
                    earlier_row, earlier_id = seen_id[contact.id.casefold()]
                    message = "%s: duplicate id %r in rows %d and %d" % (
                        name,
                        contact.id,
                        earlier_row,
                        row,
                    )
                    if earlier_id != contact.id:
                        message += _CASE_NOTE
                    problems.append(message)
                    contact = None
            if contact is not None:
                seen_email[contact.email] = row
                seen_id[contact.id.casefold()] = (row, contact.id)
                contacts.append(contact)
        if len(problems) > MAX_PROBLEMS:
            break

    if problems:
        shown = problems[:MAX_PROBLEMS]
        if len(problems) > MAX_PROBLEMS:
            shown[-1] += " [showing the first %d problems; fix them and run again]" % MAX_PROBLEMS
        raise ContactsError("\n".join(shown))
    return contacts


def _build_contact(
    file_name: str, row: int, values: Dict[str, str]
) -> Tuple[Optional[Contact], List[str]]:
    def get(key: str) -> str:
        return values.get(key, "").strip()

    problems: List[str] = []

    def fail(message: str) -> None:
        problems.append("%s: row %d: %s" % (file_name, row, message))

    person = _clean(get("name"))
    if not person:
        fail("name is required")

    email = get("email")
    if not _EMAIL_RE.match(email):
        hint = _DISPLAY_NAME_HINT if _DISPLAY_NAME_RE.match(email) else ""
        fail("invalid email %r (expected local@domain.tld)%s" % (email, hint))
        email = ""
    email = email.lower()

    contact_id = get("id") or (_slug(email) if email else "")
    if contact_id and not _ID_RE.match(contact_id):
        fail(
            "invalid id %r (use letters, digits, dot, dash or underscore, starting with a letter or digit)"
            % contact_id
        )
        contact_id = ""

    lane: Optional[Lane]
    lane_raw = get("lane").lower().replace("-", "_")
    if not lane_raw:
        lane = Lane.FIRST_TOUCH
    else:
        try:
            lane = Lane(lane_raw)
        except ValueError:
            fail("invalid lane %r (expected first_touch or follow_up)" % get("lane"))
            lane = None

    thread_subject = get("thread_subject")
    subject = get("subject")
    in_reply_to = get("in_reply_to")
    for label, text in (
        ("thread_subject", thread_subject),
        ("in_reply_to", in_reply_to),
        ("subject", values.get("subject", "")),
    ):
        if "\n" in text or "\r" in text:
            fail("%s must be a single line" % label)
            if label == "thread_subject":
                thread_subject = ""
            elif label == "in_reply_to":
                in_reply_to = ""
    references = _clean(get("references"))

    if lane is Lane.FOLLOW_UP:
        if not in_reply_to:
            fail(
                "follow_up needs in_reply_to, the original message id like <id@host>, "
                "so the email stays in its thread"
            )
        elif not _MESSAGE_ID_RE.match(in_reply_to):
            fail("in_reply_to %r must look like <id@host> (angle brackets and an @)" % in_reply_to)
        if not thread_subject:
            fail("follow_up needs thread_subject, the subject of the original email")

    if problems or lane is None:
        return None, problems

    contact_type = ContactWithSubject if subject else Contact
    subject_fields = {"subject": subject} if subject else {}
    return (
        contact_type(
            id=contact_id,
            name=person,
            email=email,
            org=_clean(get("org")),
            role=_clean(get("role")),
            facts=_split_facts(values.get("facts", "")),
            source_urls=_split_urls(values.get("source_urls", "")),
            lane=lane,
            thread_subject=thread_subject,
            in_reply_to=in_reply_to,
            references=references,
            preferred_name=_clean(get("first_name")),
            **subject_fields,
        ),
        problems,
    )
