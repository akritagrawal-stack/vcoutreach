"""Contacts in the web app: create, edit, import, export, delete, filters, activities and tasks.

SPEC_V3 section 2, SPEC_V2 section 2, SPEC.md sections 4 and 5. Errors keep the engine contact
loader's wording (checked by fragments). Drafts on disk are the engine's own files.
No dash characters appear in this file.
"""
from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, CSV_COLUMNS, GRACE, LINUS, MINA, OREN, STAGES, Web, analyze, body, contact_form_fields,
    contact_ref, csv_row, csv_text, engine_draft_all, export_rows, import_csv, page_text, run_cli,
    seed_contacts, set_stage, stage_of, typed,
)

# SPEC.md section 5 lists note, call, meeting and email_sent_manually; the kept old app has note, call, meeting and
# follow_up (SPEC_V3 section 2 keeps the old pages). The three kinds both lists share are required (SPEC_QUESTIONS.md Q3).
ACTIVITY_KINDS = ["note", "call", "meeting"]


def names_on(w: Web, path: str, **params) -> str:
    return w.text(path, query_string=params)


def create(w: Web, row, **over):
    fields = contact_form_fields(row)
    fields.update(over)
    return w.post(w.url("contact_new"), fields, follow=True)


def activity_fields(kind: str, text: str):
    out = {name: kind for name in ("kind", "activity_kind", "type", "activity_type")}
    out.update({name: text for name in ("body", "note", "text", "summary", "details", "snippet")})
    out["subject"] = text
    return out


# ---------------------------------------------------------------------------------------------------------------
# Create and validate
# ---------------------------------------------------------------------------------------------------------------

def test_the_new_contact_form_creates_a_contact_shown_in_the_list_and_the_export(web):
    r = web.post(web.url("contact_new"), contact_form_fields(ADA))
    assert r.status_code in (302, 303)
    assert "Ada Lovelace" in web.text("/crm")
    rows = export_rows(web)
    assert [(x["id"], x["email"], x["org"]) for x in rows] == [("ada", "ada@example.com", "Northwind Labs")]
    text = web.text(f"/crm/{contact_ref(web, 'ada@example.com')}")
    for expected in ("Ada Lovelace", "ada@example.com", "Northwind Labs", "Partner",
                     "your essay on how small teams ship weekly", "https://example.com/essay"):
        assert expected in text, expected


def test_emails_are_trimmed_and_lower_cased(web):
    create(web, ADA, email="  Ada@Example.COM  ")
    assert [x["email"] for x in export_rows(web)] == ["ada@example.com"]


@pytest.mark.parametrize("over, fragment", [
    ({"email": "not-an-email"}, "invalid email"),
    ({"email": "Ada Lovelace <ada@example.com>"}, "bare address"),
    ({"name": ""}, "name is required"),
    ({"lane": "follow_up", "in_reply_to": "", "thread_subject": "hello"}, "in_reply_to"),
], ids=["email", "display-name", "no-name", "follow-up-thread"])
def test_invalid_contacts_are_refused_inline_with_the_engine_wording(web, over, fragment):
    r = create(web, ADA, **over)
    assert r.status_code in (200, 400, 422)
    assert fragment in page_text(r).lower() or fragment in page_text(r)
    assert export_rows(web) == []


def test_an_empty_new_contact_form_is_refused_inline_never_a_server_error(web):
    for fields in ({}, {"name": "", "email": ""}, {"name": "   ", "email": "   "}):
        r = web.post(web.url("contact_new"), fields)
        assert r.status_code in (200, 400, 422), (fields, r.status_code)
        assert "Traceback" not in body(r)
    assert export_rows(web) == []


def test_a_duplicate_email_or_slug_is_refused_even_when_the_case_differs(web):
    create(web, ADA)
    r = create(web, GRACE, email="ADA@example.com")
    assert "duplicate" in page_text(r).lower()
    r = create(web, GRACE, slug="ada", id="ada")
    assert "duplicate" in page_text(r).lower()
    assert [x["email"] for x in export_rows(web)] == ["ada@example.com"]


