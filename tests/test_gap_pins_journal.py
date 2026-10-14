"""SPEC section 22.6: private journal and lock file, and a new entry is never glued to a torn fragment."""
from __future__ import annotations

import json
import os
import stat

import pytest

from vcoutreach.journal import Journal
from vcoutreach.send.gate import plan_sends

KEY_A = Journal.key("alice@example.com", "first_touch")
KEY_B = Journal.key("bob@example.com", "first_touch")
KEY_C = Journal.key("carol@example.com", "first_touch")

posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX notion")


@pytest.fixture
def open_umask():
    """Run with a wide-open umask, so a mode of 0600 can only come from the code asking for it."""
    old = os.umask(0)
    try:
        yield
    finally:
        os.umask(old)


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ----- the journal file is created with mode 0600 ------------------------------------------------------------------------------

@posix_only
@pytest.mark.parametrize("mode", ["test", "live"])
def test_the_journal_file_is_private_when_first_created(kit, open_umask, mode):
    kit.journal().record(KEY_A, mode)
    assert mode_of(kit.journal_path) == 0o600


@posix_only
def test_the_journal_file_stays_private_after_more_entries(kit, open_umask):
    journal = kit.journal()
    for key in (KEY_A, KEY_B, KEY_C):
        journal.record(key, "live")
    assert mode_of(kit.journal_path) == 0o600


# ----- the send lock file is created with mode 0600 ----------------------------------------------------------------------------------

def probe_class(kit):
    base = type(kit.mail_transport())

    class Probe(base):
        def __init__(self, lock):
            super().__init__()
            self.lock = lock
            self.modes = []

        def send(self, message):
            if self.lock.exists():
                self.modes.append(mode_of(self.lock))
            return super().send(message)

    return Probe


@posix_only
def test_the_send_lock_file_is_private_while_a_run_holds_it(kit, open_umask):
    lock = kit.journal_path.parent / "send.lock"
    transport = probe_class(kit)(lock)
    drafts = [kit.draft(contact_id="alice", to="alice@example.com"), kit.draft(contact_id="bob", to="bob@example.com")]
    plan_sends(drafts, kit.send_settings("test"), kit.sender(), kit.journal(), execute=True, transport=transport)
    assert len(transport.sent) == 2
    assert transport.modes, "the lock file must exist while the run is sending"
    assert set(transport.modes) == {0o600}


@posix_only
def test_a_lock_file_that_is_left_behind_is_private_too(kit, open_umask):
    lock = kit.journal_path.parent / "send.lock"
    plan_sends([kit.draft(contact_id="alice", to="alice@example.com")], kit.send_settings("test"), kit.sender(), kit.journal(),
               execute=True, transport=kit.mail_transport())
    if lock.exists():
        assert mode_of(lock) == 0o600


# ----- a torn last line (no newline) is closed off before the next entry is written -----------------------------------------------

def seed_with_fragment(kit, fragment='{"key": "' + KEY_C[:12]):
    journal = kit.journal()
    journal.record(KEY_A, "live")
    with open(kit.journal_path, "a", encoding="utf-8") as handle:
        handle.write(fragment)  # a crash in the middle of an append: no newline
    return journal


def test_a_new_entry_after_a_torn_last_line_parses_and_is_seen(kit):
    seed_with_fragment(kit)
    kit.journal().record(KEY_B, "live", note="after the crash")
    reopened = kit.journal()
    assert reopened.has(KEY_B, "live") is True
    assert reopened.has(KEY_A, "live") is True
    assert reopened.has(KEY_C, "live") is False
    assert reopened.count_today("live") == 2


def test_the_new_entry_sits_on_its_own_line(kit):
    seed_with_fragment(kit)
    kit.journal().record(KEY_B, "live")
    lines = kit.journal_path.read_text(encoding="utf-8").split("\n")
    assert lines[-1] == "", "the file must end with a newline"
    parsed = []
    for line in lines[:-1]:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            parsed.append(None)
    assert parsed[0]["key"] == KEY_A
    assert parsed[1] is None, "the fragment stays a line of its own"
    assert parsed[-1]["key"] == KEY_B and parsed[-1]["mode"] == "live"


def test_two_records_after_a_torn_line_are_both_readable(kit):
    seed_with_fragment(kit)
    journal = kit.journal()
    journal.record(KEY_B, "live")
    journal.record(KEY_C, "test")
    reopened = kit.journal()
    assert reopened.has(KEY_B, "live") and reopened.has(KEY_C, "test")
    assert reopened.count_today("live") == 2 and reopened.count_today("test") == 1


def test_a_clean_file_gets_no_blank_line_inserted(kit):
    journal = kit.journal()
    journal.record(KEY_A, "live")
    journal.record(KEY_B, "live")
    text = kit.journal_path.read_text(encoding="utf-8")
    assert text.endswith("\n") and "\n\n" not in text
    assert len(text.splitlines()) == 2


def test_the_very_first_entry_does_not_start_with_a_newline(kit):
    kit.journal().record(KEY_A, "live")
    assert kit.journal_path.read_text(encoding="utf-8").startswith("{")


def test_a_torn_fragment_alone_in_the_file_is_closed_off_too(kit):
    kit.journal_path.parent.mkdir(parents=True, exist_ok=True)
    kit.journal_path.write_text('{"key": "abc", "mo', encoding="utf-8")
    kit.journal().record(KEY_A, "live")
    assert kit.journal().has(KEY_A, "live") is True
    assert kit.journal().count_today("live") == 1
