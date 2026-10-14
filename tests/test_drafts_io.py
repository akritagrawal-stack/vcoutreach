"""drafts_io: markdown draft files and index.jsonl (SPEC sections 6 and 17)."""
from __future__ import annotations

import json
import re

import pytest

from vcoutreach.drafts_io import prune_stale, read_draft, read_drafts, write_draft, write_index
from vcoutreach.types import Draft, Hook, Lane, LintIssue, OutreachError

HEADER_KEYS = ["to", "subject", "status", "lane", "provider", "in_reply_to", "references", "issues"]


def header_keys(path):
    head = path.read_text(encoding="utf-8").split("\n\n", 1)[0]
    return [ln.split(":", 1)[0] for ln in head.split("\n")]


# ----- write_draft ---------------------------------------------------------------------------------------

def test_ok_draft_goes_to_the_top_level_blocked_draft_to_blocked_subdir(kit):
    out = kit.workdir / "drafts"
    ok = write_draft(out, kit.draft(contact_id="alice", status="ok"))
    blocked = write_draft(out, kit.draft(contact_id="bob", to="bob@example.com", status="blocked"))
    assert ok == out / "alice.md" and ok.exists()
    assert blocked == out / "blocked" / "bob.md" and blocked.exists()


def test_write_draft_creates_missing_directories(kit):
    path = write_draft(kit.workdir / "a" / "b" / "drafts", kit.draft())
    assert path.exists()


def test_file_has_exactly_the_documented_header_then_one_blank_line_then_the_body(kit):
    draft = kit.draft(
        contact_id="grace", to="grace@example.org", subject="a note for Grace",
        lane=Lane.FOLLOW_UP, in_reply_to="<orig@example.com>", references="<a@example.com> <orig@example.com>",
        hook=kit.hook(kit.HOOK, provider="template", model="none"),
    )
    path = write_draft(kit.workdir / "drafts", draft)
    expected = kit.md_text(
        to="grace@example.org", subject="a note for Grace", body=draft.body, status="ok",
        lane="follow_up", provider="template", in_reply_to="<orig@example.com>",
        references="<a@example.com> <orig@example.com>",
    )
    assert path.read_text(encoding="utf-8") == expected + "\n", "header, blank line, body, exactly one final newline"


def test_header_keys_come_in_the_documented_order(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft())
    assert header_keys(path) == HEADER_KEYS


def test_empty_header_values_are_written_as_the_bare_key_and_colon(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft())
    lines = path.read_text(encoding="utf-8").split("\n")
    assert "in_reply_to:" in lines and "references:" in lines


def test_lane_is_written_as_the_plain_value_not_the_enum_repr(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(lane=Lane.FIRST_TOUCH))
    _, headers, _ = kit.parse_md(path)
    assert headers["lane"] == "first_touch"
    path = write_draft(kit.workdir / "drafts", kit.follow_up_draft())
    _, headers, _ = kit.parse_md(path)
    assert headers["lane"] == "follow_up"


def test_rewriting_the_same_contact_id_overwrites_the_file(kit):
    out = kit.workdir / "drafts"
    write_draft(out, kit.draft(subject="first version"))
    write_draft(out, kit.draft(subject="second version"))
    assert [p.name for p in out.glob("*.md")] == ["alice.md"]
    assert read_draft(out / "alice.md").subject == "second version"


def test_non_ascii_text_round_trips_as_utf8(kit):
    body = kit.clean_body(first_name="Zo\u00eb", hook="I read your caf\u00e9 essay in \u65e5\u672c\u8a9e, and I found it interesting.")
    path = write_draft(kit.workdir / "drafts", kit.draft(contact_id="zoe", subject="caf\u00e9 note", body=body))
    assert read_draft(path).body == body
    assert "caf\u00e9 note" in path.read_bytes().decode("utf-8")


# ----- read_draft ------------------------------------------------------------------------------------------------------

