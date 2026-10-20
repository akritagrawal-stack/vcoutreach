"""Round 3 browser acceptance tests, authored from the public contracts only."""
import datetime as dt
import io
import re
from urllib.parse import urlsplit

import pytest

from web_helpers import *  # noqa: F401,F403
from web_gap_helpers import (column, draft_row_id, partner_row_id, plan_rows,
                             ready_web, schedule_form, scheduled_rows, sql)


@pytest.mark.parametrize("path,provider,key", [
    ("/api/model-provider/key", "openai", "OPENAI_API_KEY"),
    ("/api/model-provider/key", "anthropic", "ANTHROPIC_API_KEY"),
    ("/api/data-provider/key", "apollo", "APOLLO_API_KEY"),
    ("/api/data-provider/key", "enrow", "ENROW_API_KEY"),
])
def test_blank_key_keeps_saved_key_and_explicit_clear_removes_it(make_web, path, provider, key):
    w = make_web()
    saved = "fake-round3-secret"
    assert w.post(path, {"provider": provider, "api_key": saved}).status_code == 200
    assert read_env(w.ws)[key] == saved
    r = w.post(path, {"provider": provider, "api_key": "   "})
    assert r.status_code == 200
    assert "No key entered; saved key unchanged" in body(r)
    assert read_env(w.ws)[key] == saved
    r = w.post("/settings/keys", {"clear": key})
    assert urlsplit(r.location).path == "/settings"
    assert not read_env(w.ws).get(key)


def test_profile_edit_persists_identity_and_rejects_duplicate_email(make_web):
    w = ready_web(make_web, [ADA, GRACE])
    ref = contact_ref(w, ADA["email"])
    changed = dict(name="Ada Updated", org="New Firm", role="Managing Partner", email="new@example.com")
    r = w.post(f"/crm/{ref}/profile", changed, follow=True)
    assert r.status_code == 200
    row = next(r for r in export_rows(w) if r["id"] == "ada")
    assert {k: row[k] for k in changed} == changed
    r = w.post(f"/crm/{ref}/profile", {**changed, "email": GRACE["email"]}, follow=True)
    assert r.status_code == 200
    assert re.search("already|duplicate|another contact", page_text(r), re.I)
    assert next(r for r in export_rows(w) if r["id"] == "ada")["email"] == changed["email"]


def test_delete_requires_confirmation_then_removes_contact_and_drafts(make_web):
    w = ready_web(make_web, [ADA])
    pid = partner_row_id(w, "ada")
    assert sql(w, "SELECT id FROM drafts WHERE partner_id=?", (pid,))
    r = w.post(f"/crm/{pid}/delete", {}, follow=True)
    assert r.status_code == 200
    assert re.search("confirm|not deleted|not removed", page_text(r), re.I)
    assert len(export_rows(w)) == 1
    r = w.post(f"/crm/{pid}/delete", {"confirm": "yes"}, follow=True)
    assert r.status_code == 200
    assert re.search("deleted|removed", page_text(r), re.I)
    assert export_rows(w) == []
    assert sql(w, "SELECT id FROM drafts WHERE partner_id=?", (pid,)) == []
    assert w.get(f"/crm/{pid}").status_code == 404
    assert w.get("/crm/drafts/ada").status_code == 404


