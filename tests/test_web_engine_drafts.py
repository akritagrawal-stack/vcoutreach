"""The engine's drafts inside the web app: list, write with a provider, editor, lint, file safety.

SPEC_V3 section 2 ("Added on top"), SPEC.md sections 3 and 5. Drafts are the engine's own files in
<workspace>/drafts; the oracle for every lint expectation is the engine itself.
No dash characters appear in this file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, EM_DASH, GRACE, LINUS, MINA, Web, body, contact_ref, engine_draft_all, export_rows, page_text,
    read_draft_file, run_cli, seed_contacts, write_env,
)

ROWS = [ADA, GRACE, LINUS, MINA]
PROVIDERS = ["auto", "template", "publik", "openai", "anthropic"]


@pytest.fixture
def dw(make_web):
    """A workspace with a sender profile and four contacts (one thin), no drafts yet."""
    w = make_web(sender=True)
    seed_contacts(w, ROWS)
    return w


def write_all(w: Web, **extra):
    return w.post(w.url("drafts_write"), {"provider": "template", **extra}, follow=True)


def first_pos(text: str, *needles: str) -> int:
    found = [text.find(n) for n in needles if n and n in text]
    return min(found) if found else -1


def engine_errors(w: Web, subject: str, text: str):
    from vcoutreach.lint import lint_draft
    from vcoutreach.sender import load_sender

    sender = load_sender(w.ws / "sender.json")
    return [i for i in lint_draft(subject, text, sender, None) if i.severity == "error"]


KEYWORDS = {"em_dash": "dash", "exclamation": "exclamation", "forbidden_phrase": "forbidden", "hard_wrap": "line break"}


def shows_issue(text: str, issue) -> bool:
    low = text.lower()
    return issue.code in low or issue.message.lower() in low or KEYWORDS.get(issue.code, "\0") in low


# ---------------------------------------------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------------------------------------------

def test_the_drafts_page_is_empty_friendly_and_offers_every_provider(web):
    r = web.get("/crm/drafts")
    assert r.status_code == 200
    select = re.search(r'(?is)<select\b[^>]*name="provider"[^>]*>(.*?)</select>', body(r))
    assert select, "the Drafts page needs a provider select"
    values = re.findall(r'value="([^"]*)"', select.group(1))
    assert values == PROVIDERS


def test_writing_drafts_with_the_template_provider_creates_the_engine_files(dw):
    r = write_all(dw)
    assert r.status_code == 200
    drafts = dw.ws / "drafts"
    assert sorted(p.stem for p in drafts.glob("*.md")) == ["ada", "grace", "linus"]
    assert (drafts / "blocked" / "mina.md").is_file()
    ids = [json.loads(line)["contact_id"] for line in (drafts / "index.jsonl").read_text().splitlines()]
    assert sorted(ids) == ["ada", "grace", "linus", "mina"]
    assert dw.transport.calls == [], "writing drafts must never send"


def test_web_drafts_are_byte_identical_to_the_command_line_drafts(dw, tmp_path):
    write_all(dw)
    exported = tmp_path / "exported.csv"
    exported.write_text(body(dw.get(dw.url("contact_export"))), encoding="utf-8")
    out = tmp_path / "cli-out"
    result = run_cli(["draft", "--contacts", str(exported), "--sender", str(dw.ws / "sender.json"),
                      "--offline", "--out", str(out)], cwd=Path.cwd(), home=Path(os.environ["HOME"]))
    assert result.returncode == 0, result.stderr
    for slug, folder in (("ada", ""), ("grace", ""), ("linus", ""), ("mina", "blocked")):
        mine = (dw.ws / "drafts" / folder / f"{slug}.md").read_bytes()
        theirs = (out / folder / f"{slug}.md").read_bytes()
        assert mine == theirs, slug


def test_the_list_shows_ok_drafts_first_then_blocked_with_subject_issue_chips_and_word_count(dw):
    write_all(dw)
    text = dw.text("/crm/drafts")
    ok_at = max(first_pos(text, "Ada Lovelace", "ada@example.com"), first_pos(text, "Linus Pauling", "linus@example.com"))
    blocked_at = first_pos(text, "Mina Park", "mina@example.com")
    assert 0 <= ok_at < blocked_at, "ok drafts must come before blocked ones"
    assert "a note for Ada" in text
    # the thin contact's row carries an issue chip about the missing facts: the engine code, its plain-English
    # wording ("insufficient facts") or the engine message ("Add verified contact facts before drafting.")
    mina_row = text[blocked_at:]
    assert re.search(r"(?i)insufficient[_ ]facts|contact facts", mina_row), mina_row[:300]
    # The word count is on the contact's own row. "Words" is not defined by the specs (SPEC_QUESTIONS.md Q6): either
    # the engine's count before the signature (what its lint chip uses) or the whole body is accepted.
    ada_at = first_pos(text, "Ada Lovelace", "ada@example.com")
    ada_row = text[ada_at:first_pos(text[ada_at + 1:], "Grace Hopper", "grace@example.com") + ada_at + 1]
    body_text = read_draft_file(dw.ws, "ada").body.strip()
    before_signature = sum(len(p.split()) for p in body_text.split("\n\n")[:-1])
    whole = len(body_text.split())
    assert re.search(rf"(?<!\d)({before_signature}|{whole})(?!\d)", ada_row), (before_signature, whole, ada_row)


def test_the_status_and_search_filters_narrow_the_draft_list(dw):
    write_all(dw)
    ok = dw.text("/crm/drafts", query_string={"status": "ok"})
    assert "Ada Lovelace" in ok and "Grace Hopper" in ok and "Mina Park" not in ok
    blocked = dw.text("/crm/drafts", query_string={"status": "blocked"})
    assert "Mina Park" in blocked and "Ada Lovelace" not in blocked
    searched = dw.text("/crm/drafts", query_string={"q": "grace"})
    assert "Grace Hopper" in searched and "Ada Lovelace" not in searched


def test_the_provider_status_line_says_a_template_is_used_without_a_key_and_publik_with_one(make_web):
    plain = make_web(sender=True)
    text = plain.text("/crm/drafts").lower()
    assert "template" in text and "placeholder" in text
    keyed = make_web({"PUBLIK_API_KEY": "fake-key-for-the-test"}, sender=True)
    assert "publik" in keyed.text("/crm/drafts").lower()


# ---------------------------------------------------------------------------------------------------------------
# Writing: scope, overwrite, refusals
# ---------------------------------------------------------------------------------------------------------------

def test_writing_for_selected_contacts_only_writes_those(dw):
    names = ("slugs", "slug", "selected", "contacts", "contact_slugs", "contact")
    dw.post(dw.url("drafts_write"), {"provider": "template", **{n: ["ada"] for n in names}}, follow=True)
    assert sorted(p.stem for p in (dw.ws / "drafts").glob("*.md")) == ["ada"]


def test_existing_drafts_are_kept_unless_overwrite_is_asked_for(dw):
    write_all(dw)
    path = dw.ws / "drafts" / "ada.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nHAND-EDIT-MARKER\n", encoding="utf-8")
    write_all(dw)
    assert "HAND-EDIT-MARKER" in path.read_text(encoding="utf-8")
    write_all(dw, overwrite="1")
    assert "HAND-EDIT-MARKER" not in path.read_text(encoding="utf-8")


def test_a_provider_without_a_key_gives_a_one_line_message_naming_the_variable_and_writes_nothing(dw):
    for provider, variable in (("publik", "PUBLIK_API_KEY"), ("openai", "OPENAI_API_KEY"),
                               ("anthropic", "ANTHROPIC_API_KEY")):
        r = dw.post(dw.url("drafts_write"), {"provider": provider}, follow=True)
        assert r.status_code == 200 and "Traceback" not in body(r)
        assert variable in page_text(r), provider
    r = dw.post(dw.url("drafts_write"), {"provider": "bogus"}, follow=True)
    assert r.status_code < 500
    drafts = dw.ws / "drafts"
    assert not list(drafts.glob("*.md")) if drafts.exists() else True


def test_writing_drafts_with_no_contacts_or_no_sender_is_a_friendly_refusal(make_web):
    no_contacts = make_web(sender=True)
    r = write_all(no_contacts)
    assert r.status_code < 500 and "Traceback" not in body(r)
    no_sender = make_web()
    seed_contacts(no_sender, [ADA])
    r = write_all(no_sender)
    assert r.status_code < 500 and "Traceback" not in body(r)
    assert not (no_sender.ws / "drafts" / "ada.md").exists()


# ---------------------------------------------------------------------------------------------------------------
# The editor
# ---------------------------------------------------------------------------------------------------------------

def test_the_editor_shows_to_subject_and_the_body_in_a_textarea(dw):
    write_all(dw)
    markup = dw.html(dw.url("draft_edit", slug="ada"))
    draft = read_draft_file(dw.ws, "ada")
    assert re.search(r'(?is)<input\b[^>]*name="to"[^>]*value="ada@example.com"', markup) or "ada@example.com" in markup
    assert draft.subject in markup
    textarea = re.search(r'(?is)<textarea\b[^>]*name="body"[^>]*>(.*?)</textarea>', markup)
    assert textarea and "I came across your essay" in textarea.group(1)


def test_saving_a_clean_edit_updates_the_file_and_shows_no_errors(dw):
    write_all(dw)
    draft = read_draft_file(dw.ws, "ada")
    new_body = draft.body.replace("I found it interesting.", "I found it very interesting.")
    r = dw.post(dw.url("draft_save", slug="ada"), {"to": draft.to, "subject": draft.subject, "lane": "first_touch",
                                                   "in_reply_to": "", "references": "", "body": new_body}, follow=True)
    assert r.status_code == 200
    saved = read_draft_file(dw.ws, "ada")
    assert saved.body == new_body and saved.status == "ok"
    assert not engine_errors(dw, saved.subject, saved.body)
    # The recipient belongs to the contact (SPEC_QUESTIONS.md Q5): the editor shows To but a posted value must not
    # redirect the draft to another address; the address changes by editing the contact.
    dw.post(dw.url("draft_save", slug="ada"), {"to": "ada.new@example.com", "subject": draft.subject,
                                               "lane": "first_touch", "in_reply_to": "", "references": "",
                                               "body": new_body}, follow=True)
    assert read_draft_file(dw.ws, "ada").to == "ada@example.com"
    assert read_draft_file(dw.ws, "ada").body == new_body


@pytest.mark.parametrize("kind", ["em-dash-in-body", "exclamation", "forbidden-phrase"])
def test_saving_a_bad_edit_re_lints_with_the_engine_and_names_each_problem(dw, kind):
    write_all(dw)
    draft = read_draft_file(dw.ws, "ada")
    subject, new_body = draft.subject, draft.body
    if kind == "em-dash-in-body":
        new_body = new_body.replace("Hi Ada,", f"Hi Ada {EM_DASH} hello,")
    elif kind == "exclamation":
        new_body = new_body.replace("I found it interesting.", "I found it interesting!")
    elif kind == "forbidden-phrase":
        new_body = new_body.replace("a founder", "a founder seeking synergy")
    else:
        new_body = new_body.replace("We build a tool", "We build\na tool")
    expected = engine_errors(dw, subject, new_body)
    assert expected, "test setup: the engine must see an error here"
    r = dw.post(dw.url("draft_save", slug="ada"), {"to": draft.to, "subject": subject, "lane": "first_touch",
                                                   "in_reply_to": "", "references": "", "body": new_body}, follow=True)
    text = page_text(r)
    missing = [i.code for i in expected if not shows_issue(text, i)]
    assert not missing, f"the page does not show: {missing}"


def test_line_breaks_typed_into_header_fields_cannot_add_headers_to_the_draft_file(dw):
    write_all(dw)
    draft = read_draft_file(dw.ws, "ada")
    dw.post(dw.url("draft_save", slug="ada"), {
        "to": "ada@example.com\nBcc: spy@evil.example.com", "subject": "Hello\nBcc: spy@evil.example.com",
        "lane": "first_touch", "in_reply_to": "", "references": "", "body": draft.body,
    }, follow=True)
    head = (dw.ws / "drafts" / "ada.md").read_text(encoding="utf-8").split("\n\n", 1)[0].split("\n")
    assert len(head) == 8, head
    assert not [line for line in head if line.lower().startswith("bcc")]


def test_a_blocked_draft_shows_why_in_the_editor(dw):
    write_all(dw)
    # The engine records the reason in the draft file header (index.jsonl keeps an empty issue list for it).
    header = (dw.ws / "drafts" / "blocked" / "mina.md").read_text(encoding="utf-8").split("\n\n", 1)[0]
    codes = re.search(r"(?m)^issues:\s*(.+)$", header).group(1).replace(" ", "").split(",")
    assert "insufficient_facts" in codes
    text = page_text(dw.get(dw.url("draft_edit", slug="mina"))).lower()
    assert "insufficient_facts" in text or "insufficient facts" in text or "contact facts" in text


def test_marking_a_draft_reviewed_adds_an_activity_to_the_contact(dw):
    write_all(dw)
    draft = read_draft_file(dw.ws, "ada")
    r = dw.post(dw.url("draft_reviewed", slug="ada"), {
        "action": "review", "reviewed": "1", "to": draft.to, "subject": draft.subject, "lane": "first_touch",
        "in_reply_to": "", "references": "", "body": draft.body}, follow=True)
    assert r.status_code == 200
    ref = contact_ref(dw, "ada@example.com")
    assert "review" in dw.text(f"/crm/{ref}").lower()


def test_the_send_plan_lists_the_ok_drafts_that_would_go_out(send_web):
    w = send_web()
    text = w.text("/crm/send-plan")
    for email in ("ada@example.com", "grace@example.com", "linus@example.com"):
        assert email in text
    assert w.transport.calls == []


# ---------------------------------------------------------------------------------------------------------------
# File safety: traversal and symlinks
# ---------------------------------------------------------------------------------------------------------------

OUTSIDE_TEXT = (
    "to: outside@example.com\nsubject: outside\nstatus: ok\nlane: first_touch\nprovider:\n"
    "in_reply_to:\nreferences:\nissues:\n\nOUTSIDE-SECRET-MARKER body text\n"
)


@pytest.mark.parametrize("slug", ["..%2f..%2foutside", ".hidden"])
def test_slugs_that_try_to_leave_the_drafts_folder_are_refused_and_nothing_outside_changes(dw, tmp_path, slug):
    write_all(dw)
    outside = tmp_path / "outside.md"
    outside.write_text(OUTSIDE_TEXT, encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())
    r = dw.get(f"/crm/drafts/{slug}")
    assert r.status_code in (400, 404) and "OUTSIDE-SECRET-MARKER" not in body(r)
    r = dw.post(f"/crm/drafts/{slug}", {"to": "x@example.com", "subject": "s", "body": "OVERWRITTEN"})
    assert r.status_code in (400, 404)
    assert outside.read_text(encoding="utf-8") == OUTSIDE_TEXT
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_saving_to_a_slug_with_no_draft_is_a_404_and_creates_no_file(dw):
    write_all(dw)
    before = sorted(p.name for p in (dw.ws / "drafts").rglob("*"))
    r = dw.post(dw.url("draft_save", slug="nobody"), {"to": "n@example.com", "subject": "s", "body": "text"})
    assert r.status_code == 404
    assert sorted(p.name for p in (dw.ws / "drafts").rglob("*")) == before


def test_a_draft_that_is_a_symlink_to_a_file_outside_is_never_read_or_written(dw, tmp_path):
    write_all(dw)
    outside = tmp_path / "outside.md"
    outside.write_text(OUTSIDE_TEXT, encoding="utf-8")
    (dw.ws / "drafts" / "evil.md").symlink_to(outside)
    assert "OUTSIDE-SECRET-MARKER" not in body(dw.get(dw.url("draft_edit", slug="evil")))
    assert "OUTSIDE-SECRET-MARKER" not in body(dw.get("/crm/drafts"))
    dw.post(dw.url("draft_save", slug="evil"), {"to": "x@example.com", "subject": "s", "body": "OVERWRITTEN"})
    assert outside.read_text(encoding="utf-8") == OUTSIDE_TEXT


def test_a_drafts_folder_that_is_a_symlink_to_another_folder_is_not_followed(make_web, tmp_path):
    w = make_web(sender=True)
    seed_contacts(w, [ADA])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "stray.md").write_text(OUTSIDE_TEXT, encoding="utf-8")
    if (w.ws / "drafts").is_dir() and not (w.ws / "drafts").is_symlink():
        shutil.rmtree(w.ws / "drafts")  # the app created an empty folder when it started
    (w.ws / "drafts").symlink_to(elsewhere, target_is_directory=True)
    assert "OUTSIDE-SECRET-MARKER" not in body(w.get("/crm/drafts"))
    write_all(w)
    assert sorted(p.name for p in elsewhere.iterdir()) == ["stray.md"], "drafts were written through the symlink"
