"""Gaps found by the second mutation run: the command line (SPEC sections 12 and 13: ``send ... [--limit N]``)."""
from __future__ import annotations

import re

import pytest

SEND_ENV = {"SEND_MODE": "test", "TEST_RECIPIENT_EMAIL": "me@mail.test", "SENDER_EMAIL": "sam@lumenlist.example.com"}
LIMIT_SKIP = re.compile(r"^skipped (\S+): limit\b")


def limit_skips(res):
    found = []
    for line in (res.out + "\n" + res.err).splitlines():
        match = LIMIT_SKIP.match(line.strip())
        if match:
            found.append(match.group(1))
    return found


def ok_stems(kit):
    return sorted(p.stem for p in (kit.workdir / "drafts").glob("*.md"))


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_send_limit_caps_the_plan_and_names_every_draft_it_left_out(kit, limit):
    kit.quickstart()
    stems = ok_stems(kit)
    assert len(stems) == 4
    res = kit.cli("send", "--drafts", "drafts", "--limit", str(limit), env=SEND_ENV)
    assert res.code == 0, res.combined
    assert sorted(limit_skips(res)) == stems[limit:], res.combined
    assert "Traceback" not in res.combined and res.net_attempts == ""


@pytest.mark.parametrize("extra", [[], ["--limit", "4"], ["--limit", "10"]], ids=["no-flag", "exactly-all", "more-than-all"])
def test_without_a_binding_limit_nothing_is_left_out(kit, extra):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", *extra, env=SEND_ENV)
    assert res.code == 0, res.combined
    assert limit_skips(res) == []


def test_a_limit_changes_the_plan_but_not_the_drafts_on_disk(kit):
    kit.quickstart()
    before = {p.name: p.read_bytes() for p in (kit.workdir / "drafts").glob("*.md")}
    kit.cli("send", "--drafts", "drafts", "--limit", "1", env=SEND_ENV)
    assert {p.name: p.read_bytes() for p in (kit.workdir / "drafts").glob("*.md")} == before
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists(), "a plan never writes the journal"