@pytest.mark.parametrize("state", ["pending", "rejected", "contacted"])
def test_plan_and_posted_batch_hold_ineligible_rows_with_reason(make_web, state):
    w = ready_web(make_web, [ADA, GRACE], approve=["grace"] if state == "pending" else None)
    pid = partner_row_id(w, "ada")
    if state == "rejected":
        w.post(f"/partners/{pid}/reject", {"reason": "Not a fit"}, follow=True)
    elif state == "contacted":
        set_stage(w, ADA["email"], "contacted")
    row = next(r for r in plan_rows(w) if ADA["email"] in column(r, "contact"))
    assert ADA["name"] in column(row, "contact")
    assert column(row, "result").lower().startswith("held")
    assert re.search("approv|reject|contacted", " ".join(row.values()), re.I)
    if state != "contacted":
        assert state in column(row, "approval").lower()
    # Post the held row explicitly, as a stale browser form could do.
    r = w.post("/send-batch", {"draft_ids": [str(draft_row_id(w, "ada"))]}, follow=True)
    assert r.status_code == 200
    assert w.transport.calls == []
    from test_enrichment_facts_reach_drafts import flash_text
    text = flash_text(body(r))
    assert re.search("ada", text, re.I)
    assert re.search("approv|reject|contacted", text, re.I)


@pytest.mark.parametrize("recipient", [TEST_INBOX, None])
def test_plan_test_delivery_target_is_explicit(make_web, recipient):
    w = ready_web(make_web, [ADA], recipient=recipient)
    row, = plan_rows(w)
    assert ADA["name"] in column(row, "contact") and ADA["email"] in column(row, "contact")
    assert column(row, "target") == (recipient or "Test recipient not set")
    assert "approved" in column(row, "approval").lower()
    assert w.transport.calls == []


def test_blocked_reason_is_visible_and_approval_disabled(make_web):
    w = ready_web(make_web, [MINA])
    pid = partner_row_id(w, "mina")
    for path in (f"/partners/{pid}", "/crm/drafts/mina"):
        r = w.get(path)
        assert r.status_code == 200
        text = page_text(r).lower()
        assert "blocked" in text
        assert re.search("fact|ground|hook|personaliz", text)
        for form in re.findall(r"(?is)<form\b.*?</form>", body(r)):
            if f"/partners/{pid}/approve" in form:
                buttons = re.findall(r"(?is)<button\b([^>]*)>(.*?)</button>", form)
                assert buttons and all("disabled" in attrs for attrs, label in buttons if "approv" in label.lower())


def test_drafts_list_shows_true_approval_and_edit_notice(make_web):
    w = ready_web(make_web, [ADA, GRACE])
    w.post(f"/partners/{partner_row_id(w, 'grace')}/reject", {"reason": "not yet"}, follow=True)
    rows = plan_rows(w, "/crm/drafts")
    ada = next(r for r in rows if ADA["email"] in " ".join(r.values()) or ADA["name"] in " ".join(r.values()))
    grace = next(r for r in rows if GRACE["email"] in " ".join(r.values()) or GRACE["name"] in " ".join(r.values()))
    assert "approved" in " ".join(ada.values()).lower()
    assert "rejected" in " ".join(grace.values()).lower()
    draft = read_draft_file(w.ws, "ada")
    r = w.post("/crm/drafts/ada", {"subject": draft.subject + " today", "body": draft.body,
                                  "lane": "first_touch"}, follow=True)
    assert "Edit saved; draft returned to Pending and needs re-approval" in page_text(r)
    row, = [r for r in plan_rows(w) if ADA["email"] in column(r, "contact")]
    assert "pending" in column(row, "approval").lower()


def test_csv_skips_existing_and_repeated_emails_and_counts_new_rows(make_web):
    w = make_web()
    import_csv(w, csv_text([ADA]))
    r = import_csv(w, csv_text([GRACE, {**LINUS, "email": ADA["email"].upper()}, GRACE]))
    assert "Imported 1 new contact; skipped 2 already in this workspace" in page_text(r)
    assert {r["email"] for r in export_rows(w)} == {ADA["email"], GRACE["email"]}
    r = import_csv(w, csv_text([LINUS, BEA_ROW]))
    assert "Imported 2 new contacts; skipped 0 already in this workspace" in page_text(r)


BEA_ROW = csv_row(id="bea", name="Bea Arthur", email="bea@example.com", org="Bea Works")