def test_write_then_read_round_trips_every_field_except_the_hook_text_and_issues(kit):
    original = kit.follow_up_draft(
        hook=kit.hook("some hook", provider="publik", model="m"),
        issues=(LintIssue("word_count", "short", "warning"),),
    )
    path = write_draft(kit.workdir / "drafts", original)
    back = read_draft(path)
    assert isinstance(back, Draft)
    assert (back.contact_id, back.to, back.subject, back.body, back.status) == (
        original.contact_id, original.to, original.subject, original.body, original.status)
    assert back.lane is Lane.FOLLOW_UP
    assert back.in_reply_to == original.in_reply_to and back.references == original.references
    assert back.hook == Hook(text="", provider="publik")
    assert back.issues == ()


def test_contact_id_comes_from_the_file_name(kit):
    path = kit.workdir / "hand-made.md"
    path.write_text(kit.md_text(to="a@example.com", subject="s", body="Hi,\n\nBody"), encoding="utf-8")
    assert read_draft(path).contact_id == "hand-made"


def test_status_is_taken_from_the_header(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(contact_id="bob", status="blocked"))
    assert read_draft(path).status == "blocked"
    path = write_draft(kit.workdir / "drafts", kit.draft(contact_id="amy", status="ok"))
    assert read_draft(path).status == "ok"


def test_empty_threading_values_read_back_as_empty_strings_and_first_touch_lane(kit):
    path = kit.workdir / "plain.md"
    path.write_text(kit.md_text(to="a@example.com", subject="s", body="Hi,\n\nBody"), encoding="utf-8")
    d = read_draft(path)
    assert d.in_reply_to == "" and d.references == "" and d.lane is Lane.FIRST_TOUCH


def test_subject_with_colons_survives(kit):
    subject = "Re: intro: part two: the sequel"
    path = write_draft(kit.workdir / "drafts", kit.draft(subject=subject))
    assert read_draft(path).subject == subject


def test_body_lines_that_look_like_headers_do_not_confuse_the_parser(kit):
    body = "to: evil@example.com\nstatus: blocked\n\nHi Ada,\n\nsubject: not a header either\n\nSam"
    path = write_draft(kit.workdir / "drafts", kit.draft(body=body))
    back = read_draft(path)
    assert back.to == "alice@example.com"
    assert back.status == "ok"
    assert back.body == body


def test_body_whitespace_and_signature_line_breaks_are_kept_exactly(kit):
    body = kit.clean_body()
    path = write_draft(kit.workdir / "drafts", kit.draft(body=body))
    assert read_draft(path).body == body


def test_a_subject_with_a_newline_cannot_forge_header_fields(kit):
    draft = kit.draft(contact_id="bob", to="bob@example.com", status="blocked",
                      subject="hello\nstatus: ok\nto: attacker@example.com")
    try:
        path = write_draft(kit.workdir / "drafts", draft)
    except (ValueError, OutreachError):
        return  # refusing a subject that contains a newline is just as safe
    back = read_draft(path)
    assert back.status == "blocked"
    assert back.to == "bob@example.com"
    assert back.body == draft.body
    assert header_keys(path) == HEADER_KEYS


# ----- the `issues` header (SPEC 19.2) ---------------------------------------------------------------------------

def issues_of(*pairs):
    return tuple(LintIssue(code, f"UNIQUE-MESSAGE-TEXT for {code}", severity) for code, severity in pairs)


def issues_line(path):
    _, headers, _ = kit_parse(path)
    return headers["issues"]


def kit_parse(path):
    head, sep, body = path.read_text(encoding="utf-8").partition("\n\n")
    headers = {}
    for ln in head.split("\n"):
        key, _, value = ln.partition(":")
        headers[key.strip()] = value.strip()
    return head.split("\n"), headers, body


def test_issues_is_the_last_header_key_right_after_references(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft())
    keys = header_keys(path)
    assert keys[-2:] == ["references", "issues"]
    assert keys.count("issues") == 1


def test_a_draft_without_issues_gets_an_empty_issues_line(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft())
    assert "issues:" in path.read_text(encoding="utf-8").split("\n")
    assert issues_line(path) == ""