def test_csv_import_rejects_lowercase_id_already_stored_in_uppercase(web):
    seed_contacts(web, [{**ADA, "id": "ADA"}])
    assert [(r["id"], r["email"]) for r in export_rows(web)] == [("ADA", ADA["email"])]
    response = import_csv(web, csv_text([{**GRACE, "id": "ada"}]))
    assert "duplicate id" in page_text(response).lower()
    assert [(r["id"], r["email"]) for r in export_rows(web)] == [("ADA", ADA["email"])]


def test_hostile_text_in_a_contact_is_escaped_on_every_page_that_shows_it(web):
    evil = '<script>alert(1)</script>'
    create(web, ADA, name=evil + " Ada", org='"><img src=x onerror=alert(1)>')
    ref = contact_ref(web, "ada@example.com")
    for path in ("/crm", f"/crm/{ref}"):
        markup = body(web.get(path))
        assert evil not in markup
        assert "<img src=x" not in markup
        parsed = analyze(markup)
        assert parsed.inline_scripts == 0 and parsed.handlers == []
        assert "alert(1)" in page_text(markup)


# ---------------------------------------------------------------------------------------------------------------
# Edit profile, stages, activities, tasks
# ---------------------------------------------------------------------------------------------------------------

def test_every_stage_can_be_set_and_is_remembered(web):
    seed_contacts(web, [ADA])
    for stage in STAGES:
        set_stage(web, "ada@example.com", stage)
        assert stage_of(web, "ada@example.com") == stage, stage


def test_an_unknown_stage_is_refused_and_the_old_stage_stays(web):
    seed_contacts(web, [ADA])
    set_stage(web, "ada@example.com", "warm")
    set_stage(web, "ada@example.com", "not-a-stage")
    assert stage_of(web, "ada@example.com") == "warm"


def test_profile_fields_tags_notes_and_next_action_are_saved(web):
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    web.post(web.url("contact_profile", ref=ref), {
        "stage": "warm", "tags": "alpha,beta", "notes": "Prefers short emails", "next_action": "Follow up in May",
        "name": "Ada Lovelace", "email": "ada@example.com", "org": "Northwind Labs",
    }, follow=True)
    text = web.text(f"/crm/{ref}")
    for expected in ("alpha", "beta", "Prefers short emails", "Follow up in May"):
        assert expected in text, expected


def test_activity_kinds_are_recorded_and_shown_escaped(web):
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    for kind in ACTIVITY_KINDS:
        r = web.post(web.url("contact_activity", ref=ref), activity_fields(kind, f"Talked about <b>{kind}</b>"))
        assert r.status_code in (302, 303), kind
    markup = web.html(f"/crm/{ref}")
    for kind in ACTIVITY_KINDS:
        assert f"Talked about" in page_text(markup) and f"<b>{kind}</b>" not in markup, kind


def test_an_unknown_activity_kind_is_refused(web):
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    web.post(web.url("contact_activity", ref=ref), activity_fields("note", "UNIQUE-REAL-NOTE"), follow=True)
    assert "UNIQUE-REAL-NOTE" in web.text(f"/crm/{ref}"), "test setup: a valid kind is recorded and shown"
    web.post(web.url("contact_activity", ref=ref), activity_fields("hack", "UNIQUE-HACK-NOTE"), follow=True)
    assert "UNIQUE-HACK-NOTE" not in web.text(f"/crm/{ref}")


def test_a_follow_up_task_can_be_completed(web):
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    web.post(web.url("contact_profile", ref=ref), {
        "stage": "warm", "next_follow_up_at": "2031-01-02T10:00", "follow_up_reason": "Send requested materials",
        "next_action": "Send requested materials",
    }, follow=True)
    markup = web.html(f"/crm/{ref}")
    assert "Send requested materials" in page_text(markup)
    ids = re.findall(r"/tasks/(\d+)/complete", markup)
    assert ids, "the contact page needs a Complete button for the open task"
    r = web.post(web.url("task_complete", id=ids[0]), {}, follow=True)
    assert r.status_code == 200
    after = web.html(f"/crm/{ref}")
    assert f"/tasks/{ids[0]}/complete" not in after or re.search(r"(?i)done|completed|checked", after)