def test_csv_bad_rows_reject_atomically_with_original_line_numbers(make_web):
    w = make_web()
    import_csv(w, csv_text([ADA]))
    before = export_rows(w)
    r = import_csv(w, csv_text([ADA, GRACE, {**LINUS, "email": "bad"}, {**BEA_ROW, "email": "also bad"}]))
    assert "row 4" in page_text(r).lower() and "row 5" in page_text(r).lower()
    assert export_rows(w) == before


def test_binary_csv_is_explained_without_creating_contacts(make_web):
    w = make_web()
    r = w.post("/crm/import", {"file": (io.BytesIO(b"name,email\n\xff\x00,broken\n"), "contacts.csv")}, follow=True)
    assert r.status_code == 200
    assert "That file is not a UTF-8 CSV" in page_text(r)
    assert export_rows(w) == []


def test_csv_header_aliases_create_expected_contact(make_web):
    w = make_web()
    r = import_csv(w, "partner_name,email,firm\nAda Alias,alias@example.com,Alias Ventures\n")
    assert r.status_code == 200
    row, = export_rows(w)
    assert (row["name"], row["org"], row["email"]) == ("Ada Alias", "Alias Ventures", "alias@example.com")


def test_clearing_test_recipient_holds_sends_and_explains_it(make_web):
    w = ready_web(make_web, [ADA])
    r = w.post("/settings/keys", {"TEST_RECIPIENT_EMAIL": ""}, follow=True)
    assert not read_env(w.ws).get("TEST_RECIPIENT_EMAIL")
    assert re.search(r"test sends.*held.*set", page_text(r), re.I)
    send_test(w, "ada")
    assert w.transport.calls == []


def test_test_schedule_needs_no_live_phrase(make_web):
    w = ready_web(make_web, [ADA])
    when = dt.datetime.now() + dt.timedelta(minutes=10)
    r = w.post("/schedule-send", schedule_form([draft_row_id(w, "ada")], when, "test", ""), follow=True)
    assert r.status_code == 200
    row, = scheduled_rows(w)
    assert row["mode"] == "test"
    assert w.transport.calls == []


@pytest.mark.parametrize("path,data", [
    ("/settings/keys", {"OPENAI_MODEL": "round3-model"}),
    ("/settings/sender", sender_form()),
    ("/settings", {"TEST_RECIPIENT_EMAIL": TEST_INBOX}),
])
def test_settings_forms_redirect_to_settings(make_web, path, data):
    w = make_web(sender=True)
    r = w.post(path, data)
    assert r.status_code in (302, 303)
    assert urlsplit(r.location).path == "/settings"


@pytest.mark.parametrize("path", ["/crm/9999999999999999999999", "/crm/9223372036854775808", "/partners/99999999999999999999"])
def test_oversized_contact_id_is_404(make_web, path):
    assert make_web().get(path).status_code == 404


def test_unconfigured_worker_has_no_dead_dashboard_link(make_web):
    w = make_web()
    for path in ("/", "/crm", "/crm/tracking", "/settings"):
        r = w.get(path, follow_redirects=True)
        assert r.status_code == 200
        assert not re.search(r'''href\s*=\s*["']/dashboard(?:[?"'#])''', body(r), re.I)


def test_flash_consumed_once_and_absent_on_unrelated_and_error_pages(make_web):
    w = make_web()
    r = w.post("/settings/keys", {"OPENAI_MODEL": "round3-model"})
    destination = w.get(r.location)
    from web_gap_helpers import new_text
    message = new_text(w.text("/settings"), page_text(destination)).strip()
    assert message, "the save must show a flash once"
    assert message not in w.text("/crm")
    assert message not in page_text(w.get("/missing-round3"))


