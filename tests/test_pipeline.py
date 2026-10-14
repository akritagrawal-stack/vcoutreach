"""pipeline.draft_all end to end (SPEC sections 6, 7 and 17)."""
from __future__ import annotations

import importlib.resources as resources
import json
from pathlib import Path

import pytest

from vcoutreach.contacts import load_contacts
from vcoutreach.grounding import check_grounding
from vcoutreach.lint import lint_draft
from vcoutreach.pipeline import draft_all
from vcoutreach.providers import PublikProvider, TemplateProvider
from vcoutreach.sender import load_sender
from vcoutreach.types import (
    Draft,
    Hook,
    Lane,
    OutreachError,
    ProviderAuthError,
    ProviderCreditError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
)

EM = "\u2014"
EN = "\u2013"
KEY = "fk_test_0123456789abcdefZQ4W"

HOOKS = {
    "ada": "Your essay on how small teams ship weekly was the clearest take I have read, and I agree with the focus.",
    "grace": "Your talk on making software readable made me rethink how I name things, and I respect that.",
    "linus": "Your newsletter on shipping kernels calmly stayed with me, and I respect that discipline.",
}


def people(kit):
    return [
        kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                    facts=("your essay on how small teams ship weekly",)),
        kit.contact(id="grace", name="Grace Hopper", email="grace@compiler.example.org", org="Compiler Co",
                    facts=("your talk on making software readable",)),
        kit.contact(id="linus", name="Linus Torvalds", email="linus@kernel.example.org", org="Kernel Org",
                    facts=("your newsletter on shipping kernels calmly",)),
    ]


def codes(draft):
    return [i.code for i in draft.issues]


def publik(kit, ft, **env):
    settings = kit.publik_settings(PUBLIK_MAX_RETRIES="1", **env)
    return PublikProvider(settings, transport=ft, sleep=kit.sleeper(), rng=kit.rng(0.0))


def out_dir(kit):
    return kit.workdir / "drafts"


def run_template(kit, contacts, sender=None):
    out = out_dir(kit)
    sender = sender or kit.sender()
    return draft_all(contacts, sender, TemplateProvider(), out), out


def check_invariants(kit, drafts, out, contacts, sender):
    """What must hold for every pipeline run, whatever the model said."""
    by_id = {c.id: c for c in contacts}
    for d in drafts:
        ok_path = out / f"{d.contact_id}.md"
        blocked_path = out / "blocked" / f"{d.contact_id}.md"
        if d.status == "ok":
            assert ok_path.exists() and not blocked_path.exists(), d.contact_id
            text = ok_path.read_text(encoding="utf-8")
            assert EM not in text and EN not in text, "a unicode dash leaked into an ok draft"
            assert d.hook is not None
            errors = [i for i in lint_draft(d.subject, d.body, sender, by_id[d.contact_id], d.hook.text)
                      if i.severity == "error"]
            assert errors == [], errors
            assert check_grounding(d.hook.text, by_id[d.contact_id], sender) == []
        else:
            assert d.status == "blocked"
            assert blocked_path.exists() and not ok_path.exists(), d.contact_id
            assert any(i.severity == "error" for i in d.issues), "a blocked draft must say why"


# ----- template provider, end to end -----------------------------------------------------------------------------------------

def test_all_good_contacts_are_drafted_ok_in_input_order(kit):
    contacts = people(kit)
    drafts, out = run_template(kit, contacts)
    assert [d.contact_id for d in drafts] == ["ada", "grace", "linus"]
    assert [d.status for d in drafts] == ["ok"] * 3
    assert [p.name for p in kit.md_files(out)] == ["ada.md", "grace.md", "linus.md"]
    assert not (out / "blocked").exists() or list((out / "blocked").glob("*.md")) == []
    check_invariants(kit, drafts, out, contacts, kit.sender())


def test_returned_drafts_carry_recipient_subject_hook_and_lane(kit):
    contacts = people(kit)
    drafts, _ = run_template(kit, contacts)
    for contact, d in zip(contacts, drafts):
        assert isinstance(d, Draft)
        assert d.to == contact.email
        assert d.subject.lower() == f"a note for {contact.first_name}".lower()
        assert d.lane is Lane.FIRST_TOUCH
        assert d.hook is not None and d.hook.provider == "template" and d.hook.model == "none"
        assert d.hook.text in d.body
        assert d.issues == () or all(i.severity == "warning" for i in d.issues)


def test_shipped_sample_data_drafts_four_ok_and_one_blocked(kit):
    contacts_ref = resources.files("vcoutreach").joinpath("data", "examples", "contacts.sample.csv")
    sender_ref = resources.files("vcoutreach").joinpath("data", "examples", "sender.sample.json")
    with resources.as_file(contacts_ref) as c, resources.as_file(sender_ref) as s:
        contacts, sender = load_contacts(Path(c)), load_sender(Path(s))
    out = out_dir(kit)
    drafts = draft_all(contacts, sender, TemplateProvider(), out)
    assert len(drafts) == 5
    assert sum(d.status == "ok" for d in drafts) == 4
    (blocked,) = [d for d in drafts if d.status == "blocked"]
    assert "insufficient_facts" in codes(blocked)
    empty = [c for c in contacts if not c.facts]
    assert [c.id for c in empty] == [blocked.contact_id]
    (follow_up,) = [d for d in drafts if d.lane is Lane.FOLLOW_UP]
    assert follow_up.status == "ok"
    assert follow_up.in_reply_to == "<sample-thread-001@example.com>"
    assert follow_up.subject == next(c.thread_subject for c in contacts if c.lane is Lane.FOLLOW_UP)
    assert len(list(out.glob("*.md"))) == 4 and len(list((out / "blocked").glob("*.md"))) == 1
    check_invariants(kit, drafts, out, contacts, sender)