# ---------------------------------------------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------------------------------------------

def test_a_valid_csv_import_creates_every_row_and_reports_the_count(web):
    r = import_csv(web, csv_text([ADA, GRACE, LINUS]))
    assert r.status_code == 200
    assert re.search(r"\b3\b", page_text(r)) and re.search(r"(?i)import|added|created", page_text(r))
    assert sorted(x["id"] for x in export_rows(web)) == ["ada", "grace", "linus"]


def test_batches_are_named_after_the_uploaded_file_or_import_with_the_date_and_time(web):
    import_csv(web, csv_text([ADA, GRACE]), filename="investors-q3.csv")
    import_csv(web, csv_text([LINUS]))
    combined = web.text("/crm") + web.text("/crm/analytics")
    assert "investors-q3" in combined
    assert re.search(r"Import \d{4}-\d{2}-\d{2} \d{2}:\d{2}", combined)


def test_one_bad_row_blocks_the_whole_import_and_every_bad_row_is_named_in_visible_text(web):
    def bad(i):
        return csv_row(id=f"bad{i}", name=f"Bad {i}", email=f"nope{i}")

    r = import_csv(web, csv_text([bad(1), ADA, bad(2), GRACE, bad(3)]))
    text = page_text(r)
    assert "invalid email" in text
    for number in (2, 4, 6):
        assert f"row {number}" in text, number
    assert export_rows(web) == []


def test_duplicates_inside_the_file_are_skipped_and_counted(web):
    r = import_csv(web, csv_text([ADA, {**GRACE, "email": "ada@example.com"}]))
    assert "Imported 1 new contact; skipped 1 already in this workspace" in page_text(r)
    assert [x["email"] for x in export_rows(web)] == [ADA["email"]]


def test_existing_emails_are_skipped_while_new_contacts_are_imported(web):
    create(web, ADA)
    response = import_csv(web, csv_text([GRACE, {**LINUS, "email": "ADA@example.com"}]))
    assert "Imported 1 new contact; skipped 1 already in this workspace" in page_text(response)
    assert {x["email"] for x in export_rows(web)} == {ADA["email"], GRACE["email"]}


@pytest.mark.parametrize("text, fragment", [
    ('{"name": "Ada", "email": "ada@example.com"}', "looks like JSON"),
    ("name,org\nAda,Northwind\n", "missing required column"),
], ids=["json", "no-email-column"])
def test_a_file_that_is_not_a_contacts_csv_is_explained_and_nothing_is_created(web, text, fragment):
    r = import_csv(web, text)
    assert fragment in page_text(r)
    assert export_rows(web) == []


def test_a_byte_order_mark_and_windows_line_endings_are_accepted(web):
    raw = "﻿" + csv_text([ADA, GRACE]).replace("\n", "\r\n")
    import_csv(web, raw, filename="from-excel.csv")
    assert sorted(x["id"] for x in export_rows(web)) == ["ada", "grace"]


# ---------------------------------------------------------------------------------------------------------------
# Export and the round trip through the engine
# ---------------------------------------------------------------------------------------------------------------

def test_the_export_is_a_csv_with_the_eleven_standard_columns_even_when_empty(web):
    r = web.get(web.url("contact_export"))
    assert r.status_code == 200 and "csv" in r.headers["Content-Type"]
    assert next(csv.reader(io.StringIO(body(r)))) == CSV_COLUMNS


def test_the_export_loads_in_the_engine_contact_loader_with_the_same_data(web, tmp_path):
    from vcoutreach.contacts import load_contacts

    rows = [ADA, GRACE, OREN, MINA]
    source = tmp_path / "source.csv"
    source.write_text(csv_text(rows), encoding="utf-8")
    seed_contacts(web, rows)
    exported = tmp_path / "exported.csv"
    exported.write_text(body(web.get(web.url("contact_export"))), encoding="utf-8")
    key = lambda c: c.id
    assert sorted(load_contacts(exported), key=key) == sorted(load_contacts(source), key=key)
    awkward = 'Acme, Inc. "The Lab"'
    create(web, LINUS, org=awkward, role="Head of; Growth")
    row = [r for r in export_rows(web) if r["id"] == "linus"][0]
    assert row["org"] == awkward and row["role"] == "Head of; Growth"


