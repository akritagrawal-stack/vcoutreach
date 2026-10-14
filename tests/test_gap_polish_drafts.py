"""SPEC 23.1: the file name of a written draft always carries the id's exact case, on every kind of disk.

Every folder listing here uses os.listdir names. exists() would say yes for both spellings on a
case-insensitive disk (the default on macOS and Windows), which would hide exactly the bug under test.
"""
from __future__ import annotations

import json
import os

import pytest

from vcoutreach.drafts_io import read_draft, write_draft
from vcoutreach.pipeline import draft_all
from vcoutreach.providers import TemplateProvider

STALE_LINE = "removed stale draft"
RENAMES = [
    ("ines-marlowe", "Ines-Marlowe"),
    ("Ines-Marlowe", "ines-marlowe"),
    ("ines-marlowe", "INES-MARLOWE"),
    ("INES-MARLOWE", "iNES-mARLOWE"),
]
RENAME_IDS = [f"{old}-to-{new}" for old, new in RENAMES]


def names(folder):
    return sorted(os.listdir(folder))


def md_names(folder):
    return [n for n in names(folder) if n.casefold().endswith(".md")]


def ines(kit, ident, **over):
    return kit.draft(contact_id=ident, to="ines@example.com", **over)


def plant(folder, name, text="to: x@example.com\nsubject: planted\nstatus: ok\n\nBody\n"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


# ----- write_draft ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("old,new", RENAMES, ids=RENAME_IDS)
def test_a_case_only_rename_leaves_exactly_one_entry_named_with_the_new_case(kit, old, new):
    out = kit.workdir / "drafts"
    write_draft(out, ines(kit, old, subject="first version"))
    path = write_draft(out, ines(kit, new, subject="second version"))
    assert names(out) == [f"{new}.md"]
    assert path.name == f"{new}.md"


@pytest.mark.parametrize("old,new", RENAMES, ids=RENAME_IDS)
def test_the_renamed_file_holds_the_new_content(kit, old, new):
    out = kit.workdir / "drafts"
    write_draft(out, ines(kit, old, subject="first version", body="Hi Ines,\n\nFirst body.\n"))
    path = write_draft(out, ines(kit, new, subject="second version", body="Hi Ines,\n\nSecond body.\n"))
    (listed,) = md_names(out)
    assert listed == f"{new}.md"
    headers = kit.parse_md(out / listed)[1]
    assert headers["subject"] == "second version"
    assert "Second body." in (out / listed).read_text(encoding="utf-8")
    assert "First body." not in (out / listed).read_text(encoding="utf-8")
    assert read_draft(path).subject == "second version"


@pytest.mark.parametrize("old,new", RENAMES, ids=RENAME_IDS)
def test_a_blocked_draft_is_renamed_the_same_way(kit, old, new):
    out = kit.workdir / "drafts"
    write_draft(out, ines(kit, old, status="blocked", subject="first version"))
    path = write_draft(out, ines(kit, new, status="blocked", subject="second version"))
    assert names(out / "blocked") == [f"{new}.md"]
    assert path.name == f"{new}.md"
    assert kit.parse_md(out / "blocked" / f"{new}.md")[1]["subject"] == "second version"
    assert md_names(out) == [], "a blocked draft never lands in the top-level folder"


def test_several_earlier_spellings_are_all_replaced(kit):
    out = kit.workdir / "drafts"
    plant(out, "ines-marlowe.md")
    plant(out, "INES-MARLOWE.md")  # one and the same file on a case-insensitive disk
    plant(out, "Ines-marlowe.md")
    write_draft(out, ines(kit, "Ines-Marlowe", subject="newest"))
    assert names(out) == ["Ines-Marlowe.md"]
    assert kit.parse_md(out / "Ines-Marlowe.md")[1]["subject"] == "newest"


def test_a_different_case_extension_counts_as_the_same_name_ignoring_case(kit):
    out = kit.workdir / "drafts"
    plant(out, "ines-marlowe.MD")
    write_draft(out, ines(kit, "Ines-Marlowe"))
    assert names(out) == ["Ines-Marlowe.md"]


def test_only_entries_that_match_ignoring_case_are_removed(kit):
    out = kit.workdir / "drafts"
    plant(out, "ines-marlowe.md")
    for neighbor in ("ines-marlowe-2.md", "ines-marlowe.txt", "ines.md", "bob.md", "index.jsonl", "Ines-Marlowe-old.md"):
        plant(out, neighbor, text=f"neighbor {neighbor}\n")
    write_draft(out, ines(kit, "Ines-Marlowe"))
    assert names(out) == sorted(["Ines-Marlowe.md", "ines-marlowe-2.md", "ines-marlowe.txt", "ines.md", "bob.md", "index.jsonl", "Ines-Marlowe-old.md"])
    for neighbor in ("ines-marlowe-2.md", "ines-marlowe.txt", "ines.md", "bob.md", "index.jsonl", "Ines-Marlowe-old.md"):
        assert (out / neighbor).read_text(encoding="utf-8") == f"neighbor {neighbor}\n"


def test_the_blocked_folder_and_other_contacts_are_left_alone_by_a_rename_in_the_top_folder(kit):
    out = kit.workdir / "drafts"
    write_draft(out, kit.draft(contact_id="bob", to="bob@example.com", status="blocked"))
    write_draft(out, kit.draft(contact_id="amy", to="amy@example.com"))
    write_draft(out, ines(kit, "ines-marlowe"))
    write_draft(out, ines(kit, "Ines-Marlowe"))
    assert md_names(out) == ["Ines-Marlowe.md", "amy.md"]
    assert names(out / "blocked") == ["bob.md"]


def test_writing_the_same_spelling_again_still_leaves_one_file_with_the_latest_content(kit):
    out = kit.workdir / "drafts"
    write_draft(out, ines(kit, "Ines-Marlowe", subject="one"))
    write_draft(out, ines(kit, "Ines-Marlowe", subject="two"))
    assert names(out) == ["Ines-Marlowe.md"]
    assert kit.parse_md(out / "Ines-Marlowe.md")[1]["subject"] == "two"


def test_a_first_write_uses_the_exact_case_of_the_id(kit):
    out = kit.workdir / "drafts"
    path = write_draft(out, ines(kit, "Ines-Marlowe"))
    assert names(out) == ["Ines-Marlowe.md"] and path.name == "Ines-Marlowe.md"


@pytest.mark.parametrize("old,new,stem", [("A/B", "a/b", "a_b"), ("a/b", "A/B", "A_B")], ids=["upper-to-lower", "lower-to-upper"])
def test_the_rule_applies_to_the_safe_file_stem_of_an_id_that_needed_cleaning(kit, old, new, stem):
    out = kit.workdir / "drafts"
    write_draft(out, ines(kit, old))
    path = write_draft(out, ines(kit, new))
    assert names(out) == [f"{stem}.md"]
    assert path.name == f"{stem}.md"


def test_a_rename_in_a_folder_that_holds_a_hundred_other_drafts_touches_only_that_draft(kit):
    out = kit.workdir / "drafts"
    for number in range(100):
        plant(out, f"person-{number:03d}.md", text=f"p{number}\n")
    plant(out, "ines-marlowe.md")
    write_draft(out, ines(kit, "Ines-Marlowe"))
    listed = names(out)
    assert len(listed) == 101 and "Ines-Marlowe.md" in listed and "ines-marlowe.md" not in listed
    assert (out / "person-042.md").read_text(encoding="utf-8") == "p42\n"


# ----- through draft_all -------------------------------------------------------------------------------------------------------------

def person(kit, ident, *, blocked=False):
    over = {"facts": ()} if blocked else {}
    return kit.contact(id=ident, name="Ines Marlowe", email="ines@example.com", **over)


def index_ids(out):
    rows = [json.loads(line) for line in (out / "index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row["contact_id"] for row in rows]


@pytest.mark.parametrize("blocked", [False, True], ids=["ok-draft", "blocked-draft"])
@pytest.mark.parametrize("old,new", RENAMES, ids=RENAME_IDS)
def test_draft_all_keeps_one_file_named_with_the_new_case_and_prunes_nothing(kit, old, new, blocked):
    out = kit.workdir / "drafts"
    folder = out / "blocked" if blocked else out
    draft_all([person(kit, old, blocked=blocked)], kit.sender(), TemplateProvider(), out)
    removed = []
    draft_all([person(kit, new, blocked=blocked)], kit.sender(), TemplateProvider(), out, on_prune=removed.append)
    assert removed == []
    assert [n for n in names(folder) if n.casefold() == "ines-marlowe.md"] == [f"{new}.md"]
    assert index_ids(out) == [new]


def test_draft_all_after_a_rename_and_a_real_removal_reports_only_the_removed_contact(kit):
    out = kit.workdir / "drafts"
    draft_all([person(kit, "ines-marlowe"), kit.contact(id="zed", name="Zed Zero", email="zed@example.com")],
              kit.sender(), TemplateProvider(), out)
    removed = []
    draft_all([person(kit, "Ines-Marlowe")], kit.sender(), TemplateProvider(), out, on_prune=removed.append)
    assert removed == ["zed"]
    assert md_names(out) == ["Ines-Marlowe.md"]
    assert index_ids(out) == ["Ines-Marlowe"]


# ----- through the command line ------------------------------------------------------------------------------------------------------

def csv_person(kit, ident, *, blocked=False):
    facts = "" if blocked else "your essay on how small teams ship weekly"
    return kit.csv_row(id=ident, name="Ines Marlowe", email="ines@example.com", org="Northwind", facts=facts)


def csv_bob(kit):
    return kit.csv_row(id="bob", name="Bob Byte", email="bob@example.com", org="Northwind",
                       facts="your essay on how small teams ship weekly")


def run(kit, rows, *extra):
    contacts = kit.write_csv(rows, name="people.csv")
    sender = kit.write_sender()
    return kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline", *extra)


@pytest.mark.parametrize("blocked", [False, True], ids=["ok-draft", "blocked-draft"])
@pytest.mark.parametrize("old,new", RENAMES, ids=RENAME_IDS)
def test_the_command_line_leaves_one_file_with_the_new_case_and_no_removal_line(kit, old, new, blocked):
    first = run(kit, [csv_person(kit, old, blocked=blocked), csv_bob(kit)])
    assert first.code == 0, first.combined
    second = run(kit, [csv_person(kit, new, blocked=blocked), csv_bob(kit)])
    assert second.code == 0, second.combined
    assert STALE_LINE not in second.combined
    out = kit.workdir / "drafts"
    folder = out / "blocked" if blocked else out
    assert [n for n in names(folder) if n.casefold() == "ines-marlowe.md"] == [f"{new}.md"]
    assert "bob.md" in names(out)
    assert index_ids(out) == [new, "bob"]


def test_renaming_the_case_back_and_forth_always_ends_with_the_latest_spelling(kit):
    for spelling in ("ines-marlowe", "Ines-Marlowe", "ines-marlowe", "INES-MARLOWE"):
        res = run(kit, [csv_person(kit, spelling), csv_bob(kit)])
        assert res.code == 0, res.combined
        assert STALE_LINE not in res.combined
        out = kit.workdir / "drafts"
        assert [n for n in names(out) if n.casefold() == "ines-marlowe.md"] == [f"{spelling}.md"]
        assert index_ids(out) == [spelling, "bob"]


def test_a_rename_that_also_turns_a_blocked_draft_into_an_ok_one_writes_one_top_level_file_with_the_new_case(kit):
    run(kit, [csv_person(kit, "ines-marlowe", blocked=True), csv_bob(kit)])
    res = run(kit, [csv_person(kit, "Ines-Marlowe"), csv_bob(kit)])
    assert res.code == 0, res.combined
    out = kit.workdir / "drafts"
    assert [n for n in names(out) if n.casefold() == "ines-marlowe.md"] == ["Ines-Marlowe.md"]
    assert kit.parse_md(out / "Ines-Marlowe.md")[1]["status"] == "ok"
    assert index_ids(out) == ["Ines-Marlowe", "bob"]


def test_the_command_line_reports_a_real_removal_next_to_a_case_rename_for_the_removed_contact_only(kit):
    run(kit, [csv_person(kit, "ines-marlowe"), csv_bob(kit)])
    second = run(kit, [csv_person(kit, "Ines-Marlowe")])
    assert second.code == 0, second.combined
    lines = [ln.strip() for ln in second.err.splitlines() if STALE_LINE in ln]
    assert lines == ["removed stale draft bob (no longer in the contacts file)"]
    out = kit.workdir / "drafts"
    assert md_names(out) == ["Ines-Marlowe.md"]
