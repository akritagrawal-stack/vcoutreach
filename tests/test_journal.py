"""journal.Journal: the append-only send journal (SPEC sections 12 and 17)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from vcoutreach.journal import Journal
from vcoutreach.types import Lane


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lines_of(path):
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]


def utc_today():
    return datetime.now(timezone.utc).date()


# ----- key ------------------------------------------------------------------------------------------

def test_key_is_sha256_of_lowercase_recipient_pipe_lane():
    assert Journal.key("alice@example.com", "first_touch") == sha("alice@example.com|first_touch")


def test_key_lowercases_the_recipient():
    assert Journal.key("Alice@Example.COM", "first_touch") == Journal.key("alice@example.com", "first_touch")


def test_key_differs_by_lane_and_by_recipient():
    keys = {
        Journal.key("alice@example.com", "first_touch"),
        Journal.key("alice@example.com", "follow_up"),
        Journal.key("bob@example.com", "first_touch"),
    }
    assert len(keys) == 3


def test_key_is_a_64_char_lowercase_hex_digest():
    key = Journal.key("alice@example.com", "first_touch")
    assert len(key) == 64 and key == key.lower() and int(key, 16) >= 0


def test_key_with_a_lane_enum_matches_the_plain_string_value():
    assert Journal.key("alice@example.com", Lane.FOLLOW_UP) == sha("alice@example.com|follow_up")


def test_key_can_be_called_on_an_instance_too(kit):
    assert kit.journal().key("a@example.com", "first_touch") == Journal.key("a@example.com", "first_touch")


# ----- has / record ------------------------------------------------------------------------------------

def test_new_journal_has_nothing_and_creates_no_file(kit):
    j = kit.journal()
    assert j.has(Journal.key("a@example.com", "first_touch"), "live") is False
    assert j.count_today("live") == 0
    assert not kit.journal_path.exists()


def test_record_then_has_in_the_same_mode(kit):
    j = kit.journal()
    key = Journal.key("a@example.com", "first_touch")
    j.record(key, "live")
    assert j.has(key, "live") is True


def test_test_and_live_are_separate_namespaces(kit):
    j = kit.journal()
    key = Journal.key("a@example.com", "first_touch")
    j.record(key, "test")
    assert j.has(key, "test") is True
    assert j.has(key, "live") is False, "a test send must never block a later live send"
    j.record(key, "live")
    assert j.has(key, "live") is True


def test_different_keys_do_not_collide(kit):
    j = kit.journal()
    j.record(Journal.key("a@example.com", "first_touch"), "live")
    assert j.has(Journal.key("b@example.com", "first_touch"), "live") is False
    assert j.has(Journal.key("a@example.com", "follow_up"), "live") is False


def test_record_creates_missing_parent_directories(kit):
    path = kit.workdir / "fresh" / "deeper" / ".vcoutreach" / "journal.jsonl"
    Journal(path).record(Journal.key("a@example.com", "first_touch"), "live")
    assert path.exists()


def test_record_appends_one_json_object_per_line_with_key_mode_ts_and_extras(kit):
    j = kit.journal()
    key = Journal.key("a@example.com", "first_touch")
    j.record(key, "live", recipient="a@example.com", lane="first_touch", message_id="<m1@mail.test>", attempt=2)
    (line,) = lines_of(kit.journal_path)
    obj = json.loads(line)
    assert obj["key"] == key and obj["mode"] == "live"
    assert obj["recipient"] == "a@example.com" and obj["message_id"] == "<m1@mail.test>"
    assert obj["attempt"] == 2
    ts = datetime.fromisoformat(obj["ts"].replace("Z", "+00:00"))
    assert ts.tzinfo is not None and ts.utcoffset() == timedelta(0)
    assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 120


def test_test_mode_entries_carry_mode_test_on_disk(kit):
    kit.journal().record(Journal.key("a@example.com", "first_touch"), "test")
    assert json.loads(lines_of(kit.journal_path)[0])["mode"] == "test"


def test_journal_survives_reopening(kit):
    key = Journal.key("a@example.com", "first_touch")
    kit.journal().record(key, "live")
    reopened = kit.journal()
    assert reopened.has(key, "live") is True
    assert reopened.has(key, "test") is False


def test_journal_is_append_only(kit):
    j = kit.journal()
    j.record(Journal.key("a@example.com", "first_touch"), "live")
    snapshot = kit.journal_path.read_bytes()
    j.record(Journal.key("b@example.com", "first_touch"), "live")
    j.record(Journal.key("c@example.com", "first_touch"), "test")
    after = kit.journal_path.read_bytes()
    assert after.startswith(snapshot)
    assert len(lines_of(kit.journal_path)) == 3


def test_has_does_not_write(kit):
    j = kit.journal()
    j.record(Journal.key("a@example.com", "first_touch"), "live")
    before = kit.journal_path.read_bytes()
    for _ in range(5):
        j.has(Journal.key("z@example.com", "first_touch"), "live")
        j.count_today("live")
    assert kit.journal_path.read_bytes() == before


# ----- messy files ------------------------------------------------------------------------------------------------

def test_journal_file_without_a_trailing_newline_does_not_glue_the_next_entry(kit):
    first = Journal.key("a@example.com", "first_touch")
    kit.journal_path.parent.mkdir(parents=True)
    kit.journal_path.write_text(json.dumps({"key": first, "mode": "live", "ts": datetime.now(timezone.utc).isoformat()}),
                                encoding="utf-8")  # no newline, as after a hand edit
    j = kit.journal()
    second = Journal.key("b@example.com", "first_touch")
    j.record(second, "live")
    entries = [json.loads(ln) for ln in lines_of(kit.journal_path)]
    assert [e["key"] for e in entries] == [first, second]
    assert kit.journal().has(first, "live") and kit.journal().has(second, "live")


def test_blank_lines_in_the_journal_are_ignored(kit):
    first = Journal.key("a@example.com", "first_touch")
    kit.journal_path.parent.mkdir(parents=True)
    entry = json.dumps({"key": first, "mode": "live", "ts": datetime.now(timezone.utc).isoformat()})
    kit.journal_path.write_text("\n" + entry + "\n\n\n", encoding="utf-8")
    j = kit.journal()
    assert j.has(first, "live")
    assert j.count_today("live") == 1


def test_field_values_with_newlines_and_unicode_stay_on_one_line(kit):
    j = kit.journal()
    key = Journal.key("a@example.com", "first_touch")
    j.record(key, "live", note="line one\nline two \u2192 caf\u00e9", message_id="<x@y>")
    j.record(Journal.key("b@example.com", "first_touch"), "live")
    entries = [json.loads(ln) for ln in lines_of(kit.journal_path)]
    assert len(entries) == 2
    assert entries[0]["note"] == "line one\nline two \u2192 caf\u00e9"


# ----- count_today ------------------------------------------------------------------------------------------------------

def test_count_today_counts_only_the_requested_mode(kit):
    j = kit.journal()
    for i in range(3):
        j.record(Journal.key(f"live{i}@example.com", "first_touch"), "live")
    for i in range(2):
        j.record(Journal.key(f"test{i}@example.com", "first_touch"), "test")
    assert j.count_today("live") == 3
    assert j.count_today("test") == 2


def test_count_today_accepts_an_explicit_date(kit):
    j = kit.journal()
    j.record(Journal.key("a@example.com", "first_touch"), "live")
    j.record(Journal.key("b@example.com", "first_touch"), "live")
    assert j.count_today("live", today=utc_today()) == 2
    assert j.count_today("live", today=date(2001, 1, 1)) == 0
    assert j.count_today("live", date(2001, 1, 1)) == 0


def test_entries_from_yesterday_do_not_count_today(kit):
    j = kit.journal()
    for i in range(3):
        j.record(Journal.key(f"old{i}@example.com", "first_touch"), "live")
    today, yesterday = utc_today().isoformat(), (utc_today() - timedelta(days=1)).isoformat()
    text = kit.journal_path.read_text(encoding="utf-8")
    kit.journal_path.write_text(text.replace(today, yesterday), encoding="utf-8")
    j2 = kit.journal()
    assert j2.count_today("live") == 0
    j2.record(Journal.key("new@example.com", "first_touch"), "live")
    assert j2.count_today("live") == 1
    assert j2.count_today("live", today=utc_today() - timedelta(days=1)) == 3


# ----- concurrency -----------------------------------------------------------------------------------------------------------

def hammer(journals, per_thread, field_size):
    barrier = threading.Barrier(len(journals))
    errors = []

    def work(idx, journal):
        try:
            barrier.wait(timeout=10)
            for n in range(per_thread):
                key = Journal.key(f"t{idx}-{n}@example.com", "first_touch")
                journal.record(key, "live", recipient=f"t{idx}-{n}@example.com", pad="x" * field_size)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i, j)) for i, j in enumerate(journals)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors


def check_intact(path, expected):
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    entries = [json.loads(ln) for ln in raw.split("\n") if ln.strip()]  # any corrupt line raises here
    assert len(entries) == expected
    assert len({e["key"] for e in entries}) == expected
    return entries


def test_threads_sharing_one_journal_do_not_corrupt_lines(kit):
    j = kit.journal()
    hammer([j] * 8, per_thread=25, field_size=6000)
    check_intact(kit.journal_path, 200)


def test_threads_with_separate_journal_objects_on_one_file_do_not_corrupt_lines(kit):
    hammer([kit.journal() for _ in range(8)], per_thread=25, field_size=6000)
    entries = check_intact(kit.journal_path, 200)
    reopened = kit.journal()
    assert all(reopened.has(e["key"], "live") for e in entries)


def test_separate_processes_appending_to_one_file_do_not_corrupt_lines(kit):
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from vcoutreach.journal import Journal\n"
        "path, who = Path(sys.argv[1]), sys.argv[2]\n"
        "j = Journal(path)\n"
        "for n in range(30):\n"
        "    j.record(Journal.key(f'p{who}-{n}@example.com', 'first_touch'), 'live', pad='y' * 3000)\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(kit.journal_path), str(i)],
            env=kit.cli_env(net_guard=False), cwd=str(kit.workdir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(4)
    ]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
    check_intact(kit.journal_path, 120)


def test_journal_exposes_its_file_path_as_a_public_path_attribute(kit):
    j = kit.journal()
    assert isinstance(j.path, Path)
    assert j.path == kit.journal_path
    assert j.path.parent == kit.workdir / ".vcoutreach", "the send lock lives next to the journal"
