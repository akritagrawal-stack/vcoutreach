"""F1: first-run path on a fresh workspace (spec: outputs/ux_pass_20261021/fix_spec.md).

A new user imports contacts, clicks Write drafts, adds facts. Everything goes through the Flask test client,
TEST send mode, no network.
"""
from __future__ import annotations

from web_helpers import *  # noqa: F401,F403  (fixtures + autouse env isolation)
from web_helpers import (
    ADA, MINA, TEST_INBOX, body, contact_ref, csv_text, export_rows, import_csv, page_text, read_draft_file,
    seed_contacts,
)

from audit_fix_helpers import (
    NO_FACT_ROWS, dom, facts_of, find_form, flashes, has_dash, submit_form, write_drafts,
)


def _env():
    return {"SENDER_EMAIL": "sender@example.com", "TEST_RECIPIENT_EMAIL": TEST_INBOX}


def _draft_files(ws):
    root = ws / "drafts"
    return sorted(p for p in root.rglob("*.md")) if root.exists() else []


# --- Write drafts with no sender profile -----------------------------------------------------------------------


def test_write_drafts_without_sender_shows_plain_message_not_a_file_path(make_web):
    web = make_web(_env(), sender=False)
    seed_contacts(web, [ADA])
    page = body(write_drafts(web))
    shown = " ".join(t for t, _ in flashes(page))
    assert shown, "Write drafts without a sender profile must still say something"
    assert "sender.json" not in shown
    assert str(web.ws) not in shown
    assert "/var/folders" not in shown and "/tmp/" not in shown and "/private/" not in shown
    assert "add your sender profile first" in shown.lower()


def test_write_drafts_without_sender_links_to_the_sender_card_and_writes_nothing(make_web):
    web = make_web(_env(), sender=False)
    seed_contacts(web, [ADA])
    page = body(write_drafts(web))
    hrefs = [h for _, links in flashes(page) for h in links]
    assert any(h.startswith("/settings") for h in hrefs), f"flash has no link into Settings: {hrefs}"
    assert _draft_files(web.ws) == []


# --- Write drafts: honest counts --------------------------------------------------------------------------------


def test_write_drafts_names_how_many_need_facts(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, NO_FACT_ROWS)
    shown = " ".join(t for t, _ in flashes(body(write_drafts(web))))
    assert "Wrote 3 drafts, 3 need facts" in shown, shown


def test_write_drafts_links_each_contact_that_needs_facts(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA] + NO_FACT_ROWS[:2])
    page = body(write_drafts(web))
    hrefs = [h for _, links in flashes(page) for h in links]
    for row in NO_FACT_ROWS[:2]:
        ref = contact_ref(web, row["email"])
        assert any(h.rstrip("/").endswith(f"/crm/{ref}") or f"/crm/{ref}#" in h or f"/crm/{ref}?" in h
                   for h in hrefs), f"no link to {row['name']} (/crm/{ref}) in {hrefs}"
    ada_ref = contact_ref(web, ADA["email"])
    assert not any(h.rstrip("/").endswith(f"/crm/{ada_ref}") for h in hrefs), "Ada has facts and needs no link"


