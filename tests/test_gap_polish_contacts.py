"""SPEC 23.4: an email written as `Some Name <addr@host.tld>` gets a hint to use the bare address."""
from __future__ import annotations

import re

import pytest

from vcoutreach.contacts import load_contacts
from vcoutreach.types import ContactsError

HINT = "(use the bare address, without a display name)"
LETTERS = "abcdefghijklmnopqrstuvwxyz"
DISPLAY_NAME_EMAILS = [
    "Ann <ann@example.com>",
    "Ann Lee <ann.lee@example.com>",
    "Dr Ann Lee <ann@mail.example.com>",
    "  Ann <ann@example.com>  ",
]
PLAIN_BAD_EMAILS = ["not-an-email", "missing-tld@host", "@example.com", "two words@example.com", "a@@example.com", "plain.example.com", "trailing@dot."]


def row(kit, n, **over):
    base = dict(id=f"c{LETTERS[n]}", name=f"Person {LETTERS[n].upper()}", email=f"person{LETTERS[n]}@example.com", org="Org")
    base.update(over)
    return kit.csv_row(**base)


def rows_with(kit, overrides):
    return [row(kit, i, **overrides.get(i, {})) for i in range(5)]


def problem_lines(message):
    return [ln for ln in message.splitlines() if re.search(r"\brows?\s+#?\d+", ln, re.IGNORECASE)]


def line_for_row(message, number):
    found = [ln for ln in problem_lines(message) if re.search(rf"\brows?\s+#?{number}\b", ln, re.IGNORECASE)]
    assert len(found) == 1, f"expected exactly one line naming row {number}, got {found} in {message!r}"
    return found[0]


def error_for(kit, rows):
    path = kit.write_csv(rows)
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    return str(info.value)


@pytest.mark.parametrize("value", DISPLAY_NAME_EMAILS, ids=lambda v: v.strip().replace(" ", "_"))
def test_a_display_name_email_is_rejected_with_the_hint_on_the_line_for_its_row(kit, value):
    message = error_for(kit, rows_with(kit, {4: {"email": value}}))
    line = line_for_row(message, 6)  # data row 5 is file row 6, the header is row 1
    assert HINT in line, line
    assert message.count(HINT) == 1


def test_the_hint_names_the_right_row_when_the_bad_value_is_in_the_first_data_row(kit):
    message = error_for(kit, rows_with(kit, {0: {"email": "Ann <ann@example.com>"}}))
    assert HINT in line_for_row(message, 2)
    assert "row 1 " not in message and not re.search(r"\brow\s+1\b", message, re.IGNORECASE), "the header is row 1"


def test_each_display_name_row_gets_its_own_hint(kit):
    message = error_for(kit, rows_with(kit, {1: {"email": "Ann <ann@example.com>"}, 3: {"email": "Bo Lee <bo@example.com>"}}))
    assert HINT in line_for_row(message, 3)
    assert HINT in line_for_row(message, 5)
    assert message.count(HINT) == 2


@pytest.mark.parametrize("bad", PLAIN_BAD_EMAILS)
def test_an_invalid_email_without_a_display_name_gets_no_hint(kit, bad):
    message = error_for(kit, rows_with(kit, {4: {"email": bad}}))
    assert HINT not in message and "display name" not in message
    assert line_for_row(message, 6)


def test_a_plain_bad_email_next_to_a_display_name_one_gets_no_hint_on_its_own_line(kit):
    message = error_for(kit, rows_with(kit, {1: {"email": "not-an-email"}, 3: {"email": "Ann <ann@example.com>"}}))
    assert HINT not in line_for_row(message, 3)
    assert HINT in line_for_row(message, 5)


def test_the_bare_address_next_to_the_hint_style_value_is_accepted(kit):
    (contact,) = load_contacts(kit.write_csv([kit.csv_row(id="ann", name="Ann Lee", email="ann@example.com", org="Org")]))
    assert contact.email == "ann@example.com"


def test_the_message_still_names_the_problem_as_an_email_problem(kit):
    line = line_for_row(error_for(kit, rows_with(kit, {4: {"email": "Ann <ann@example.com>"}})), 6)
    assert "email" in line.lower()


def test_the_command_line_prints_the_hint_and_exits_2(kit):
    contacts = kit.write_csv([row(kit, 0), row(kit, 1, email="Ann <ann@example.com>")], name="people.csv")
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2, res.combined
    assert HINT in res.combined
    assert any(HINT in ln and re.search(r"\brow\s+3\b", ln, re.IGNORECASE) for ln in res.combined.splitlines()), res.combined
    assert "Traceback" not in res.combined