def test_issue_codes_are_joined_with_semicolon_space(kit):
    issues = issues_of(("em_dash", "error"), ("exclamation", "error"), ("odd_greeting", "warning"))
    path = write_draft(kit.workdir / "drafts", kit.draft(status="blocked", issues=issues))
    assert issues_line(path) == "em_dash; exclamation; odd_greeting"
    assert "issues: em_dash; exclamation; odd_greeting" in path.read_text(encoding="utf-8").split("\n")


def test_a_single_issue_has_no_separator(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(issues=issues_of(("odd_greeting", "warning"))))
    assert issues_line(path) == "odd_greeting"


def test_warnings_on_an_ok_draft_are_listed_too(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(status="ok", issues=issues_of(("word_count", "warning"))))
    assert path.parent.name == "drafts"
    assert issues_line(path) == "word_count"


def test_only_the_codes_are_written_not_the_messages(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(issues=issues_of(("word_count", "warning"), ("filler", "error"))))
    assert "UNIQUE-MESSAGE-TEXT" not in path.read_text(encoding="utf-8")


def test_issue_codes_keep_the_order_of_the_draft(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(issues=issues_of(("zeta", "error"), ("alpha", "warning"), ("mid", "error"))))
    assert issues_line(path) == "zeta; alpha; mid"


def test_the_body_is_still_written_exactly_after_the_new_header_key(kit):
    draft = kit.draft(issues=issues_of(("word_count", "warning")))
    path = write_draft(kit.workdir / "drafts", draft)
    _, _, body = kit_parse(path)
    assert body.rstrip("\n") == draft.body.rstrip("\n")
    assert read_draft(path).body == draft.body


def test_read_draft_ignores_the_issues_header(kit):
    draft = kit.draft(status="blocked", issues=issues_of(("em_dash", "error"), ("odd_greeting", "warning")))
    back = read_draft(write_draft(kit.workdir / "drafts", draft))
    assert back.issues == ()
    assert back.status == "blocked" and back.to == draft.to and back.body == draft.body


def test_a_hand_edited_issues_value_changes_nothing_on_read(kit):
    path = kit.workdir / "hand.md"
    path.write_text(kit.md_text(to="a@example.com", subject="s", body="Hi,\n\nBody", status="ok",
                                issues="em_dash; status: blocked; to: evil@example.com"), encoding="utf-8")
    back = read_draft(path)
    assert back.issues == () and back.status == "ok" and back.to == "a@example.com"


def test_a_draft_file_from_before_the_issues_key_still_reads(kit):
    path = kit.workdir / "old.md"
    path.write_text("to: a@example.com\nsubject: s\nstatus: ok\nlane: first_touch\nprovider: template\n"
                    "in_reply_to:\nreferences:\n\nHi,\n\nBody", encoding="utf-8")
    back = read_draft(path)
    assert back.to == "a@example.com" and back.body == "Hi,\n\nBody" and back.issues == ()


def test_a_body_line_that_looks_like_the_issues_header_stays_in_the_body(kit):
    body = "issues: em_dash\n\nHi Ada,\n\nissues: not a header either\n\nSam"
    path = write_draft(kit.workdir / "drafts", kit.draft(body=body, issues=issues_of(("word_count", "warning"))))
    back = read_draft(path)
    assert back.body == body and back.issues == ()
    assert issues_line(path) == "word_count"


def test_a_subject_with_a_newline_cannot_forge_the_issues_header(kit):
    draft = kit.draft(subject="hello\nissues: nothing to see", issues=issues_of(("em_dash", "error")))
    try:
        path = write_draft(kit.workdir / "drafts", draft)
    except (ValueError, OutreachError):
        return
    assert header_keys(path) == HEADER_KEYS
    assert issues_line(path) == "em_dash"


def test_the_index_still_carries_full_issue_objects(kit):
    draft = kit.draft(issues=issues_of(("word_count", "warning")))
    (row,) = [json.loads(ln) for ln in write_index(kit.workdir / "drafts", [draft]).read_text(encoding="utf-8").splitlines()]
    assert row["issues"] == [{"code": "word_count", "message": "UNIQUE-MESSAGE-TEXT for word_count", "severity": "warning"}]


