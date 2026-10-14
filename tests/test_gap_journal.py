"""Gaps found by the second mutation run: journal.Journal (SPEC sections 12 and 17)."""
from __future__ import annotations

import json

import pytest

from vcoutreach.journal import Journal

KEY_A = Journal.key("alice@example.com", "first_touch")
KEY_B = Journal.key("bob@example.com", "first_touch")
KEY_C = Journal.key("carol@example.com", "first_touch")


# ----- a torn line (a crash in the middle of an append) must not block every later send ------------------------------------------------

def seed(kit):
    journal = kit.journal()
    journal.record(KEY_A, "live")
    journal.record(KEY_B, "test")
    return journal


def test_an_unparsable_line_in_the_middle_is_ignored_by_has_and_count_today(kit):
    seed(kit)
    lines = kit.journal_path.read_text(encoding="utf-8").splitlines()
    lines.insert(1, '{"key": "abc", "mo')
    kit.journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reopened = kit.journal()
    assert reopened.has(KEY_A, "live") is True
    assert reopened.has(KEY_B, "test") is True
    assert reopened.has(KEY_C, "live") is False
    assert reopened.count_today("live") == 1 and reopened.count_today("test") == 1


def test_a_half_written_last_line_without_a_newline_is_ignored(kit):
    seed(kit)
    with open(kit.journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"key": "' + KEY_C[:10])
    reopened = kit.journal()
    assert reopened.has(KEY_A, "live") is True
    assert reopened.has(KEY_C, "live") is False
    assert reopened.count_today("live") == 1


@pytest.mark.parametrize("junk", ["", "   ", "not json at all", '{"key": ', "}{", "\x00\x00\x00"], ids=["empty", "blank", "text", "open-brace", "reversed", "nul"])
def test_junk_lines_never_raise_and_never_count(kit, junk):
    seed(kit)
    with open(kit.journal_path, "a", encoding="utf-8") as handle:
        handle.write(junk + "\n")
    reopened = kit.journal()
    assert reopened.has(KEY_A, "live") is True
    assert reopened.count_today("live") == 1 and reopened.count_today("test") == 1


def test_recording_after_a_torn_line_still_works(kit):
    seed(kit)
    with open(kit.journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"key": "abc", "mo\n')
    journal = kit.journal()
    journal.record(KEY_C, "live")
    assert kit.journal().has(KEY_C, "live") is True
    assert kit.journal().count_today("live") == 2


# ----- only "test" and "live" exist (SPEC 17: ``mode is "test" or "live"``) ----------------------------------------------------------

@pytest.mark.parametrize("bogus", ["bogus", "", "prod"], ids=["bogus", "empty", "prod"])
def test_has_rejects_a_mode_that_is_neither_test_nor_live(kit, bogus):
    with pytest.raises(ValueError):
        kit.journal().has(KEY_A, bogus)


@pytest.mark.parametrize("bogus", ["bogus", "", "prod"], ids=["bogus", "empty", "prod"])
def test_count_today_rejects_a_mode_that_is_neither_test_nor_live(kit, bogus):
    with pytest.raises(ValueError):
        kit.journal().count_today(bogus)


@pytest.mark.parametrize("bogus", ["bogus", "", "prod"], ids=["bogus", "empty", "prod"])
def test_record_rejects_a_mode_that_is_neither_test_nor_live_and_writes_nothing(kit, bogus):
    journal = kit.journal()
    with pytest.raises(ValueError):
        journal.record(KEY_A, bogus)
    assert kit.journal_lines() == []


def test_the_two_real_modes_still_work(kit):
    journal = kit.journal()
    journal.record(KEY_A, "test")
    journal.record(KEY_A, "live", note="x")
    entries = kit.journal_lines()
    assert [e["mode"] for e in entries] == ["test", "live"]
    assert all(set(e) >= {"key", "mode", "ts"} for e in entries)
    assert json.dumps(entries)