def test_contact_without_facts_and_template_provider_is_blocked_with_insufficient_facts(kit):
    contacts = people(kit)
    contacts[1] = kit.contact(id="grace", name="Grace Hopper", email="grace@compiler.example.org", facts=())
    drafts, out = run_template(kit, contacts)
    assert [d.status for d in drafts] == ["ok", "blocked", "ok"]
    assert "insufficient_facts" in codes(drafts[1])
    assert (out / "blocked" / "grace.md").exists() and not (out / "grace.md").exists()


def test_follow_up_contact_gets_threading_headers_and_the_thread_subject(kit):
    contact = kit.follow_up_contact()
    drafts, out = run_template(kit, [contact])
    (d,) = drafts
    assert d.status == "ok"
    assert d.lane is Lane.FOLLOW_UP and d.subject == "a note for Grace"
    assert d.in_reply_to == contact.in_reply_to and d.references == contact.references
    _, headers, body = kit.parse_md(out / "grace.md")
    assert headers["lane"] == "follow_up"
    assert headers["in_reply_to"] == contact.in_reply_to
    assert headers["subject"] == "a note for Grace"
    assert kit.paragraphs(body)[1].startswith("Following up in case you missed my previous email. ")


def test_follow_up_whose_subject_has_an_en_dash_is_blocked_not_rewritten(kit):
    contact = kit.follow_up_contact(thread_subject=f"Intro {EN} Lumenlist and Compiler Co")
    drafts, out = run_template(kit, [contact])
    (d,) = drafts
    assert d.status == "blocked" and "em_dash" in codes(d)
    assert d.subject == contact.thread_subject, "the subject must stay identical or threading breaks"
    assert (out / "blocked" / "grace.md").exists()


# ----- file formats ----------------------------------------------------------------------------------------------------------------

def test_markdown_header_has_the_documented_keys_in_order_then_one_blank_line_then_the_body(kit):
    contacts = people(kit)
    drafts, out = run_template(kit, contacts)
    for d in drafts:
        lines, headers, body = kit.parse_md(out / f"{d.contact_id}.md")
        assert [ln.partition(":")[0] for ln in lines] == [
            "to", "subject", "status", "lane", "provider", "in_reply_to", "references", "issues"]
        assert headers["to"] == d.to and headers["subject"] == d.subject
        assert headers["status"] == "ok" and headers["lane"] == "first_touch"
        assert headers["provider"] == "template"
        assert body.rstrip("\n") == d.body.rstrip("\n"), "the body must be written exactly as it would be sent"
        assert not body.startswith("\n"), "exactly one blank line between header and body"


def test_empty_threading_headers_are_the_bare_key(kit):
    _, out = run_template(kit, people(kit)[:1])
    lines = (out / "ada.md").read_text(encoding="utf-8").split("\n")
    assert "in_reply_to:" in lines and "references:" in lines


def first_seen(items):
    """De-duplicate, keeping the first-seen order (SPEC 21.2)."""
    return list(dict.fromkeys(items))


def header_codes(path):
    head = path.read_text(encoding="utf-8").split("\n\n", 1)[0]
    (line,) = [ln for ln in head.split("\n") if ln.startswith("issues:")]
    value = line.partition(":")[2].strip()
    return [c.strip() for c in value.split(";")] if value else []


def test_a_clean_draft_has_an_empty_issues_header(kit):
    _, out = run_template(kit, people(kit)[:1])
    lines = (out / "ada.md").read_text(encoding="utf-8").split("\n")
    assert "issues:" in lines
    assert header_codes(out / "ada.md") == []


def test_a_blocked_draft_lists_every_issue_code_errors_and_warnings_in_its_header(kit):
    contact = kit.contact(id="jay", name="J Smith", email="jay@example.com", facts=("your launch!",))
    (d,), out = run_template(kit, [contact])
    assert d.status == "blocked"
    listed = header_codes(out / "blocked" / "jay.md")
    assert {"exclamation", "odd_greeting"} <= set(listed)
    assert set(listed) == set(codes(d)), "the header must carry the code of every issue on the draft"


def test_warnings_on_an_ok_draft_are_visible_in_its_header_and_do_not_block_it(kit):
    contact = kit.contact(id="jay", name="J Smith", email="jay@example.com")
    (d,), out = run_template(kit, [contact])
    assert d.status == "ok" and (out / "jay.md").exists()
    assert "odd_greeting" in codes(d)
    assert header_codes(out / "jay.md") == first_seen(codes(d))
    assert all(i.severity == "warning" for i in d.issues)