# ----- one trailing newline (SPEC 20.9) ---------------------------------------------------------------------------

BODIES = {
    "clean": lambda kit: kit.clean_body(),
    "trailing-spaces": lambda kit: "Hi Ada,  \n\nA line with trailing spaces   \n\nSam  ",
    "unicode": lambda kit: kit.clean_body(first_name="Zo" + chr(0xEB), hook="I read your caf" + chr(0xE9) + " essay, and I found it interesting."),
    "single-line": lambda kit: "Just one line",
    "empty": lambda kit: "",
}


def test_a_draft_file_ends_with_exactly_one_newline(kit):
    for draft in (kit.draft(), kit.draft(contact_id="bob", to="bob@example.com", status="blocked"), kit.follow_up_draft()):
        text = write_draft(kit.workdir / "drafts", draft).read_text(encoding="utf-8")
        assert text.endswith("\n") and not text.endswith("\n\n"), repr(text[-20:])


@pytest.mark.parametrize("name", list(BODIES), ids=list(BODIES))
def test_write_then_read_returns_the_original_body_exactly(kit, name):
    draft = kit.draft(body=BODIES[name](kit))
    assert read_draft(write_draft(kit.workdir / "drafts", draft)).body == draft.body


def test_an_empty_body_still_ends_the_file_with_a_newline(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(body=""))
    assert path.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.parametrize("flavour", ["first_touch", "follow_up"])
def test_write_read_write_gives_byte_identical_files(kit, flavour):
    hook = kit.hook(kit.HOOK, provider="template", model="none")
    draft = kit.draft(hook=hook) if flavour == "first_touch" else kit.follow_up_draft(hook=hook)
    first = write_draft(kit.workdir / "one", draft)
    second = write_draft(kit.workdir / "two", read_draft(first))
    assert first.read_bytes() == second.read_bytes()


def test_read_draft_strips_exactly_one_trailing_newline(kit):
    head = kit.md_text(to="a@example.com", subject="s", body="Hi,\n\nBody")
    for suffix, expected in (("", "Hi,\n\nBody"), ("\n", "Hi,\n\nBody"), ("\n\n", "Hi,\n\nBody\n"), ("\n\n\n", "Hi,\n\nBody\n\n")):
        path = kit.workdir / "hand.md"
        path.write_text(head + suffix, encoding="utf-8")
        assert read_draft(path).body == expected, repr(suffix)


# ----- the provider header comes back as a placeholder hook (SPEC 20.10) -----------------------------------------------

@pytest.mark.parametrize("provider", ["template", "publik", "openai", "anthropic"])
def test_read_draft_turns_the_provider_header_into_a_hook_with_empty_text(kit, provider):
    path = kit.workdir / "p.md"
    path.write_text(kit.md_text(to="a@example.com", subject="s", body="Hi,\n\nBody", provider=provider), encoding="utf-8")
    hook = read_draft(path).hook
    assert hook == Hook(text="", provider=provider)
    assert hook.text == "" and hook.provider == provider


def test_a_padded_provider_header_is_stripped(kit):
    path = kit.workdir / "p.md"
    path.write_text(kit.md_text(to="a@example.com", subject="s", body="b", provider="  publik  "), encoding="utf-8")
    assert read_draft(path).hook == Hook(text="", provider="publik")


def test_an_empty_or_missing_provider_header_means_no_hook(kit):
    empty = kit.workdir / "empty.md"
    empty.write_text(kit.md_text(to="a@example.com", subject="s", body="b", provider=""), encoding="utf-8")
    assert read_draft(empty).hook is None
    old = kit.workdir / "old.md"
    old.write_text("to: a@example.com\nsubject: s\nstatus: ok\nlane: first_touch\n\nBody", encoding="utf-8")
    assert read_draft(old).hook is None


def test_a_draft_written_without_a_hook_reads_back_without_one(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft(hook=None))
    assert read_draft(path).hook is None


