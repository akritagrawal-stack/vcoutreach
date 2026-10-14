"""SPEC 22.15 and 22.16: pruning compares safe file stems ignoring case, and trusts a previous index only if every line is sound."""
from __future__ import annotations

import json
import os

import pytest

from vcoutreach.drafts_io import prune_stale
from vcoutreach.pipeline import draft_all
from vcoutreach.providers import TemplateProvider

STALE_LINE = "removed stale draft"


def touch(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("to: x@example.com\nsubject: s\nstatus: ok\n\nBody\n", encoding="utf-8")
    return path


def case_insensitive_disk(directory):
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / "CaseProbe.tmp"
    probe.write_text("x", encoding="utf-8")
    try:
        return (directory / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def contacts(kit, *ids, facts=None):
    out = []
    for ident in ids:
        over = {} if facts is None else {"facts": facts}
        out.append(kit.contact(id=ident, name="Ines Marlowe", email=f"{ident.lower()}@example.com", **over))
    return out


# ----- 22.15 at the prune_stale API ------------------------------------------------------------------------------------------------

def test_a_case_only_rename_of_an_id_deletes_nothing(tmp_path):
    out = tmp_path / "out"
    kept = touch(out, "ines-marlowe.md")
    assert prune_stale(out, {"ines-marlowe"}, {"Ines-Marlowe"}) == []
    assert kept.exists()


def test_the_rename_in_the_other_direction_deletes_nothing(tmp_path):
    out = tmp_path / "out"
    kept = touch(out, "Ines-Marlowe.md")
    assert prune_stale(out, {"Ines-Marlowe"}, {"ines-marlowe"}) == []
    assert kept.exists()


def test_a_blocked_draft_is_protected_by_a_case_only_rename_too(tmp_path):
    out = tmp_path / "out"
    kept = touch(out / "blocked", "ines-marlowe.md")
    assert prune_stale(out, {"ines-marlowe"}, {"INES-MARLOWE"}) == []
    assert kept.exists()


def test_an_id_that_is_written_with_an_underscore_protects_the_previous_id_with_the_underscore(tmp_path):
    out = tmp_path / "out"
    kept = touch(out, "a_b.md")
    assert prune_stale(out, {"a_b"}, {"a/b"}) == []
    assert kept.exists()


def test_a_genuinely_removed_id_is_still_deleted(tmp_path):
    out = tmp_path / "out"
    touch(out, "gone.md")
    kept = touch(out, "keep.md")
    assert prune_stale(out, {"gone", "keep"}, {"keep"}) == ["gone"]
    assert not (out / "gone.md").exists() and kept.exists()


def test_a_removed_id_is_deleted_while_the_case_renamed_one_survives(tmp_path):
    out = tmp_path / "out"
    kept = touch(out, "ines-marlowe.md")
    touch(out, "zed.md")
    assert prune_stale(out, {"ines-marlowe", "zed"}, {"Ines-Marlowe"}) == ["zed"]
    assert kept.exists() and not (out / "zed.md").exists()


def test_an_id_that_only_looks_similar_is_not_protected(tmp_path):
    out = tmp_path / "out"
    touch(out, "ines-marlowe.md")
    assert prune_stale(out, {"ines-marlowe"}, {"ines-marlow"}) == ["ines-marlowe"]
    assert not (out / "ines-marlowe.md").exists()


# ----- 22.15 through draft_all and through the command line --------------------------------------------------------------------------

@pytest.mark.parametrize("old,new", [("ines-marlowe", "Ines-Marlowe"), ("Ines-Marlowe", "ines-marlowe"), ("ines-marlowe", "INES-MARLOWE")])
def test_draft_all_keeps_the_draft_after_a_case_only_rename(kit, old, new):
    out = kit.workdir / "drafts"
    draft_all(contacts(kit, old), kit.sender(), TemplateProvider(), out)
    removed = []
    draft_all(contacts(kit, new), kit.sender(), TemplateProvider(), out, on_prune=removed.append)
    assert removed == []
    # SPEC 23.1: exactly one file remains and it carries the new spelling (listed by name, exists() cannot tell the cases apart)
    survivors = [n for n in os.listdir(out) if n.casefold() == f"{old}.md".casefold()]
    assert survivors == [f"{new}.md"], "the draft for the renamed contact must exist once, under the new spelling"
    assert all(kit.parse_md(out / n)[1]["to"] == f"{old.lower()}@example.com" for n in survivors)


def person_row(kit, ident, *, facts="your essay on how small teams ship weekly"):
    return kit.csv_row(id=ident, name="Ines Marlowe", email=f"{ident.lower()}@example.com", org="Northwind", facts=facts)


def cli_draft(kit, rows):
    contacts_file = kit.write_csv(rows, name="people.csv")
    sender = kit.write_sender()
    return kit.cli("draft", "--contacts", contacts_file.name, "--sender", sender.name, "--offline")


@pytest.mark.parametrize("facts,folder", [("your essay on how small teams ship weekly", ""), ("", "blocked")], ids=["ok-draft", "blocked-draft"])
def test_the_command_line_keeps_the_draft_after_a_case_only_rename_and_prints_no_removal(kit, facts, folder):
    first = cli_draft(kit, [person_row(kit, "ines-marlowe", facts=facts), person_row(kit, "bob")])
    assert first.code == 0, first.combined
    second = cli_draft(kit, [person_row(kit, "Ines-Marlowe", facts=facts), person_row(kit, "bob")])
    assert second.code == 0, second.combined
    assert STALE_LINE not in second.combined
    directory = kit.workdir / "drafts" / folder if folder else kit.workdir / "drafts"
    survivors = [n for n in os.listdir(directory) if n.casefold() == "ines-marlowe.md"]
    assert survivors == ["Ines-Marlowe.md"], "SPEC 23.1: one file, named with the new spelling"
    assert all(kit.parse_md(directory / n)[1]["to"] == "ines-marlowe@example.com" for n in survivors)
    assert (kit.workdir / "drafts" / "bob.md").exists()


def test_a_real_removal_next_to_a_case_rename_is_reported_for_the_removed_contact_only(kit):
    cli_draft(kit, [person_row(kit, "ines-marlowe"), person_row(kit, "zed")])
    second = cli_draft(kit, [person_row(kit, "Ines-Marlowe")])
    assert second.code == 0, second.combined
    lines = [ln.strip() for ln in second.err.splitlines() if STALE_LINE in ln]
    assert lines == ["removed stale draft zed (no longer in the contacts file)"]
    assert not (kit.workdir / "drafts" / "zed.md").exists()


# ----- 22.16 a previous index with one damaged line is not trusted at all -------------------------------------------------------------------

def first_run(kit, out):
    draft_all(contacts(kit, "ca", "cb", "cc", "cd"), kit.sender(), TemplateProvider(), out)
    index = out / "index.jsonl"
    lines = index.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["contact_id"] for ln in lines] == ["ca", "cb", "cc", "cd"]
    return index, lines


def replaced(lines, position, text):
    return lines[:position] + [text] + lines[position + 1:]


DAMAGE = {
    "torn-extra-last-line": lambda lines: (lines + ['{"contact_id": "ce", "stat'], False),
    "torn-last-row-itself": lambda lines: (lines[:-1] + [lines[-1][: len(lines[-1]) // 2]], False),
    "garbage-middle-line": lambda lines: (replaced(lines, 1, '{"contact_id": "cb", "sta'), True),
    "json-list-row": lambda lines: (replaced(lines, 1, '["cb", "ok"]'), True),
    "object-without-contact-id": lambda lines: (replaced(lines, 1, '{"status": "ok"}'), True),
    "contact-id-is-a-number": lambda lines: (replaced(lines, 1, '{"contact_id": 5, "status": "ok"}'), True),
    "contact-id-is-null": lambda lines: (replaced(lines, 1, '{"contact_id": null}'), True),
    "contact-id-is-a-list": lambda lines: (replaced(lines, 1, '{"contact_id": ["cb"]}'), True),
    "bare-string-row": lambda lines: (replaced(lines, 1, '"cb"'), True),
    "bare-number-row": lambda lines: (replaced(lines, 1, "42"), True),
}


@pytest.mark.parametrize("name", list(DAMAGE), ids=list(DAMAGE))
def test_one_damaged_line_means_nothing_is_pruned_even_though_drafts_are_stale(kit, name):
    out = kit.workdir / "drafts"
    index, lines = first_run(kit, out)
    damaged, newline_at_end = DAMAGE[name](lines)
    index.write_text("\n".join(damaged) + ("\n" if newline_at_end else ""), encoding="utf-8")
    removed = []
    draft_all(contacts(kit, "ca", "cb"), kit.sender(), TemplateProvider(), out, on_prune=removed.append)
    assert removed == []
    assert (out / "cc.md").exists() and (out / "cd.md").exists(), "stale drafts must survive a doubtful index"
    assert (out / "ca.md").exists() and (out / "cb.md").exists()


def test_an_intact_index_prunes_the_two_stale_drafts_as_a_control(kit):
    out = kit.workdir / "drafts"
    first_run(kit, out)
    removed = []
    draft_all(contacts(kit, "ca", "cb"), kit.sender(), TemplateProvider(), out, on_prune=removed.append)
    assert sorted(removed) == ["cc", "cd"]
    assert not (out / "cc.md").exists() and not (out / "cd.md").exists()


def test_the_run_after_a_doubtful_index_writes_a_clean_one_that_works_again(kit):
    out = kit.workdir / "drafts"
    index, lines = first_run(kit, out)
    index.write_text("\n".join(lines + ['{"contact_id": "ce", "stat']), encoding="utf-8")
    draft_all(contacts(kit, "ca", "cb", "cc"), kit.sender(), TemplateProvider(), out)
    assert [json.loads(ln)["contact_id"] for ln in index.read_text(encoding="utf-8").splitlines()] == ["ca", "cb", "cc"]
    removed = []
    draft_all(contacts(kit, "ca"), kit.sender(), TemplateProvider(), out, on_prune=removed.append)
    assert sorted(removed) == ["cb", "cc"]


def test_the_command_line_prints_no_removal_when_the_previous_index_has_a_torn_last_line(kit):
    rows = [person_row(kit, name) for name in ("ca", "cb", "cc", "cd")]
    assert cli_draft(kit, rows).code == 0
    index = kit.workdir / "drafts" / "index.jsonl"
    index.write_text(index.read_text(encoding="utf-8") + '{"contact_id": "ce", "sta', encoding="utf-8")
    second = cli_draft(kit, rows[:2])
    assert second.code == 0, second.combined
    assert STALE_LINE not in second.combined
    assert (kit.workdir / "drafts" / "cc.md").exists() and (kit.workdir / "drafts" / "cd.md").exists()
