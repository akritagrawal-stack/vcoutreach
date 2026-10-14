"""Gaps found by the second mutation run: contacts.load_contacts validation (SPEC sections 5, 19.6 and 21.1)."""
from __future__ import annotations

import pytest

from vcoutreach.contacts import load_contacts
from vcoutreach.types import ContactsError

LETTERS = "abcdefghijklmnopqrstuvwxyz"


def row(kit, n, **over):
    """Data row n (0-based). Names, ids and emails use letters only, so row numbers in a message are unambiguous."""
    base = dict(id=f"c{LETTERS[n]}", name=f"Person {LETTERS[n].upper()}",
                email=f"person{LETTERS[n]}@example.com", org="Org")
    base.update(over)
    return kit.csv_row(**base)


def five_rows(kit, overrides=None):
    overrides = overrides or {}
    return [row(kit, i, **overrides.get(i, {})) for i in range(5)]


def message_of(path):
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    return str(info.value)


# ----- duplicate ids (SPEC 5 and 21.1: "reported like any duplicate id, naming both rows") -------------------------------------------

def test_an_explicit_id_used_twice_is_rejected_naming_both_rows(kit):
    path = kit.write_csv(five_rows(kit, {1: {"id": "same"}, 3: {"id": "same"}}))
    kit.assert_names_rows(message_of(path), [2, 4], 5, path)


def test_an_id_repeated_in_three_rows_names_all_three(kit):
    path = kit.write_csv(five_rows(kit, {0: {"id": "same"}, 2: {"id": "same"}, 4: {"id": "same"}}))
    kit.assert_names_rows(message_of(path), [1, 3, 5], 5, path)


def test_an_explicit_id_that_equals_another_rows_default_id_is_a_duplicate(kit):
    default_id = load_contacts(kit.write_csv([row(kit, 0, id="")], name="alone.csv"))[0].id
    rows = [row(kit, 0, id=""), row(kit, 1, id=default_id)]
    path = kit.write_csv(rows)
    kit.assert_names_rows(message_of(path), [1, 2], 2, path)


def test_two_addresses_cannot_end_up_with_the_same_default_id_and_overwrite_each_other(kit):
    rows = [row(kit, 0, id="", email="a.b@example.com"), row(kit, 1, id="", email="a-b@example.com")]
    path = kit.write_csv(rows)
    try:
        contacts = load_contacts(path)
    except ContactsError as exc:
        kit.assert_names_rows(str(exc), [1, 2], 2, path)
        return
    ids = [c.id.casefold() for c in contacts]
    assert len(ids) == len(set(ids)), f"two contacts would write the same draft file: {ids}"


def test_ids_that_are_different_load_fine(kit):
    assert [c.id for c in load_contacts(kit.write_csv(five_rows(kit)))] == ["ca", "cb", "cc", "cd", "ce"]


# ----- ids that cannot be a safe file name -------------------------------------------------------------------------------------------------

BAD_IDS = ["../evil", "bad id!", ".hidden", "-lead", "a/b", "a\\b", "x/../../y"]


@pytest.mark.parametrize("bad", BAD_IDS, ids=["parent-dir", "space-and-bang", "leading-dot", "leading-dash", "slash", "backslash", "traversal"])
def test_an_id_that_is_not_a_safe_file_name_is_rejected_naming_the_row(kit, bad):
    path = kit.write_csv(five_rows(kit, {2: {"id": bad}}))
    message = message_of(path)
    kit.assert_names_rows(message, [3], 5, path)
    assert "id" in message.lower()


@pytest.mark.parametrize("good", ["ada", "ada-lovelace", "ada_lovelace", "ada.lovelace", "A1", "x-1.y_2"])
def test_ordinary_ids_are_accepted(kit, good):
    (c,) = load_contacts(kit.write_csv([row(kit, 0, id=good)]))
    assert c.id == good


# ----- a name is required -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "blank"])
def test_a_row_without_a_name_is_rejected_naming_the_row(kit, blank):
    path = kit.write_csv(five_rows(kit, {3: {"name": blank}}))
    message = message_of(path)
    kit.assert_names_rows(message, [4], 5, path)
    assert "name" in message.lower()


def test_several_rows_without_a_name_are_all_reported_together(kit):
    path = kit.write_csv(five_rows(kit, {0: {"name": ""}, 4: {"name": " "}}))
    kit.assert_names_rows(message_of(path), [1, 5], 5, path)


# ----- thread_subject and in_reply_to end up in mail headers, so they must be one line each ---------------------------------------------

def follow_up(**over):
    base = {"lane": "follow_up", "thread_subject": "a note for Person", "in_reply_to": "<orig@example.com>"}
    base.update(over)
    return base


MULTI_LINE_SUBJECTS = ["Intro\nBcc: x@example.org", "Intro\r\nBcc: x@example.org", "Intro\rBcc: x@example.org"]


@pytest.mark.parametrize("subject", MULTI_LINE_SUBJECTS, ids=["lf", "crlf", "cr"])
def test_a_thread_subject_with_a_line_break_is_rejected_naming_the_row(kit, subject):
    path = kit.write_csv(five_rows(kit, {2: follow_up(thread_subject=subject)}))
    message = message_of(path)
    kit.assert_names_rows(message, [3], 5, path)
    assert "line" in message.lower()


MULTI_LINE_REPLY_IDS = ["<orig@example.com>\r\nBcc: x@example.org", "<orig@example.com>\nBcc: x@example.org", "<orig\n@example.com>"]


@pytest.mark.parametrize("reply_to", MULTI_LINE_REPLY_IDS, ids=["crlf-after", "lf-after", "lf-inside"])
def test_an_in_reply_to_with_a_line_break_is_rejected_naming_the_row(kit, reply_to):
    path = kit.write_csv(five_rows(kit, {1: follow_up(in_reply_to=reply_to)}))
    kit.assert_names_rows(message_of(path), [2], 5, path)


def test_a_one_line_thread_subject_and_message_id_are_accepted(kit):
    contacts = load_contacts(kit.write_csv(five_rows(kit, {2: follow_up()})))
    assert contacts[2].thread_subject == "a note for Person" and contacts[2].in_reply_to == "<orig@example.com>"


# ----- required columns (SPEC 5: the header) --------------------------------------------------------------------------------------------------

def test_a_header_only_file_without_an_email_column_is_a_clear_error_not_an_empty_list(kit):
    path = kit.write_csv([], header=["id", "name", "org"])
    message = message_of(path)
    assert "email" in message.lower()


def test_a_file_with_rows_but_no_email_column_names_the_missing_column(kit):
    header = ["id", "name", "org"]
    path = kit.write_csv([["ca", "Person A", "Org"], ["cb", "Person B", "Org"]], header=header)
    assert "email" in message_of(path).lower()