def test_the_header_codes_match_the_index_codes(kit):
    contacts = [kit.contact(id="jay", name="J Smith", email="jay@example.com"), people(kit)[0]]
    drafts, out = run_template(kit, contacts)
    rows = {r["contact_id"]: r for r in kit.read_index(out)}
    for d in drafts:
        path = out / f"{d.contact_id}.md"
        assert header_codes(path) == first_seen([i["code"] for i in rows[d.contact_id]["issues"]])


def test_a_no_facts_blocked_draft_says_why_in_its_header(kit):
    contact = kit.contact(id="nofacts", name="No Facts", email="nofacts@example.com", facts=())
    (d,), out = run_template(kit, [contact])
    assert d.status == "blocked"
    assert "insufficient_facts" in header_codes(out / "blocked" / "nofacts.md")


def test_the_greeting_and_subject_use_the_cleaned_or_preferred_name(kit):
    contacts = [
        kit.contact(id="jane", name="Dr. Jane Roe", email="jane@example.com"),
        kit.contact(id="alice", name="Smith, Alice", email="alice@example.com"),
        kit.contact(id="alex", name="Alexander Hamilton", email="alex@example.com", preferred_name="Alex"),
    ]
    drafts, out = run_template(kit, contacts)
    by_id = {d.contact_id: d for d in drafts}
    assert by_id["jane"].body.startswith("Hi Jane,") and by_id["jane"].subject == "a note for Jane"
    assert by_id["alice"].body.startswith("Hi Alice,") and by_id["alice"].subject == "a note for Alice"
    assert by_id["alex"].body.startswith("Hi Alex,") and by_id["alex"].subject == "a note for Alex"
    assert all("odd_greeting" not in codes(d) for d in drafts)


def test_index_jsonl_has_one_row_per_draft_in_input_order_with_all_keys(kit):
    contacts = people(kit)
    contacts.append(kit.contact(id="nofacts", name="No Facts", email="nofacts@example.com", facts=()))
    drafts, out = run_template(kit, contacts)
    rows = kit.read_index(out)
    assert [r["contact_id"] for r in rows] == ["ada", "grace", "linus", "nofacts"]
    for row, d in zip(rows, drafts):
        for key in ("contact_id", "to", "subject", "body", "status", "lane", "in_reply_to", "references",
                    "hook_text", "hook_provider", "hook_model", "issues"):
            assert key in row, key
        assert row["status"] == d.status and row["to"] == d.to and row["body"] == d.body
        assert row["lane"] == "first_touch"
        assert isinstance(row["issues"], list)
        for issue in row["issues"]:
            assert set(issue) == {"code", "message", "severity"}
    assert rows[0]["hook_provider"] == "template" and rows[0]["hook_model"] == "none"
    assert rows[0]["hook_text"] == drafts[0].hook.text
    assert any(i["code"] == "insufficient_facts" for i in rows[3]["issues"])


def test_unicode_names_survive_into_files_and_index(kit):
    contacts = [kit.contact(id="zoe", name="Zo\u00eb \u00c5kesson", email="zoe@example.com",
                            facts=("your talk on caf\u00e9 culture",))]
    drafts, out = run_template(kit, contacts)
    text = (out / "zoe.md").read_bytes().decode("utf-8")
    assert "Hi Zo\u00eb," in text and "caf\u00e9 culture" in text
    assert kit.read_index(out)[0]["hook_text"].count("caf\u00e9") == 1


# ----- stale drafts (SPEC 20.3) -------------------------------------------------------------------------------------------

def five(kit):
    return [
        kit.contact(id=cid, name=f"Person {cid.upper()}", email=f"{cid}@example.com",
                    facts=("your essay on how small teams ship weekly",))
        for cid in ("amy", "bob", "cat", "dan", "eve")
    ]


def md_names(out):
    return sorted(str(p.relative_to(out)) for p in out.rglob("*.md"))


def rerun(kit, contacts, out, pruned=None, provider=None, sender=None):
    callback = pruned.append if pruned is not None else None
    return draft_all(contacts, sender or kit.sender(), provider or TemplateProvider(), out, on_prune=callback)