def test_identical_rejections_collapse_and_404_discards_pending_flash(make_web):
    w = make_web()
    for _ in range(9):
        r = w.post("/settings/keys", {"SEARXNG_URL": "javascript:bad"})
    text = page_text(w.get(r.location))
    # Compare with one rejection, avoiding assumptions about the precise wording.
    baseline = w.text("/settings")
    w.post("/settings/keys", {"SEARXNG_URL": "javascript:bad"})
    from web_gap_helpers import new_text
    one = new_text(baseline, w.text("/settings")).strip()
    assert one and text.count(one) == 1
    w.post("/settings/keys", {"SEARXNG_URL": "javascript:bad"})
    assert one not in page_text(w.get("/missing-round3"))
    assert one not in w.text("/settings")


def test_searxng_settings_row_and_http_url_persistence(make_web):
    w = make_web()
    assert "searxng" in w.text("/settings").lower()
    r = w.post("/settings/keys", {"SEARXNG_URL": "http://127.0.0.1:8080"})
    assert urlsplit(r.location).path == "/settings"
    assert read_env(w.ws)["SEARXNG_URL"] == "http://127.0.0.1:8080"
    r = w.post("/settings/keys", {"SEARXNG_URL": "file:///tmp/private"}, follow=True)
    assert r.status_code == 200
    assert read_env(w.ws)["SEARXNG_URL"] == "http://127.0.0.1:8080"


def test_no_gmail_refusal_points_to_settings(make_web):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX, "SENDER_EMAIL": SENDER_EMAIL}, sender=True, with_transport=False)
    seed_contacts(w, [ADA])
    w.post("/crm/drafts/write", {"provider": "template"}, follow=True)
    approve_drafts(w, ["ada"])
    from web_gap_helpers import new_text
    before = w.text(f"/partners/{partner_row_id(w, 'ada')}")
    response = send_test(w, "ada")
    text = new_text(before, page_text(response))
    assert "Settings" in text
    assert "vcoutreach auth gmail-web" not in text


@pytest.mark.parametrize("field,limit", [("name", 200), ("org", 200), ("role", 200), ("notes", 5000)])
def test_profile_text_caps_survive_export_and_reload(make_web, field, limit):
    w = ready_web(make_web, [ADA])
    pid = partner_row_id(w, "ada")
    value = "A" * (limit + 73)
    r = w.post(f"/crm/{pid}/profile", {field: value}, follow=True)
    assert r.status_code == 200
    # Both refusal and truncation satisfy a cap; a boundary-sized edit must work.
    stored = (sql(w, "SELECT notes FROM partners WHERE id=?", (pid,))[0][0] if field == "notes"
              else next(r for r in export_rows(w) if r["id"] == "ada")[field])
    assert len(stored or "") <= limit
    w.post(f"/crm/{pid}/profile", {field: "B" * limit}, follow=True)
    stored = (sql(w, "SELECT notes FROM partners WHERE id=?", (pid,))[0][0] if field == "notes"
              else next(r for r in export_rows(w) if r["id"] == "ada")[field])
    assert stored == "B" * limit


def test_contact_controls_and_bidi_are_stripped_and_export_has_no_nul(make_web):
    w = ready_web(make_web, [ADA])
    pid = partner_row_id(w, "ada")
    dirty = "Clean\x00\x01\x1b\x7f\u202e\u2066 Text"
    w.post(f"/crm/{pid}/profile", dict(name=dirty, org=dirty, role=dirty, notes=dirty), follow=True)
    row = next(r for r in export_rows(w) if r["id"] == "ada")
    assert (row["name"], row["org"], row["role"]) == ("Clean Text",) * 3
    assert sql(w, "SELECT notes FROM partners WHERE id=?", (pid,))[0][0] == "Clean Text"
    assert b"\x00" not in w.get("/crm/export.csv").data


@pytest.mark.parametrize("website", ["javascript:alert(1)", "file:///tmp/private", "ftp://example.com/file"])
def test_sender_website_requires_http_scheme(make_web, website):
    w = make_web(sender=True)
    before = (w.ws / "sender.json").read_text()
    r = w.post("/settings/sender", sender_form(website=website), follow=True)
    assert r.status_code == 200
    assert (w.ws / "sender.json").read_text() == before