def test_the_cli_drafts_from_an_exported_csv(make_web, tmp_path):
    w = make_web(sender=True)
    seed_contacts(w, [ADA, GRACE, MINA, OREN])
    exported = tmp_path / "exported.csv"
    exported.write_text(body(w.get(w.url("contact_export"))), encoding="utf-8")
    out = tmp_path / "cli-drafts"
    result = run_cli(["draft", "--contacts", str(exported), "--sender", str(w.ws / "sender.json"),
                      "--offline", "--out", str(out)], cwd=Path.cwd(), home=Path(os.environ["HOME"]))
    assert result.returncode == 0, result.stderr
    assert "drafted=3 blocked=1 provider=template" in result.stdout
    assert (out / "ada.md").is_file() and (out / "blocked" / "mina.md").is_file()


def test_importing_an_export_into_a_new_workspace_gives_the_same_export(make_web):
    first, second = make_web(), make_web()
    seed_contacts(first, [ADA, GRACE, OREN])
    exported = body(first.get(first.url("contact_export")))
    import_csv(second, exported)
    key = lambda r: r["id"]
    assert sorted(export_rows(second), key=key) == sorted(export_rows(first), key=key)


# ---------------------------------------------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("typed_value", ["ADA"])
def test_delete_needs_the_exact_slug_typed_and_otherwise_keeps_the_contact(send_web, typed_value):
    w = send_web([ADA, GRACE])
    ref = contact_ref(w, "ada@example.com")
    w.post(w.url("contact_delete", ref=ref), typed(typed_value), follow=True)
    assert "ada" in [x["id"] for x in export_rows(w)]
    assert (w.ws / "drafts" / "ada.md").is_file()


def test_delete_with_the_slug_removes_the_contact_and_moves_draft_files_to_removed(send_web):
    w = send_web([ADA, GRACE, MINA])
    ada_text = (w.ws / "drafts" / "ada.md").read_text(encoding="utf-8")
    mina_text = (w.ws / "drafts" / "blocked" / "mina.md").read_text(encoding="utf-8")
    for slug, email in (("ada", "ada@example.com"), ("mina", "mina@example.com")):
        ref = contact_ref(w, email)
        r = w.post(w.url("contact_delete", ref=ref), typed(slug), follow=True)
        assert r.status_code == 200
        assert slug not in [x["id"] for x in export_rows(w)]
        assert w.get(f"/crm/{ref}").status_code == 404
    assert not (w.ws / "drafts" / "ada.md").exists() and not (w.ws / "drafts" / "blocked" / "mina.md").exists()
    # SPEC only says the files are moved to drafts/removed/; a collision-proof suffix after the slug is fine.
    removed = {p.name: p.read_text(encoding="utf-8") for p in (w.ws / "drafts" / "removed").rglob("*.md")}

    def archived(slug: str):
        return [text for name, text in removed.items() if re.fullmatch(rf"{re.escape(slug)}([-_.][A-Za-z0-9]+)*\.md", name)]

    assert archived("ada") == [ada_text] and archived("mina") == [mina_text]
    assert (w.ws / "drafts" / "grace.md").is_file()


def test_a_deleted_contact_takes_its_activities_with_it(web):
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    web.post(web.url("contact_activity", ref=ref), activity_fields("note", "OLD-HISTORY-MARKER"))
    assert "OLD-HISTORY-MARKER" in web.text(f"/crm/{ref}"), "test setup: the activity must be visible first"
    web.post(web.url("contact_delete", ref=ref), typed("ada"), follow=True)
    create(web, ADA)
    assert "OLD-HISTORY-MARKER" not in web.text(f"/crm/{contact_ref(web, 'ada@example.com')}")