def test_a_contact_deleted_from_the_input_loses_its_draft_on_the_next_run(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    assert md_names(out) == ["amy.md", "bob.md", "cat.md", "dan.md", "eve.md"]
    pruned = []
    drafts = rerun(kit, [c for c in five(kit) if c.id != "cat"], out, pruned)
    assert md_names(out) == ["amy.md", "bob.md", "dan.md", "eve.md"]
    assert pruned == ["cat"]
    assert [d.contact_id for d in drafts] == ["amy", "bob", "dan", "eve"]
    assert [r["contact_id"] for r in kit.read_index(out)] == ["amy", "bob", "dan", "eve"]


def test_a_stale_blocked_draft_is_removed_from_the_blocked_folder(kit):
    out = out_dir(kit)
    contacts = five(kit)
    contacts[2] = kit.contact(id="cat", name="Person CAT", email="cat@example.com", facts=())
    rerun(kit, contacts, out)
    assert "blocked/cat.md" in md_names(out)
    pruned = []
    rerun(kit, [c for c in contacts if c.id != "cat"], out, pruned)
    assert md_names(out) == ["amy.md", "bob.md", "dan.md", "eve.md"]
    assert pruned == ["cat"]


def test_several_removed_contacts_are_each_reported_once(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    pruned = []
    rerun(kit, [c for c in five(kit) if c.id in ("amy", "eve")], out, pruned)
    assert sorted(pruned) == ["bob", "cat", "dan"]
    assert md_names(out) == ["amy.md", "eve.md"]


def test_the_callback_runs_after_the_file_is_gone(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    seen = []

    def on_prune(contact_id):
        seen.append((contact_id, (out / f"{contact_id}.md").exists(), (out / "blocked" / f"{contact_id}.md").exists()))

    draft_all([c for c in five(kit) if c.id != "bob"], kit.sender(), TemplateProvider(), out, on_prune=on_prune)
    assert seen == [("bob", False, False)]


def test_an_unchanged_rerun_prunes_nothing(kit):
    out = out_dir(kit)
    pruned = []
    rerun(kit, five(kit), out, pruned)
    rerun(kit, five(kit), out, pruned)
    assert pruned == [] and len(md_names(out)) == 5


def test_a_contact_that_moves_from_ok_to_blocked_is_not_stale(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    contacts = five(kit)
    contacts[1] = kit.contact(id="bob", name="Person BOB", email="bob@example.com", facts=())
    pruned = []
    rerun(kit, contacts, out, pruned)
    assert pruned == []
    assert md_names(out) == ["amy.md", "blocked/bob.md", "cat.md", "dan.md", "eve.md"]


def test_a_removed_contact_that_comes_back_gets_a_fresh_draft(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    rerun(kit, [c for c in five(kit) if c.id != "dan"], out)
    assert "dan.md" not in md_names(out)
    rerun(kit, five(kit), out)
    assert "dan.md" in md_names(out)


def test_hand_written_files_that_were_never_in_the_index_are_not_deleted(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    (out / "blocked").mkdir(parents=True, exist_ok=True)
    (out / "hand-made.md").write_text(kit.md_text(to="h@example.com", subject="s", body="mine"), encoding="utf-8")
    (out / "blocked" / "hand-blocked.md").write_text(kit.md_text(to="h2@example.com", subject="s", body="mine too"), encoding="utf-8")
    (out / "README.txt").write_text("notes", encoding="utf-8")
    pruned = []
    rerun(kit, [c for c in five(kit) if c.id != "amy"], out, pruned)
    assert pruned == ["amy"]
    assert (out / "hand-made.md").exists() and (out / "blocked" / "hand-blocked.md").exists() and (out / "README.txt").exists()


def test_a_provider_abort_in_the_middle_of_a_run_prunes_nothing(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    calls = []

    def behavior(request):
        calls.append(request.contact.id)
        if len(calls) == 2:
            raise ProviderAuthError("the key was rejected")
        return kit.hook(kit.HOOK, provider="stub")

    pruned = []
    with pytest.raises(ProviderAuthError):
        rerun(kit, [c for c in five(kit) if c.id != "eve"], out, pruned, provider=kit.stub_provider(behavior))
    assert pruned == []
    assert "eve.md" in md_names(out), "an aborted run must not delete anything"
    assert len(md_names(out)) == 5


def test_a_provider_abort_on_the_first_contact_prunes_nothing(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)

    def behavior(request):
        raise ProviderCreditError("out of credit", "https://example.com/top-up")

    pruned = []
    with pytest.raises(ProviderCreditError):
        rerun(kit, five(kit)[:2], out, pruned, provider=kit.stub_provider(behavior))
    assert pruned == [] and len(md_names(out)) == 5


def test_without_a_previous_index_nothing_is_pruned_and_nothing_crashes(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    (out / "index.jsonl").unlink()
    pruned = []
    rerun(kit, [c for c in five(kit) if c.id != "bob"], out, pruned)
    assert pruned == []
    assert "bob.md" in md_names(out), "without an index nobody can say the file was ours"


GARBAGE_INDEXES = {
    "not-json": b"this is not json at all\n",
    "binary": b"\x00\xff\xfe\x01\x02",
    "empty": b"",
    "blank-lines": b"\n\n   \n",
    "json-array": b"[1, 2, 3]\n",
    "json-string": b'"amy"\n',
    "no-contact-id": b'{"to": "a@example.com"}\n{"status": "ok"}\n',
    "odd-id-types": b'{"contact_id": 5}\n{"contact_id": null}\n{"contact_id": ["bob"]}\n{"contact_id": {"x": 1}}\n',
    "truncated": b'{"contact_id": "bo',
}


@pytest.mark.parametrize("name", list(GARBAGE_INDEXES), ids=list(GARBAGE_INDEXES))
def test_a_garbage_index_prunes_nothing_and_does_not_crash(kit, name):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    (out / "index.jsonl").write_bytes(GARBAGE_INDEXES[name])
    pruned = []
    drafts = rerun(kit, [c for c in five(kit) if c.id != "bob"], out, pruned)
    assert pruned == []
    assert len(drafts) == 4
    assert "bob.md" in md_names(out)
    assert [r["contact_id"] for r in kit.read_index(out)] == ["amy", "cat", "dan", "eve"], "the run still writes a fresh index"


def test_a_tampered_index_cannot_make_the_run_delete_outside_the_output_directory(kit):
    out = out_dir(kit)
    rerun(kit, five(kit), out)
    victim = kit.workdir / "victim.md"
    victim.write_text("precious", encoding="utf-8")
    absolute = kit.workdir / "abs-victim.md"
    absolute.write_text("precious too", encoding="utf-8")
    nested = out / "sub"
    nested.mkdir()
    (nested / "inner.md").write_text("precious three", encoding="utf-8")
    lines = [{"contact_id": cid} for cid in
             ("../victim", "sub/inner", str(kit.workdir / "abs-victim"), "..\\victim", "..")]
    lines.append({"contact_id": "bob"})
    (out / "index.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    pruned = []
    rerun(kit, [c for c in five(kit) if c.id != "bob"], out, pruned)
    assert victim.read_text(encoding="utf-8") == "precious"
    assert absolute.read_text(encoding="utf-8") == "precious too"
    assert (nested / "inner.md").read_text(encoding="utf-8") == "precious three"
    assert "bob" in pruned or "bob.md" not in md_names(out), "the one honest stale id is still handled"
    assert not [p for p in pruned if p != "bob"]


def test_only_ids_from_the_previous_index_are_candidates(kit):
    out = out_dir(kit)
    rerun(kit, five(kit)[:3], out)
    (out / "dan.md").write_text(kit.md_text(to="dan@example.com", subject="s", body="by hand"), encoding="utf-8")
    pruned = []
    rerun(kit, five(kit)[:2], out, pruned)
    assert pruned == ["cat"]
    assert "dan.md" in md_names(out)


# ----- reruns ---------------------------------------------------------------------------------------------------------------------------

def test_rerun_overwrites_files_and_does_not_duplicate_index_rows(kit):
    contacts = people(kit)
    run_template(kit, contacts, sender=kit.sender(signature="Sam Rivera\nVersion One"))
    drafts, out = run_template(kit, contacts, sender=kit.sender(signature="Sam Rivera\nVersion Two"))
    assert len(kit.read_index(out)) == 3
    assert [p.name for p in kit.md_files(out)] == ["ada.md", "grace.md", "linus.md"]
    for d in drafts:
        text = (out / f"{d.contact_id}.md").read_text(encoding="utf-8")
        assert "Version Two" in text and "Version One" not in text


def test_a_contact_that_turns_from_ok_to_blocked_leaves_no_stale_ok_file(kit):
    contact = people(kit)[0]
    run_template(kit, [contact])
    assert (out_dir(kit) / "ada.md").exists()
    blocked_now = kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com", facts=())
    drafts, out = run_template(kit, [blocked_now])
    assert drafts[0].status == "blocked"
    assert (out / "blocked" / "ada.md").exists()
    assert not (out / "ada.md").exists(), "a stale ok draft would still be sendable"


def test_a_contact_that_turns_from_blocked_to_ok_leaves_no_stale_blocked_file(kit):
    blocked_first = kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com", facts=())
    run_template(kit, [blocked_first])
    assert (out_dir(kit) / "blocked" / "ada.md").exists()
    drafts, out = run_template(kit, [people(kit)[0]])
    assert drafts[0].status == "ok"
    assert (out / "ada.md").exists() and not (out / "blocked" / "ada.md").exists()


def test_output_directory_is_created_when_missing(kit):
    out = kit.workdir / "deep" / "er" / "drafts"
    draft_all(people(kit)[:1], kit.sender(), TemplateProvider(), out)
    assert (out / "ada.md").exists() and (out / "index.jsonl").exists()


@pytest.mark.parametrize("bad_id", ["../escape", "..\\escape", "sub/dir/escape", "ABS"])
def test_a_hostile_contact_id_cannot_write_outside_the_output_directory(kit, bad_id):
    out = out_dir(kit)
    if bad_id == "ABS":
        bad_id = str(kit.tmp_path / "abs-target")
    contact = kit.contact(id=bad_id, name="Eve Hostile", email="eve@example.com", facts=("your essay on small teams",))

    def outside():
        return {p for p in kit.tmp_path.rglob("*") if p != out and out not in p.parents and p != out.parent}

    before = outside()
    try:
        draft_all([contact], kit.sender(), TemplateProvider(), out)
    except OutreachError:
        pass
    new = outside() - before
    assert not {p for p in new if p.is_file()}, f"files escaped the output dir: {sorted(new)}"


# ----- publik provider through a fake transport -------------------------------------------------------------------------------------------

def test_publik_hooks_become_ok_drafts_and_each_contact_gets_one_request(kit):
    contacts = people(kit)
    ft = kit.fake_transport(*[kit.chat_ok(HOOKS[c.id], headers={"x-publik-model": "publik-balanced"}) for c in contacts])
    drafts = draft_all(contacts, kit.sender(), publik(kit, ft), out_dir(kit))
    assert [d.status for d in drafts] == ["ok"] * 3
    assert len(ft.requests) == 3
    for contact, req in zip(contacts, ft.requests):
        assert contact.name in req.json()["messages"][1]["content"], "requests must follow file order"
    assert drafts[0].hook.provider == "publik" and drafts[0].hook.text == HOOKS["ada"]
    check_invariants(kit, drafts, out_dir(kit), contacts, kit.sender())


def test_publik_with_no_facts_blocks_the_row_without_calling_the_model(kit):
    contacts = people(kit)
    contacts[1] = kit.contact(id="grace", name="Grace Hopper", email="grace@compiler.example.org", facts=())
    ft = kit.fake_transport(kit.chat_ok(HOOKS["ada"]), kit.chat_ok(HOOKS["linus"]))
    drafts = draft_all(contacts, kit.sender(), publik(kit, ft), out_dir(kit))
    assert [d.status for d in drafts] == ["ok", "blocked", "ok"]
    assert "no_facts" in codes(drafts[1])
    assert len(ft.requests) == 2, "a contact without facts must never cost an API call"
    assert (out_dir(kit) / "blocked" / "grace.md").exists()


def test_a_no_hook_answer_blocks_that_row_only_and_the_run_continues(kit):
    contacts = people(kit)
    ft = kit.fake_transport(kit.chat_ok(HOOKS["ada"]), kit.chat_ok("NO_HOOK"), kit.chat_ok(HOOKS["linus"]))
    drafts = draft_all(contacts, kit.sender(), publik(kit, ft), out_dir(kit))
    assert [d.status for d in drafts] == ["ok", "blocked", "ok"]
    assert "insufficient_facts" in codes(drafts[1])
    assert len(ft.requests) == 3
    check_invariants(kit, drafts, out_dir(kit), contacts, kit.sender())


ABORTS = [
    pytest.param(lambda kit: [kit.response(401, body=b"nope")], ProviderAuthError, 2, id="revoked-key"),
    pytest.param(lambda kit: [kit.error_response(402, "Out of credit", top_up_url="https://x.example/top")], ProviderCreditError, 2, id="no-credit"),
    pytest.param(lambda kit: [kit.response(429), kit.response(429)], ProviderRateLimitError, 3, id="rate-limited"),
    pytest.param(lambda kit: [kit.response(503), kit.response(503)], ProviderTransportError, 3, id="gateway-down"),
    pytest.param(lambda kit: [kit.failure("refused"), kit.failure("refused")], ProviderTransportError, 3, id="offline"),
    pytest.param(lambda kit: [kit.chat_ok("")], ProviderResponseError, 2, id="empty-answer"),
    pytest.param(lambda kit: [kit.error_response(400, "bad request")], ProviderError, 2, id="bad-request"),
]


@pytest.mark.parametrize("script,expected,total_requests", ABORTS)
def test_provider_errors_other_than_no_hook_abort_the_whole_run(kit, script, expected, total_requests):
    contacts = people(kit)
    ft = kit.fake_transport(kit.chat_ok(HOOKS["ada"]), *script(kit), kit.chat_ok(HOOKS["linus"]), kit.chat_ok(HOOKS["linus"]))
    out = out_dir(kit)
    with pytest.raises(expected):
        draft_all(contacts, kit.sender(), publik(kit, ft), out)
    assert len(ft.requests) == total_requests, "the run must stop at the first fatal provider error"
    assert not (out / "grace.md").exists() and not (out / "blocked" / "grace.md").exists()
    assert not (out / "linus.md").exists() and not (out / "blocked" / "linus.md").exists()


def test_the_api_key_never_ends_up_in_any_output_file(kit):
    contacts = people(kit)
    ft = kit.fake_transport(kit.chat_ok(HOOKS["ada"]), kit.chat_ok("NO_HOOK"), kit.chat_ok(HOOKS["linus"]))
    draft_all(contacts, kit.sender(), publik(kit, ft), out_dir(kit))
    for path in out_dir(kit).rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8"), path


# ----- hostile model output ----------------------------------------------------------------------------------------------------------------

def one_contact(kit):
    return [kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                        facts=("your essay on small teams",))]


def run_with_hook(kit, hook_text, contacts=None):
    contacts = contacts or one_contact(kit)
    ft = kit.fake_transport(kit.chat_ok(hook_text))
    drafts = draft_all(contacts, kit.sender(), publik(kit, ft), out_dir(kit))
    check_invariants(kit, drafts, out_dir(kit), contacts, kit.sender())
    return drafts[0]


def test_a_hook_that_invents_a_company_is_blocked_by_grounding(kit):
    d = run_with_hook(kit, "I noticed you left Initech to build your own fund, and I respect that.")
    assert d.status == "blocked"
    ungrounded = [i for i in d.issues if i.code == "ungrounded_claim"]
    assert ungrounded and any("Initech" in i.message for i in ungrounded)
    assert (out_dir(kit) / "blocked" / "ada.md").exists()
    assert not (out_dir(kit) / "ada.md").exists()


def test_a_hook_with_an_em_dash_is_blocked(kit):
    d = run_with_hook(kit, f"Your essay {EM} on small teams, stayed with me, and I respect that.")
    assert d.status == "blocked" and "em_dash" in codes(d)


def test_a_hook_with_an_en_dash_is_blocked(kit):
    d = run_with_hook(kit, f"Your essay {EN} on small teams, stayed with me, and I respect that.")
    assert d.status == "blocked" and "em_dash" in codes(d)


def test_a_false_parallel_hook_is_blocked(kit):
    d = run_with_hook(kit, "Your essay on small teams is exactly what we are building, and I respect that.")
    assert d.status == "blocked" and "false_parallel" in codes(d)


def test_the_women_founded_funding_style_parallel_is_blocked(kit):
    contact = kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                          facts=("your writing on funding for women-founded startups",))
    d = run_with_hook(kit, "Your writing on funding for women-founded startups, which is close to what we are doing, stayed with me.", [contact])
    assert d.status == "blocked" and "false_parallel" in codes(d)


def test_a_number_the_model_made_up_is_blocked(kit):
    d = run_with_hook(kit, "Your essay on small teams reached 40 readers, and I respect that.")
    assert d.status == "blocked" and "unapproved_number" in codes(d)


def test_a_hook_with_an_exclamation_mark_is_blocked(kit):
    d = run_with_hook(kit, "Your essay on small teams was great!")
    assert d.status == "blocked" and "exclamation" in codes(d)


def test_a_filler_hook_is_blocked(kit):
    d = run_with_hook(kit, "Just circling back on your essay about small teams, and I respect it.")
    assert d.status == "blocked" and "filler" in codes(d)


def test_a_hook_containing_a_forbidden_phrase_is_blocked(kit):
    d = run_with_hook(kit, "Your essay on small teams has real synergy, and I respect it.")
    assert d.status == "blocked" and "forbidden_phrase" in codes(d)


def test_a_multi_line_hook_never_yields_an_ok_draft_with_hard_wraps(kit):
    d = run_with_hook(kit, "Your essay on small teams\nstayed with me, and I respect it.")
    if d.status == "ok":
        assert all("\n" not in p for p in kit.paragraphs(d.body)[:-1])
    else:
        assert "hard_wrap" in codes(d)


def test_template_like_placeholders_in_a_hook_are_not_expanded(kit):
    d = run_with_hook(kit, "Your essay on small teams and the {signature} line stayed with me, and I respect it.")
    if d.status == "ok":
        assert d.body.count(kit.sender().signature) == 1
        assert "{signature}" in d.body


def test_surrounding_whitespace_in_the_model_answer_never_reaches_the_draft(kit):
    d = run_with_hook(kit, "\n\n   Your essay on small teams stayed with me, and I respect it.   \n")
    assert d.status == "ok"
    assert d.hook.text == "Your essay on small teams stayed with me, and I respect it."


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_provider_that_returns_a_blank_hook_never_produces_an_ok_draft(kit, blank):
    provider = kit.stub_provider(lambda request: Hook(text=blank, provider="stub", model="none"))
    drafts = draft_all(one_contact(kit), kit.sender(), provider, out_dir(kit))
    assert drafts[0].status == "blocked"
    assert not (out_dir(kit) / "ada.md").exists()
    if blank:
        assert "empty_hook" in codes(drafts[0])


# ----- contact data that fights back ----------------------------------------------------------------------------------------------------------

def test_an_em_dash_inside_a_contact_fact_never_leaks_into_an_ok_draft(kit):
    contact = kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                          facts=(f"your essay {EM} on how small teams ship weekly",))
    drafts, out = run_template(kit, [contact])
    check_invariants(kit, drafts, out, [contact], kit.sender())
    if drafts[0].status == "blocked":
        assert "em_dash" in codes(drafts[0])


def test_an_em_dash_in_a_fact_echoed_by_the_model_never_leaks_either(kit):
    contact = kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                          facts=(f"your essay {EM} on small teams",))
    d = run_with_hook(kit, f"I read your essay {EM} on small teams, and I respect it.", [contact])
    assert d.status == "blocked"


def test_an_en_dash_in_a_year_range_fact_is_handled_the_same_way(kit):
    contact = kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                          facts=(f"you wrote weekly from 2019{EN}2021",))
    drafts, out = run_template(kit, [contact])
    check_invariants(kit, drafts, out, [contact], kit.sender())


def test_a_fact_with_a_trailing_period_still_reads_naturally_in_the_hook(kit):
    contact = kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                          facts=("your essay on how small teams ship weekly.",))
    drafts, _ = run_template(kit, [contact])
    assert drafts[0].status == "ok"
    assert ".," not in drafts[0].body and ".." not in drafts[0].body


def test_a_short_body_is_still_ok_when_only_a_word_count_warning_is_raised(kit):
    sender = kit.sender(product_lines=[], traction_lines=[], ask_line="")
    drafts, out = run_template(kit, people(kit)[:1], sender=sender)
    (d,) = drafts
    assert d.status == "ok"
    warnings = [i for i in d.issues if i.code == "word_count"]
    assert warnings and warnings[0].severity == "warning"
    assert (out / "ada.md").exists()
    row = kit.read_index(out)[0]
    assert any(i["code"] == "word_count" and i["severity"] == "warning" for i in row["issues"])


def test_a_sender_forbidden_phrase_blocks_every_draft(kit):
    sender = kit.sender(forbidden_phrases=["interesting"])
    drafts, out = run_template(kit, people(kit), sender=sender)
    assert [d.status for d in drafts] == ["blocked"] * 3
    assert all("forbidden_phrase" in codes(d) for d in drafts)
    assert list(out.glob("*.md")) == [] and len(list((out / "blocked").glob("*.md"))) == 3


def test_non_template_providers_never_get_a_contact_without_facts(kit):
    calls = []

    def behavior(request):
        calls.append(request.contact.id)
        return Hook(text=HOOKS["ada"], provider="stub", model="none")

    contacts = [kit.contact(id="nofacts", name="No Facts", email="nofacts@example.com", facts=())]
    drafts = draft_all(contacts, kit.sender(), kit.stub_provider(behavior), out_dir(kit))
    assert calls == []
    assert drafts[0].status == "blocked" and "no_facts" in codes(drafts[0])


def test_the_run_never_touches_the_network_with_the_template_provider(kit, network_guard):
    run_template(kit, people(kit))
    assert network_guard.attempts == []


def test_draft_files_are_plain_utf8_json_lines_without_trailing_garbage(kit):
    _, out = run_template(kit, people(kit))
    raw = (out / "index.jsonl").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    for line in raw.splitlines():
        json.loads(line)


# ----- SPEC 21: header codes, greetings and template facts ------------------------------------------------------------------

NOUN_PHRASE_HINT = "noun phrases"


def test_a_hook_with_several_ungrounded_names_lists_the_code_once_in_the_header(kit):
    d = run_with_hook(kit, "I met Initech, Zorgon and Blorpco at your talk, and I respect it.")
    assert d.status == "blocked" and "ungrounded_claim" in codes(d)
    listed = header_codes(out_dir(kit) / "blocked" / "ada.md")
    assert listed == first_seen(codes(d))
    assert listed.count("ungrounded_claim") == 1


def test_a_blocked_draft_header_follows_the_first_seen_order_of_its_issues(kit):
    contact = kit.contact(id="jay", name="J Smith", email="jay@example.com", facts=("your launch!",))
    (d,), out = run_template(kit, [contact])
    assert d.status == "blocked"
    assert header_codes(out / "blocked" / "jay.md") == first_seen(codes(d))


def test_a_degree_after_the_comma_does_not_become_the_greeting(kit):
    contacts = [
        kit.contact(id="alice", name="Alice Brown, PhD", email="alice@example.com"),
        kit.contact(id="taylor", name="Smith, Taylor", email="taylor@example.com"),
        kit.contact(id="john", name="John Smith, Jr.", email="john@example.com"),
    ]
    drafts, out = run_template(kit, contacts)
    by_id = {d.contact_id: d for d in drafts}
    assert by_id["alice"].body.startswith("Hi Alice,") and by_id["alice"].subject == "a note for Alice"
    assert by_id["taylor"].body.startswith("Hi Taylor,") and by_id["taylor"].subject == "a note for Taylor"
    assert by_id["john"].body.startswith("Hi John,") and by_id["john"].subject == "a note for John"
    assert all(d.status == "ok" and "odd_greeting" not in codes(d) for d in drafts)


def test_a_team_style_name_is_drafted_with_an_odd_greeting_warning(kit):
    contact = kit.contact(id="team", name="The Smith Team", email="team@example.com")
    (d,), out = run_template(kit, [contact])
    assert d.status == "ok" and (out / "team.md").exists()
    assert "odd_greeting" in codes(d)
    assert all(i.severity == "warning" for i in d.issues)
    assert "odd_greeting" in header_codes(out / "team.md")


def test_a_sentence_style_fact_blocks_the_row_with_the_noun_phrase_message(kit):
    contact = kit.contact(id="bob", name="Bob Byte", email="bob@example.com", facts=("You wrote a great essay on queues",))
    (d,), out = run_template(kit, [contact])
    assert d.status == "blocked"
    found = [i for i in d.issues if i.code == "insufficient_facts"]
    assert found and all(NOUN_PHRASE_HINT in i.message for i in found), d.issues
    assert (out / "blocked" / "bob.md").exists() and not (out / "bob.md").exists()
    rows = {r["contact_id"]: r for r in kit.read_index(out)}
    assert any(NOUN_PHRASE_HINT in i["message"] for i in rows["bob"]["issues"] if i["code"] == "insufficient_facts")


def test_a_fact_starting_with_a_capital_your_gets_a_lower_case_your_in_the_hook(kit):
    contact = kit.contact(id="bob", name="Bob Byte", email="bob@example.com", facts=("Your essay on queue design.",))
    (d,), out = run_template(kit, [contact])
    assert d.status == "ok"
    assert d.hook.text == "I came across your essay on queue design, and I found it interesting."
    assert d.hook.text in d.body