def test_read_drafts_carries_the_provider_of_every_file(kit):
    out = kit.workdir / "drafts"
    write_draft(out, kit.draft(contact_id="amy", to="amy@example.com", hook=kit.hook(kit.HOOK, provider="template")))
    write_draft(out, kit.draft(contact_id="bob", to="bob@example.com", hook=kit.hook(kit.HOOK, provider="publik")))
    write_draft(out, kit.draft(contact_id="cat", to="cat@example.com", hook=None))
    providers = [d.hook.provider if d.hook else None for d in read_drafts(out)]
    assert providers == ["template", "publik", None]


# ----- prune_stale (SPEC 20.3) -------------------------------------------------------------------------------------

def seed(kit, ok=(), blocked=()):
    out = kit.workdir / "drafts"
    for cid in ok:
        write_draft(out, kit.draft(contact_id=cid, to=f"{cid}@example.com", status="ok"))
    for cid in blocked:
        write_draft(out, kit.draft(contact_id=cid, to=f"{cid}@example.com", status="blocked"))
    return out


def present(out):
    return sorted(str(p.relative_to(out)) for p in out.rglob("*.md"))


def test_prune_stale_removes_the_drafts_of_ids_that_left_the_contacts(kit):
    out = seed(kit, ok=["a", "b", "c"], blocked=["d", "e"])
    removed = prune_stale(out, {"a", "b", "c", "d", "e"}, {"a", "d"})
    assert isinstance(removed, list) and all(isinstance(i, str) for i in removed)
    assert sorted(removed) == ["b", "c", "e"]
    assert present(out) == ["a.md", "blocked/d.md"]


def test_prune_stale_removes_a_stale_id_from_both_folders(kit):
    out = seed(kit, ok=["a"])
    # write_draft removes the copy in the other folder by design, so plant both files by hand
    (out / "blocked").mkdir(parents=True, exist_ok=True)
    (out / "gone.md").write_text(kit.md_text(to="gone@example.com", subject="s", body="ok copy"), encoding="utf-8")
    (out / "blocked" / "gone.md").write_text(
        kit.md_text(to="gone@example.com", subject="s", body="blocked copy", status="blocked"), encoding="utf-8")
    assert present(out) == ["a.md", "blocked/gone.md", "gone.md"]
    removed = prune_stale(out, {"a", "gone"}, {"a"})
    assert removed == ["gone"] or sorted(set(removed)) == ["gone"]
    assert present(out) == ["a.md"]


def test_prune_stale_does_nothing_when_nothing_left(kit):
    out = seed(kit, ok=["a", "b"], blocked=["c"])
    assert prune_stale(out, {"a", "b", "c"}, {"a", "b", "c"}) == []
    assert prune_stale(out, {"a", "b"}, {"a", "b", "c", "d"}) == []
    assert present(out) == ["a.md", "b.md", "blocked/c.md"]


def test_prune_stale_with_no_previous_ids_deletes_nothing(kit):
    out = seed(kit, ok=["a"], blocked=["b"])
    assert prune_stale(out, set(), {"a"}) == []
    assert prune_stale(out, set(), set()) == []
    assert present(out) == ["a.md", "blocked/b.md"]


def test_prune_stale_never_touches_a_file_whose_id_was_not_listed(kit):
    out = seed(kit, ok=["listed", "by-hand"], blocked=["also-by-hand"])
    removed = prune_stale(out, {"listed"}, set())
    assert removed == ["listed"]
    assert present(out) == ["blocked/also-by-hand.md", "by-hand.md"]


def test_prune_stale_leaves_the_index_other_extensions_and_folders_alone(kit):
    out = seed(kit, ok=["a"])
    (out / "index.jsonl").write_text('{"contact_id": "a"}\n', encoding="utf-8")
    (out / "a.txt").write_text("notes", encoding="utf-8")
    (out / "notes").mkdir()
    (out / "notes" / "a.md").write_text("nested", encoding="utf-8")
    assert prune_stale(out, {"a"}, set()) == ["a"]
    assert (out / "index.jsonl").exists() and (out / "a.txt").exists() and (out / "notes" / "a.md").exists()
    assert not (out / "a.md").exists()


