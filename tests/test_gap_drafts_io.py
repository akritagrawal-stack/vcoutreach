"""Gaps found by the second mutation run: drafts_io (SPEC sections 17, 20.3 and 21.3)."""
from __future__ import annotations

import pytest

from vcoutreach.drafts_io import read_draft, read_drafts, write_draft
from vcoutreach.types import OutreachError


def files_under(root):
    return {p for p in root.rglob("*") if p.is_file()}


def assert_stays_inside(kit, out, ident, status):
    """Write a draft for ``ident`` and check that nothing lands outside ``out`` (or ``out/blocked``)."""
    before = files_under(kit.tmp_path)
    allowed = {out.resolve(), (out / "blocked").resolve()}
    try:
        path = write_draft(out, kit.draft(contact_id=ident, status=status))
    except (ValueError, OutreachError, OSError):
        path = None  # refusing an unsafe id is just as safe
    for created in files_under(kit.tmp_path) - before:
        assert created.resolve().parent in allowed, f"{created} was written outside the output directory"
    if path is not None:
        assert path.resolve().parent in allowed, f"{path} is not directly inside the output directory"


# ----- a contact id with path characters cannot choose where the draft is written ------------------------------------------------

PATHY_IDS = ["a/b", "x/../../y", "../evil", "..", ".", "sub/dir/deep", "a/../b"]


@pytest.mark.parametrize("ident", PATHY_IDS, ids=["slash", "traversal", "parent", "dotdot", "dot", "deep", "inner-dotdot"])
@pytest.mark.parametrize("status", ["ok", "blocked"])
def test_an_id_with_path_characters_never_writes_outside_the_output_directory(kit, ident, status):
    assert_stays_inside(kit, kit.workdir / "drafts", ident, status)


@pytest.mark.parametrize("status", ["ok", "blocked"])
def test_an_absolute_id_never_writes_where_it_points(kit, status):
    target = kit.tmp_path / "elsewhere" / "escaped"
    assert_stays_inside(kit, kit.workdir / "drafts", str(target), status)
    assert not target.with_suffix(".md").exists() and not (kit.tmp_path / "elsewhere").exists()


def test_two_different_unsafe_ids_do_not_leave_files_in_subfolders(kit):
    out = kit.workdir / "drafts"
    for ident in ("team/ada", "team/bob"):
        assert_stays_inside(kit, out, ident, "ok")
    assert [p for p in out.rglob("*") if p.is_dir() and p.name != "blocked"] == []


def test_an_ordinary_id_is_written_as_id_dot_md(kit):
    out = kit.workdir / "drafts"
    assert write_draft(out, kit.draft(contact_id="ada-1", status="ok")) == out / "ada-1.md"
    assert write_draft(out, kit.draft(contact_id="bob", status="blocked")) == out / "blocked" / "bob.md"


# ----- a draft file without its status line is refused, not a KeyError (fail closed) -------------------------------------------------

NO_STATUS = ("to: a@example.com\nsubject: s\nlane: first_touch\nprovider: template\n"
             "in_reply_to:\nreferences:\nissues:\n\nHi,\n\nBody")


def test_read_draft_without_a_status_line_is_an_outreach_error_naming_it(kit):
    path = kit.workdir / "nostatus.md"
    path.write_text(NO_STATUS, encoding="utf-8")
    with pytest.raises(OutreachError) as info:
        read_draft(path)
    assert "status" in str(info.value)


def test_read_drafts_stops_on_a_draft_without_a_status_line(kit):
    out = kit.workdir / "drafts"
    write_draft(out, kit.draft(contact_id="ok-one", to="one@example.com"))
    (out / "nostatus.md").write_text(NO_STATUS, encoding="utf-8")
    with pytest.raises(OutreachError):
        read_drafts(out)