def test_write_drafts_with_all_facts_has_no_need_facts_suffix(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    shown = " ".join(t for t, _ in flashes(body(write_drafts(web))))
    assert "Wrote 1 draft" in shown
    assert "need facts" not in shown and "needs facts" not in shown


# --- Add facts on the contact page ---------------------------------------------------------------------------


def _facts_form(web, email):
    ref = contact_ref(web, email)
    root = dom(web, f"/crm/{ref}")
    form = find_form(root, textarea_like="fact")
    return ref, form


def test_contact_page_has_an_add_facts_form(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [MINA])
    ref, form = _facts_form(web, MINA["email"])
    assert form is not None, "no form with a facts textarea on the contact page"
    assert form.select_one('input[name="csrf_token"]') is not None
    assert form.select_one("button, input[type=submit]") is not None
    assert "fact" in form.get_text(" ", strip=True).lower() or "fact" in form.parent.get_text(" ", strip=True).lower()


def test_add_facts_one_per_line_saves_onto_the_existing_contact(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [MINA])
    ref, form = _facts_form(web, MINA["email"])
    assert form is not None
    name = next(t.get("name") for t in form.select("textarea") if "fact" in (t.get("name") or "").lower())
    submit_form(web, form, {name: "your talk on shipping weekly\nyour note on small teams"}, fallback_path=f"/crm/{ref}")
    saved = facts_of(web, MINA["email"])
    assert "your talk on shipping weekly" in saved and "your note on small teams" in saved, saved
    rows = [r for r in export_rows(web) if r["email"] == MINA["email"]]
    assert len(rows) == 1 and rows[0]["name"] == MINA["name"], "facts must save onto the existing contact"
    page = page_text(body(web.get(f"/crm/{ref}")))
    assert "your talk on shipping weekly" in page and "your note on small teams" in page


def test_add_facts_then_write_drafts_unblocks_the_contact(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [MINA])
    write_drafts(web)
    first = read_draft_file(web.ws, MINA["id"])
    assert first is not None and first.status != "ok"
    ref, form = _facts_form(web, MINA["email"])
    assert form is not None
    name = next(t.get("name") for t in form.select("textarea") if "fact" in (t.get("name") or "").lower())
    submit_form(web, form, {name: "your talk on shipping weekly"}, fallback_path=f"/crm/{ref}")
    web.post(web.url("drafts_write"), {"provider": "template", "overwrite": "1"}, follow=True)
    again = read_draft_file(web.ws, MINA["id"])
    assert again is not None and again.status == "ok", getattr(again, "status", None)


# --- CSV import help and empty state -------------------------------------------------------------------------


def test_csv_import_help_documents_the_facts_column(make_web):
    web = make_web(_env(), sender=True)
    root = dom(web, "/crm")
    area = root.select_one('textarea[name="csv_text"]')
    assert area is not None
    form = area.find_parent("form")
    visible = (area.get("placeholder", "") + " " + form.get_text(" ", strip=True)).lower()
    assert "facts" in visible, visible


def test_empty_csv_error_is_sentence_case_and_names_expected_columns(make_web):
    web = make_web(_env(), sender=True)
    shown = [t for t, _ in flashes(body(import_csv(web, "")))]
    assert shown, "an empty CSV must produce a message"
    text = shown[0]
    assert text[0].isupper(), text
    assert not text.isupper()
    lowered = text.lower()
    assert "name" in lowered and "email" in lowered, text
    assert not has_dash(text)


def test_crm_empty_state_links_to_the_import_control(make_web):
    web = make_web(_env(), sender=True)
    root = dom(web, "/crm")
    targets = []
    for a in root.select("a[href]"):
        href = a["href"]
        label = a.get_text(" ", strip=True).lower()
        if "#" not in href or href.split("#", 1)[0] not in ("", "/crm"):
            continue
        if "import" in label or "csv" in label:
            targets.append(href.split("#", 1)[1])
    ok = False
    for frag in targets:
        el = root.find(id=frag)
        if el is None or frag == "main-content":
            continue
        if el.name in ("textarea", "input") and (el.get("name") in ("csv_text", "file")):
            ok = True
        elif el.select_one('textarea[name="csv_text"], input[name="file"]'):
            ok = True
    assert ok, f"no import/CSV link on the empty /crm that jumps to the CSV import control; fragments: {targets}"


def test_import_then_write_drafts_end_to_end_message_is_honest(make_web):
    """The real first-run click path: paste a CSV with facts for one contact and none for another."""
    web = make_web(_env(), sender=True)
    import_csv(web, csv_text([ADA, MINA]))
    shown = " ".join(t for t, _ in flashes(body(write_drafts(web))))
    assert "Wrote 2 drafts, 1 needs fact" in shown or "Wrote 2 drafts, 1 needs facts" in shown, shown
    assert not has_dash(shown)