def test_prune_stale_copes_with_an_id_whose_file_is_already_gone(kit):
    out = seed(kit, ok=["a"])
    removed = prune_stale(out, {"a", "never-written"}, set())
    assert "a" in removed and not (out / "a.md").exists()


def test_prune_stale_on_a_missing_directory_is_not_an_error(kit):
    assert prune_stale(kit.workdir / "no-such-dir", {"a"}, set()) in ([], ["a"])


HOSTILE_IDS = ["../victim", "..", "sub/inner", "a/../../victim", "..\\victim", "a\x00b", "/abs-victim"]


@pytest.mark.parametrize("hostile", HOSTILE_IDS, ids=[repr(h) for h in HOSTILE_IDS])
def test_prune_stale_never_deletes_outside_the_output_directory(kit, hostile):
    out = seed(kit, ok=["keep"])
    victim = kit.workdir / "victim.md"
    victim.write_text("precious", encoding="utf-8")
    nested = out / "sub"
    nested.mkdir()
    (nested / "inner.md").write_text("precious too", encoding="utf-8")
    absolute = kit.workdir / "abs-victim.md"
    absolute.write_text("precious three", encoding="utf-8")
    previous = {hostile, str(kit.workdir / "abs-victim"), str(kit.workdir / "victim")}
    removed = prune_stale(out, previous, set())
    assert victim.read_text(encoding="utf-8") == "precious"
    assert (nested / "inner.md").read_text(encoding="utf-8") == "precious too"
    assert absolute.read_text(encoding="utf-8") == "precious three"
    assert (out / "keep.md").exists(), "an id that was never listed stays"
    assert hostile not in removed


def test_prune_stale_still_removes_the_safe_ids_next_to_hostile_ones(kit):
    out = seed(kit, ok=["gone", "keep"])
    victim = kit.workdir / "victim.md"
    victim.write_text("precious", encoding="utf-8")
    removed = prune_stale(out, {"gone", "keep", "../victim"}, {"keep"})
    assert removed == ["gone"]
    assert victim.exists() and (out / "keep.md").exists()


# ----- read_drafts -----------------------------------------------------------------------------------------------------------

def test_read_drafts_returns_top_level_markdown_sorted_by_file_name(kit):
    out = kit.workdir / "drafts"
    for cid in ("carol", "alice", "bob"):
        write_draft(out, kit.draft(contact_id=cid, to=f"{cid}@example.com"))
    assert [d.contact_id for d in read_drafts(out)] == ["alice", "bob", "carol"]


def test_read_drafts_never_looks_into_blocked_or_at_other_files(kit):
    out = kit.workdir / "drafts"
    write_draft(out, kit.draft(contact_id="alice"))
    write_draft(out, kit.draft(contact_id="zed", to="zed@example.com", status="blocked"))
    (out / "index.jsonl").write_text('{"contact_id": "alice"}\n', encoding="utf-8")
    (out / "notes.txt").write_text("not a draft", encoding="utf-8")
    (out / "sub").mkdir()
    (out / "sub" / "nested.md").write_text(kit.md_text(to="n@example.com", subject="s", body="b"), encoding="utf-8")
    assert [d.contact_id for d in read_drafts(out)] == ["alice"]


def test_read_drafts_keeps_a_hand_placed_blocked_status_so_the_gate_can_refuse_it(kit):
    out = kit.workdir / "drafts"
    out.mkdir()
    (out / "mis.md").write_text(kit.md_text(to="m@example.com", subject="s", status="blocked", body="b"), encoding="utf-8")
    (draft,) = read_drafts(out)
    assert draft.status == "blocked"


def test_read_drafts_of_an_empty_directory_is_empty(kit):
    out = kit.workdir / "drafts"
    out.mkdir()
    assert read_drafts(out) == []


# ----- write_index ------------------------------------------------------------------------------------------------------------------

def test_index_has_one_json_object_per_draft_in_order(kit):
    out = kit.workdir / "drafts"
    drafts = [kit.draft(contact_id=c, to=f"{c}@example.com") for c in ("zed", "amy", "bob")]
    path = write_index(out, drafts)
    assert path == out / "index.jsonl"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and "\n\n" not in text
    rows = [json.loads(ln) for ln in text.splitlines()]
    assert [r["contact_id"] for r in rows] == ["zed", "amy", "bob"]


