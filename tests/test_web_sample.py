"""The shipped sample: what "Load the sample" creates, that it never overwrites, and what it must not contain.

SPEC_V3 sections 2 and 3, SPEC.md section 7. The sample sender is Publik's, built from a real first-email
structure with the private parts removed; all people, firms and addresses are fictional.
No dash characters appear in this file.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List
from urllib.parse import urlsplit

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, REPO_ROOT, Web, analyze, body, contact_ref, export_rows, owner_hits, page_text, read_draft_file, run_cli,
    seed_contacts, workspace_text_files, write_sender_json,
)

# SPEC.md section 7 names the owner's real first-email subject; SPEC_V3 section 2 ("remove only owner identity") makes the
# shipped sample use a generic subject instead. The subject is therefore read from the loaded sample sender and must be
# a non-empty, single-line value that the engine accepted (SPEC_QUESTIONS.md Q9).
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
SAMPLE_PAGES = ["/crm", "/crm/drafts", "/crm/analytics", "/settings", "/replies", "/crm/send-plan"]
# The only Calendly link a shipped file may carry is the generic placeholder. Any other calendly.com link fails.
CALENDLY_PLACEHOLDER = "https://calendly.com/your-handle/30min"
CALENDLY_URL_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?calendly\.com[^\s\"'<>)\]},;\\]*")


def calendly_offenders(text: str) -> List[str]:
    """Every calendly.com link in text that is not exactly the placeholder."""
    found = (m.group(0).rstrip(".") for m in CALENDLY_URL_RE.finditer(text))
    return sorted({url for url in found if url != CALENDLY_PLACEHOLDER})


def sample_subject(w: Web) -> str:
    from vcoutreach.sender import load_sender

    subject = load_sender(w.ws / "sender.json").first_touch_subject
    assert subject and "\n" not in subject and "Texas A&M" not in subject
    return subject


def example_domain(domain: str) -> bool:
    domain = domain.lower()
    return domain == "example.com" or domain.endswith(".example.com")


@pytest.fixture
def drafted(sample_web):
    sample_web.post(sample_web.url("drafts_write"), {"provider": "template"}, follow=True)
    return sample_web


def test_the_empty_overview_and_settings_offer_the_sample_button_with_a_csrf_field(web):
    for path in ("/crm", "/settings"):
        markup = web.html(path)
        form = re.search(r'(?is)<form\b[^>]*action="/sample/load"[^>]*>(.*?)</form>', markup)
        assert form, f"{path} needs a Load the sample form"
        assert 'name="csrf_token"' in form.group(1)
    assert web.get(web.url("sample_load")).status_code == 405
    assert export_rows(web) == []


def test_loading_the_sample_creates_five_contacts_four_first_touch_and_one_follow_up_with_thread_fields(sample_web):
    rows = export_rows(sample_web)
    assert len(rows) == 5
    lanes = sorted(r["lane"] for r in rows)
    assert lanes == ["first_touch"] * 4 + ["follow_up"]
    follow = [r for r in rows if r["lane"] == "follow_up"][0]
    assert re.fullmatch(r"<[^<>@\s]+@[^<>@\s]+>", follow["in_reply_to"]) and follow["thread_subject"]
    assert "/sample/load" not in sample_web.html("/crm"), "the overview should stop pushing the sample once loaded"


def test_every_address_and_source_url_in_the_sample_is_on_example_com(sample_web):
    for row in export_rows(sample_web):
        assert example_domain(row["email"].split("@")[1]), row["email"]
        for url in filter(None, (u.strip() for u in row["source_urls"].split("|"))):
            assert example_domain(urlsplit(url).hostname or ""), url
    leaks = []
    for path, text in workspace_text_files(sample_web.ws).items():
        if path.name == ".env" or path.suffix in (".db", ".sqlite", ".sqlite3"):
            continue
        leaks += [(path.name, m.group(0)) for m in EMAIL_RE.finditer(text) if not example_domain(m.group(1))]
    # the database is read cell by cell: scanning its raw bytes glues neighbouring values together
    import sqlite3

    conn = sqlite3.connect(str(sample_web.ws / "data" / "outreach.db"))
    try:
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                for cell in row:
                    if isinstance(cell, str):
                        leaks += [(table, m.group(0)) for m in EMAIL_RE.finditer(cell)
                                  if not example_domain(m.group(1))]
    finally:
        conn.close()
    assert not leaks, leaks[:5]


def test_the_sample_sender_is_publiks_and_passes_the_engine_loader(sample_web):
    from vcoutreach.sender import load_sender

    sender = load_sender(sample_web.ws / "sender.json")
    assert sample_subject(sample_web) == sender.first_touch_subject
    assert sender.org == "Publik"
    text = (sample_web.ws / "sender.json").read_text(encoding="utf-8")
    for needle in ("25M+ views", "7,500", "2,100"):
        assert needle in text, needle
    assert "https://calendly.com/your-handle/30min" in sender.ask_line


def test_the_sample_carries_no_wrong_numbers_no_owner_calendly_and_no_raise_amount(sample_web):
    for path, text in workspace_text_files(sample_web.ws).items():
        if path.name == ".env":
            continue
        low = text.lower()
        assert "750k" not in low, path.name
        assert not calendly_offenders(text), path.name
    assert "$" not in (sample_web.ws / "sender.json").read_text(encoding="utf-8")


def test_loading_twice_changes_nothing_and_says_what_was_skipped(sample_web):
    before_rows = export_rows(sample_web)
    before_sender = (sample_web.ws / "sender.json").read_bytes()
    r = sample_web.post(sample_web.url("sample_load"), {}, follow=True)
    assert r.status_code == 200
    assert re.search(r"(?i)skip|already|exist", page_text(r))
    assert export_rows(sample_web) == before_rows
    assert (sample_web.ws / "sender.json").read_bytes() == before_sender


def test_an_existing_sender_profile_is_never_overwritten(make_web):
    w = make_web(sender=True, name="Existing Person", org="Existing Org")
    before = (w.ws / "sender.json").read_bytes()
    r = w.post(w.url("sample_load"), {}, follow=True)
    assert r.status_code < 500
    assert (w.ws / "sender.json").read_bytes() == before


def test_an_existing_contact_with_a_sample_address_is_kept_as_is_and_own_contacts_are_untouched(make_web, tmp_path):
    probe = make_web(dirname="probe")
    probe.post(probe.url("sample_load"), {}, follow=True)
    sample_email = export_rows(probe)[0]["email"]
    w = make_web(dirname="mine")
    seed_contacts(w, [ADA, {**ADA, "id": "mine", "name": "My Own Name", "email": sample_email}])
    w.post(w.url("sample_load"), {}, follow=True)
    rows = {r["email"]: r for r in export_rows(w)}
    assert rows[sample_email]["name"] == "My Own Name"
    assert rows["ada@example.com"]["name"] == "Ada Lovelace"
    assert len([e for e in rows if e not in (sample_email, "ada@example.com")]) in (0, 4), (
        "the sample must either skip everything or add only the four people that do not clash")


def test_with_the_template_provider_exactly_one_sample_contact_is_blocked_for_insufficient_facts(drafted):
    ok = sorted(p.stem for p in (drafted.ws / "drafts").glob("*.md"))
    blocked = sorted(p.stem for p in (drafted.ws / "drafts" / "blocked").glob("*.md"))
    assert len(ok) == 4 and len(blocked) == 1
    # the engine writes the reason into the draft file header (its index.jsonl keeps an empty issue list)
    header = (drafted.ws / "drafts" / "blocked" / f"{blocked[0]}.md").read_text(encoding="utf-8").split("\n\n", 1)[0]
    assert "insufficient_facts" in header


def test_the_command_line_agrees_the_sample_gives_four_drafts_and_one_blocked(sample_web, tmp_path):
    exported = tmp_path / "sample-export.csv"
    exported.write_text(body(sample_web.get(sample_web.url("contact_export"))), encoding="utf-8")
    result = run_cli(["draft", "--contacts", str(exported), "--sender", str(sample_web.ws / "sender.json"),
                      "--offline", "--out", str(tmp_path / "cli")], cwd=Path.cwd(), home=Path(os.environ["HOME"]))
    assert result.returncode == 0, result.stderr
    assert "drafted=4 blocked=1 provider=template" in result.stdout
    assert "insufficient_facts" in result.stdout


def test_first_touch_sample_drafts_use_the_sender_subject_and_the_follow_up_keeps_its_thread(drafted):
    rows = export_rows(drafted)
    subject = sample_subject(drafted)
    for row in rows:
        draft = read_draft_file(drafted.ws, row["id"])
        if row["lane"] == "first_touch" and draft.status == "ok":
            assert draft.subject == subject
        if row["lane"] == "follow_up":
            assert draft.subject == row["thread_subject"]
            assert draft.in_reply_to == row["in_reply_to"]


def test_the_sample_adds_one_reply_and_two_batches(sample_web):
    replies = sample_web.html("/replies")
    ids = {m for m in re.findall(r"/replies/(\d+)", replies)}
    assert len(ids) == 1
    # batches are the CSV cohorts of SPEC.md section 4 (table `batches` in the workspace database, CONTRACT.md)
    import sqlite3

    conn = sqlite3.connect(str(sample_web.ws / "data" / "outreach.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(DISTINCT contact_id) FROM contact_batches").fetchone()[0] == 5
    finally:
        conn.close()


def test_pages_of_the_loaded_sample_contain_no_owner_strings_and_no_wrong_numbers(drafted):
    problems = []
    for path in SAMPLE_PAGES:
        markup = drafted.html(path)
        low = markup.lower()
        if owner_hits(markup) or "750k" in low or calendly_offenders(markup):
            problems.append(path)
    assert not problems, problems


def test_every_calendly_link_in_the_shipped_web_package_is_the_generic_placeholder():
    shipped = workspace_text_files(REPO_ROOT / "vcoutreach" / "web")
    assert shipped, "the shipped web package must be readable"
    # counts only: a failure message must not echo the link itself
    offending = {
        str(path.relative_to(REPO_ROOT)): len(calendly_offenders(text))
        for path, text in shipped.items() if calendly_offenders(text)
    }
    assert not offending, offending


def test_the_calendly_guard_rejects_every_link_other_than_the_placeholder():
    assert calendly_offenders(CALENDLY_PLACEHOLDER) == []
    assert calendly_offenders(f'<a href="{CALENDLY_PLACEHOLDER}">book</a>') == []
    assert calendly_offenders(f"book at {CALENDLY_PLACEHOLDER}.") == []
    assert calendly_offenders("https://calendly.com/someone-else/30min") == ["https://calendly.com/someone-else/30min"]
    assert calendly_offenders("see calendly.com/someone-else/30min") == ["calendly.com/someone-else/30min"]
    assert calendly_offenders("https://calendly.com/your-handle/15min") == ["https://calendly.com/your-handle/15min"]
    assert calendly_offenders("https://calendly.com") == ["https://calendly.com"]