def test_delete_is_post_only_and_missing_contacts_and_tasks_are_not_server_errors(web):
    assert web.get("/crm/no-such-person-xyz").status_code == 404
    assert web.post(web.url("task_complete", id=987654), {}).status_code in (302, 303, 404)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    assert web.get(web.url("contact_delete", ref=ref)).status_code == 405
    assert [x["id"] for x in export_rows(web)] == ["ada"]
    r = web.post(web.url("contact_delete", ref="no-such-person"), typed("no-such-person"))
    assert r.status_code in (302, 303, 404)


# ---------------------------------------------------------------------------------------------------------------
# Filters on the contact list
# ---------------------------------------------------------------------------------------------------------------

C1 = csv_row(id="c1", name="Zebediah Quill", email="c1@example.com", org="Aaa Org", facts="your note on kayaks")
C2 = csv_row(id="c2", name="Berta Moon", email="berta.mm@example.com", org="Marigold Capital",
             facts="your essay on lighthouses")
C3 = csv_row(id="c3", name="Carl Dune", email="carl@example.net", org="Ccc Org", facts="your talk on glaciers")
NAMES = {"c1": "Zebediah Quill", "c2": "Berta Moon", "c3": "Carl Dune"}


def shown(w: Web, **params):
    text = w.text("/crm", query_string=params)
    return sorted(slug for slug, name in NAMES.items() if name in text)


def test_the_stage_filter_shows_only_that_stage(web):
    seed_contacts(web, [C1, C2, C3])
    set_stage(web, "berta.mm@example.com", "warm")
    set_stage(web, "carl@example.net", "passed")
    assert shown(web, stage="warm") == ["c2"]
    assert shown(web, stage="passed") == ["c3"]
    assert shown(web, stage="prospect") == ["c1"]


@pytest.mark.parametrize("query, expected", [
    ("zebediah", ["c1"]), ("MARIGOLD", ["c2"]), ("nobody-matches-this", []),
])
def test_the_search_box_matches_name_org_email_and_facts_ignoring_case(web, query, expected):
    seed_contacts(web, [C1, C2, C3])
    assert shown(web, q=query) == expected


def test_the_tag_filter_matches_comma_separated_tags(web):
    # tags are set through the New contact form here (CONTRACT.md lists tags there); editing them on the profile
    # is covered by test_profile_fields_tags_notes_and_next_action_are_saved
    create(web, C1, tags="alpha,beta")
    create(web, C2, tags="beta")
    assert shown(web, tag="alpha") == ["c1"]
    assert shown(web, tag="beta") == ["c1", "c2"]


def test_the_draft_status_filter_separates_ok_blocked_and_no_draft(send_web):
    thin = {**C3, "facts": ""}
    w = send_web([C1, C2, thin], drafts=False)
    engine_draft_all(w.ws, [C1, thin])
    assert (shown(w, draft="ok"), shown(w, draft="blocked"), shown(w, draft="none")) == (["c1"], ["c3"], ["c2"])


def test_the_batch_filter_shows_only_the_contacts_of_that_import(web):
    import_csv(web, csv_text([C1]), filename="first-batch.csv")
    import_csv(web, csv_text([C2]), filename="second-batch.csv")
    markup = web.html("/crm")
    select = re.search(r'(?is)<select\b[^>]*name="batch"[^>]*>(.*?)</select>', markup)
    if not select:
        # SPEC_V2 section 2 lists the filters stage, q, tag and draft status; the batch filter is only in SPEC.md
        # section 5 (SPEC_QUESTIONS.md Q4). The batch names must still show somewhere (next test).
        pytest.skip("no batch filter on the contact list (optional, SPEC_QUESTIONS.md Q4)")
    options = re.findall(r'(?is)<option\b[^>]*value="([^"]+)"[^>]*>(.*?)</option>', select.group(1))
    wanted = [value for value, label in options if "first-batch" in label]
    assert wanted
    assert shown(web, batch=wanted[0]) == ["c1"]


def test_hostile_or_odd_filter_values_never_cause_a_server_error_or_list_everyone(web):
    seed_contacts(web, [C1, C2, C3])
    assert shown(web, q="' OR '1'='1") == []
    for params in ({"stage": "nonsense"}, {"draft": "weird"}, {"batch": "999999"}, {"q": "%"}, {"q": "_"}):
        assert web.get("/crm", query_string=params).status_code < 500, params