def test_index_rows_carry_every_draft_key_with_the_hook_flattened(kit):
    draft = kit.follow_up_draft(
        hook=kit.hook("a hook", provider="publik", model="publik-balanced"),
        issues=(LintIssue("word_count", "body is short", "warning"),),
    )
    (row,) = [json.loads(ln) for ln in write_index(kit.workdir / "drafts", [draft]).read_text(encoding="utf-8").splitlines()]
    for key in ("contact_id", "to", "subject", "body", "status", "lane", "in_reply_to", "references",
                "hook_text", "hook_provider", "hook_model", "issues"):
        assert key in row, key
    assert row["contact_id"] == "grace" and row["to"] == draft.to and row["body"] == draft.body
    assert row["lane"] == "follow_up"
    assert row["hook_text"] == "a hook" and row["hook_provider"] == "publik"
    assert row["hook_model"] == "publik-balanced"
    assert row["issues"] == [{"code": "word_count", "message": "body is short", "severity": "warning"}]
    assert not isinstance(row.get("hook"), dict)


def test_index_rows_for_drafts_without_a_hook_still_have_the_hook_keys(kit):
    (row,) = [json.loads(ln) for ln in write_index(kit.workdir / "d", [kit.draft(status="blocked")]).read_text(encoding="utf-8").splitlines()]
    for key in ("hook_text", "hook_provider", "hook_model", "issues"):
        assert key in row
    assert row["issues"] == []


def test_index_is_rewritten_not_appended(kit):
    out = kit.workdir / "drafts"
    write_index(out, [kit.draft(contact_id="a1"), kit.draft(contact_id="a2")])
    write_index(out, [kit.draft(contact_id="b1")])
    rows = [json.loads(ln) for ln in (out / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["contact_id"] for r in rows] == ["b1"]


def test_index_handles_unicode_and_embedded_newlines_in_one_line_each(kit):
    draft = kit.draft(body=kit.clean_body(first_name="Zo\u00eb"), subject="caf\u00e9")
    path = write_index(kit.workdir / "drafts", [draft, kit.draft(contact_id="bob", to="bob@example.com")])
    lines = path.read_text(encoding="utf-8").split("\n")
    assert len([ln for ln in lines if ln]) == 2
    assert json.loads(lines[0])["body"] == draft.body


# ----- 21.2 issue codes in the header are listed once ------------------------------------------------------------

def test_repeated_issue_codes_are_written_once_in_first_seen_order(kit):
    issues = issues_of(("filler", "error"), ("em_dash", "error"), ("filler", "error"), ("odd_greeting", "warning"),
                       ("em_dash", "error"), ("filler", "error"))
    path = write_draft(kit.workdir / "drafts", kit.draft(status="blocked", issues=issues))
    assert issues_line(path) == "filler; em_dash; odd_greeting"
    assert header_keys(path) == HEADER_KEYS


def test_a_code_repeated_with_a_different_severity_is_still_listed_once(kit):
    issues = (LintIssue("word_count", "too short", "warning"), LintIssue("word_count", "too long", "error"))
    path = write_draft(kit.workdir / "drafts", kit.draft(status="blocked", issues=issues))
    assert issues_line(path) == "word_count"


def test_codes_that_are_all_different_are_untouched_by_the_deduplication(kit):
    issues = issues_of(("zeta", "error"), ("alpha", "warning"), ("mid", "error"))
    assert issues_line(write_draft(kit.workdir / "drafts", kit.draft(issues=issues))) == "zeta; alpha; mid"


def test_the_index_keeps_every_distinct_issue_message_when_a_code_repeats(kit):
    issues = (LintIssue("ungrounded_claim", "token Initech is not in the facts", "error"),
              LintIssue("ungrounded_claim", "token Zorgon is not in the facts", "error"))
    (row,) = [json.loads(ln) for ln in write_index(kit.workdir / "drafts", [kit.draft(status="blocked", issues=issues)])
              .read_text(encoding="utf-8").splitlines()]
    messages = {i["message"] for i in row["issues"]}
    assert {"token Initech is not in the facts", "token Zorgon is not in the facts"} <= messages


# ----- 21.3 a markdown file without the header lines is not a draft ------------------------------------------------------------

NOT_A_DRAFT = re.compile(
    r"draft (\S+) is not a vcoutreach draft \(missing header line\(s\): ([^)]*)\); "
    r"move it out of the drafts folder or fix its header"
)


def missing_of(message):
    match = NOT_A_DRAFT.search(message)
    assert match, message
    return match.group(1), [t for t in re.split(r"[,\s]+", match.group(2)) if t]


def test_read_draft_on_a_plain_note_raises_the_not_a_draft_error(kit):
    path = kit.workdir / "notes.md"
    path.write_text("Remember to read every hook.\nThen send on Tuesday.\n", encoding="utf-8")
    with pytest.raises(OutreachError) as info:
        read_draft(path)
    name, missing = missing_of(str(info.value))
    assert name in ("notes.md", "notes")
    assert {"to", "subject"} <= set(missing), missing


@pytest.mark.parametrize("content", [b"", b"\n\n", b"Hi Ada,\n\nI liked your essay.\n\nSam\n", b"# Notes\n\n- one\n- two\n"],
                         ids=["zero-bytes", "blank-lines", "a-bare-body", "markdown-notes"])
def test_files_with_no_header_at_all_are_refused_with_the_same_message(kit, content):
    path = kit.workdir / "stray.md"
    path.write_bytes(content)
    with pytest.raises(OutreachError) as info:
        read_draft(path)
    name, missing = missing_of(str(info.value))
    assert name in ("stray.md", "stray") and "to" in missing


def test_a_draft_that_lost_only_its_to_line_names_exactly_that_line(kit):
    path = kit.workdir / "hand.md"
    path.write_text("subject: s\nstatus: ok\nlane: first_touch\nprovider: template\nin_reply_to:\nreferences:\nissues:\n\nHi,\n\nBody",
                    encoding="utf-8")
    with pytest.raises(OutreachError) as info:
        read_draft(path)
    _, missing = missing_of(str(info.value))
    assert "to" in missing and "subject" not in missing, missing


def test_a_draft_that_lost_only_its_subject_line_names_exactly_that_line(kit):
    path = kit.workdir / "hand.md"
    path.write_text("to: a@example.com\nstatus: ok\nlane: first_touch\nprovider: template\nin_reply_to:\nreferences:\nissues:\n\nHi,\n\nBody",
                    encoding="utf-8")
    with pytest.raises(OutreachError) as info:
        read_draft(path)
    _, missing = missing_of(str(info.value))
    assert "subject" in missing and "to" not in missing, missing


def test_read_drafts_fails_closed_on_a_stray_note_wherever_it_sorts(kit):
    out = kit.workdir / "drafts"
    for ident in ("bob", "dan"):
        write_draft(out, kit.draft(contact_id=ident, to=f"{ident}@example.com"))
    (out / "carl-notes.md").write_text("just a reminder to myself\n", encoding="utf-8")
    with pytest.raises(OutreachError) as info:
        read_drafts(out)
    name, _ = missing_of(str(info.value))
    assert name in ("carl-notes.md", "carl-notes")


def test_a_stray_note_in_the_blocked_folder_or_with_another_extension_is_not_read(kit):
    out = kit.workdir / "drafts"
    write_draft(out, kit.draft(contact_id="bob", to="bob@example.com"))
    (out / "blocked").mkdir(exist_ok=True)
    (out / "blocked" / "notes.md").write_text("not a draft\n", encoding="utf-8")
    (out / "notes.txt").write_text("not a draft\n", encoding="utf-8")
    assert [d.contact_id for d in read_drafts(out)] == ["bob"]


def test_a_complete_draft_is_never_called_a_stray_note(kit):
    path = write_draft(kit.workdir / "drafts", kit.draft())
    assert read_draft(path).to == "alice@example.com"
