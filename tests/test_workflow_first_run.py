"""First run workflow pass (plan items P0-1 to P2-11), observed through the Flask test client.

Spec only: the Plan section of ux_analysis_and_plan.md. A throwaway workspace per test, obviously fake values,
no network, and the injected RecordingTransport as the only way mail could leave. No dash characters in this
file (EM_DASH and EN_DASH come from web_helpers).
"""
from __future__ import annotations

import json
import re
import sqlite3

import pytest
from bs4 import BeautifulSoup, Tag

from web_helpers import *  # noqa: F401,F403
from web_helpers import (ADA, EM_DASH, EN_DASH, MINA, SENDER_EMAIL, TEST_INBOX, approve_drafts, banner_mode, body,
                         go_live, journal_lines, mark_verified, page_text, post_keys, read_draft_file, seed_contacts,
                         send_live, send_test)

MAIN_PAGES = ["/crm", "/crm/drafts", "/crm/analytics", "/crm/send-plan", "/settings", "/partners", "/replies",
              "/scheduled", "/candidates", "/email-verification", "/crm/tracking", "/enrichment"]
CRED_WORDS = ("50+ meetings", "$600K")
STEP_KEYS = ["gmail", "test_recipient", "contact", "draft", "approve", "send_test"]


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------

def dom(web, path, follow=False):
    response = web.get(path, follow_redirects=follow)
    assert response.status_code == 200, f"GET {path} returned {response.status_code}"
    return BeautifulSoup(body(response), "html.parser")


def first_body_content(s):
    for child in s.body.children:
        if not isinstance(child, Tag):
            continue
        if child.name in ("script", "noscript", "style"):
            continue
        if "skip-link" in (child.get("class") or []):
            continue
        return child
    return None


def before(s, a, b):
    """True when element a comes before element b in document order."""
    order = {id(e): i for i, e in enumerate(s.find_all(True))}
    return order[id(a)] < order[id(b)]


def connect_gmail(web):
    web.app.extensions["workspace"].path("data", ".gmail_token.json").write_text(
        json.dumps({"refresh_token": "fake-refresh", "token": "fake-access"}))


def db_partner_id(web, email):
    conn = sqlite3.connect(str(web.ws / "data" / "outreach.db"))
    try:
        row = conn.execute("SELECT id FROM partners WHERE lower(email)=?", (email.lower(),)).fetchone()
    finally:
        conn.close()
    assert row, f"no partner row for {email}"
    return str(row[0])


def write_drafts(web):
    return web.post(web.url("drafts_write"), {"provider": "template"}, follow=True)


def next_step(web, path="/crm"):
    """(key, label text, href, whole banner text) of the Next step banner, or None when it is not rendered."""
    banner = dom(web, path).select_one("#nextStep")
    if banner is None:
        return None
    link = banner.select_one("a[data-next-step]")
    assert link is not None, "the Next step banner must carry its call to action link"
    return link["data-next-step"], link.get_text(" ", strip=True), link["href"], " ".join(banner.get_text().split())


def add_enrichment_fact(web, email):
    """Record one researched fact for a contact, the state a finished Enrich leaves behind."""
    facts = [{"type": "thesis_quote", "text": "Ada wrote an essay on how small teams ship weekly",
              "source_url": "https://example.com/essay", "confidence": 0.9}]
    conn = sqlite3.connect(str(web.ws / "data" / "outreach.db"))
    try:
        conn.execute("INSERT INTO enrichment(partner_id, source, url, facts_json, confidence) VALUES (?,?,?,?,?)",
                     (int(db_partner_id(web, email)), "web_search", "https://example.com/essay", json.dumps(facts), 0.9))
        conn.commit()
    finally:
        conn.close()


def ready_web(send_web, rows=None, **kw):
    """A workspace that has a connected Gmail, a test recipient, contacts and approved drafts (TEST mode)."""
    web = send_web(rows, **kw)
    connect_gmail(web)
    return web


def set_deck(web, path):
    settings = web.ws / "data" / "workspace-settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"pitch_deck_path": str(path)}))


# ---------------------------------------------------------------------------------------------------------------
# P0-1  The global Next step banner, advancing through the whole first run
# ---------------------------------------------------------------------------------------------------------------

def test_fresh_workspace_starts_at_step_one_connect_gmail(make_web):
    web = make_web()
    key, label, href, text = next_step(web)
    assert (key, href) == ("gmail", "/settings#svc-gmail")
    assert "Step 1 of 6" in text
    assert "Connect Gmail" in text + " " + label


def test_the_next_step_banner_advances_through_all_six_steps_and_then_goes_away(make_web):
    web = make_web({"SENDER_EMAIL": SENDER_EMAIL}, sender=True)
    key, _, href, text = next_step(web)
    assert (key, href, "Step 1 of 6" in text) == ("gmail", "/settings#svc-gmail", True)

    connect_gmail(web)
    key, _, href, text = next_step(web)
    assert (key, href) == ("test_recipient", "/settings#svc-test-recipient")
    assert "Step 2 of 6" in text and "Set your test recipient" in text

    assert post_keys(web, {"TEST_RECIPIENT_EMAIL": TEST_INBOX}, follow=False).status_code == 302
    key, _, href, text = next_step(web)
    assert (key, href) == ("contact", "/crm/new")
    assert "Step 3 of 6" in text and "Add your first contact" in text

    seed_contacts(web, [ADA])
    key, _, href, text = next_step(web)
    assert (key, href) == ("draft", "/crm/drafts#writeDrafts")
    assert "Step 4 of 6" in text and "Write drafts" in text

    write_drafts(web)
    key, _, href, text = next_step(web)
    assert (key, href) == ("approve", "/crm/drafts")
    assert "Step 5 of 6" in text and "Review and approve a draft" in text

    approve_drafts(web, ["ada"])
    key, _, href, text = next_step(web)
    assert (key, href) == ("send_test", f"/partners/{db_partner_id(web, 'ada@example.com')}")
    assert "Step 6 of 6" in text and "Send a test email" in text

    mark_verified(web, ["ada@example.com"])
    assert send_test(web, "ada").status_code == 200
    assert len(web.transport.calls) == 1
    for path in ("/crm", "/crm/drafts", "/partners"):
        assert next_step(web, path) is None, f"the banner must disappear on {path} once all six steps are met"


@pytest.mark.parametrize("path", ["/crm", "/crm/drafts", "/partners"])
def test_the_next_step_banner_is_global_and_sits_below_the_test_mode_banner(make_web, path):
    web = make_web()
    s = dom(web, path)
    assert s.select_one("#nextStep") is not None, f"{path} should show the Next step banner on a fresh workspace"
    first = first_body_content(s)
    assert "send-banner" in first.get("class", []) and "TEST MODE" in first.get_text()


def test_the_next_step_banner_is_not_repeated_on_settings_where_the_step_is_done_on_the_page(make_web):
    web = make_web()
    assert dom(web, "/settings").select_one("#nextStep") is None, "step gmail"
    connect_gmail(web)
    assert next_step(web)[0] == "test_recipient"
    assert dom(web, "/settings").select_one("#nextStep") is None, "step test_recipient"


def test_the_next_step_logic_survives_a_corrupt_journal(send_web):
    web = ready_web(send_web, [ADA])
    journal = web.ws / ".vcoutreach" / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b'{"broken json\n\x00\xff\xfe\n[1,2\n{"also": }\n')
    for path in ("/crm", "/crm/drafts", "/partners", "/settings"):
        assert web.get(path).status_code == 200, f"{path} must not fail on a corrupt journal"
    key, *_ = next_step(web)
    assert key in STEP_KEYS


def test_the_next_step_logic_survives_a_corrupt_journal_on_a_fresh_workspace(make_web):
    web = make_web()
    journal = web.ws / ".vcoutreach" / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not json at all\n\n{\n", encoding="utf-8")
    assert web.get("/crm").status_code == 200
    assert next_step(web)[0] == "gmail"


# ---------------------------------------------------------------------------------------------------------------
# P0-2  The Drafts page leads with Write drafts
# ---------------------------------------------------------------------------------------------------------------

def test_write_drafts_form_is_visible_busy_aware_and_offers_replace(make_web):
    s = dom(make_web(), "/crm/drafts")
    form = s.select_one('form[action="/crm/drafts/write"]')
    assert form is not None
    assert form.find_parent("details") is None, "Write drafts must not hide inside a details element"
    assert form.get("data-busy"), "the slow Write drafts action needs a data-busy message"
    box = form.select_one('input[name="overwrite"][value="1"]')
    assert box is not None and box.get("type") == "checkbox"
    assert "Replace existing drafts" in form.get_text(" ")


@pytest.mark.parametrize("with_drafts", [False, True])
def test_write_drafts_comes_before_the_template_and_deck_panel(send_web, make_web, with_drafts):
    web = send_web([ADA]) if with_drafts else make_web()
    s = dom(web, "/crm/drafts")
    write, panel = s.select_one("#writeDrafts"), s.select_one("#templateDeckPanel")
    assert write is not None and panel is not None
    assert before(s, write, panel)


@pytest.mark.parametrize("with_drafts", [False, True])
def test_the_credibility_sentence_appears_once_and_before_write_drafts(send_web, make_web, with_drafts):
    web = send_web([ADA]) if with_drafts else make_web()
    markup = body(web.get("/crm/drafts"))
    text = page_text(markup)
    for word in CRED_WORDS:
        assert text.count(word) == 1, f"{word!r} must appear exactly once on the Drafts page"
    anchor = re.search(r"""id=["']writeDrafts["']""", markup)
    assert anchor and markup.find("50+ meetings") < anchor.start()


def test_the_credibility_sentence_is_never_baked_into_a_draft_body(send_web):
    web = send_web([ADA, GRACE, LINUS])
    files = sorted((web.ws / "drafts").rglob("*.md"))
    assert files, "drafts should have been written"
    for path in files:
        assert "50+ meetings" not in path.read_text(encoding="utf-8"), path.name
    for area in dom(web, "/crm/drafts/ada").find_all("textarea"):
        assert "50+ meetings" not in area.get_text() and "$600K" not in area.get_text()


def test_the_drafting_explainer_says_a_template_draft_comes_first(make_web):
    how = dom(make_web(), "/crm/drafts").select_one("#draftingHow")
    assert how is not None
    assert "creates a template draft first" in " ".join(how.get_text().split())


def test_the_deck_panel_is_open_before_any_draft_and_collapses_after(send_web, make_web):
    fresh = dom(make_web(), "/crm/drafts").select_one("#templateDeckPanel")
    assert fresh.name == "section"
    panel = dom(send_web([ADA]), "/crm/drafts").select_one("#templateDeckPanel")
    assert panel.name == "details" and not panel.has_attr("open")


def test_the_drafts_table_headers_and_empty_state_are_actionable(make_web):
    s = dom(make_web(), "/crm/drafts")
    heads = [th.get_text(" ", strip=True) for th in s.select("table th")]
    assert heads == ["Contact", "Subject", "Status", "Grounding", "Issues", "Words", "Action"]
    cell = next((td for td in s.select("table td") if "No drafts yet." in td.get_text()), None)
    assert cell is not None
    assert "No drafts yet. Add a contact, then use Write drafts above." in " ".join(cell.get_text().split())
    assert cell.select_one('a[href="/crm/new"]') is not None


def test_the_drafts_table_keeps_its_headers_when_drafts_exist(send_web):
    s = dom(send_web([ADA]), "/crm/drafts")
    assert [th.get_text(" ", strip=True) for th in s.select("table th")] == [
        "Contact", "Subject", "Status", "Grounding", "Issues", "Words", "Action"]
    assert not any("No drafts yet." in td.get_text() for td in s.select("table td"))


# ---------------------------------------------------------------------------------------------------------------
# P0-3  Test recipient lives in Settings and drives the banner
# ---------------------------------------------------------------------------------------------------------------

def test_settings_has_one_visible_test_recipient_card_and_tile(make_web):
    s = dom(make_web(), "/settings")
    card = s.select_one("#svc-test-recipient")
    assert card is not None and card.find_parent("details") is None
    inputs = s.select('input[name="TEST_RECIPIENT_EMAIL"]')
    assert len(inputs) == 1 and inputs[0].find_parent("details") is None
    assert s.select_one('a[href="#svc-test-recipient"]') is not None


def test_a_fresh_workspace_banner_asks_for_a_test_recipient(make_web):
    s = dom(make_web(), "/crm")
    first = first_body_content(s)
    assert "send-banner" in first["class"] and "mode-test" in first["class"]
    assert "TEST MODE" in first.get_text()
    link = next((a for a in first.find_all("a") if "Set test recipient" in a.get_text()), None)
    assert link is not None and link["href"] == "/settings#svc-test-recipient"


def test_saving_the_test_recipient_updates_the_banner_and_the_settings_card(make_web):
    web = make_web()
    response = post_keys(web, {"TEST_RECIPIENT_EMAIL": "me@example.com"}, follow=False)
    assert response.status_code == 302
    for path in ("/crm", "/crm/drafts", "/settings"):
        first = first_body_content(dom(web, path))
        assert "mode-test" in first["class"] and "TEST MODE" in first.get_text()
        assert "Test mailbox: me@example.com" in " ".join(first.get_text().split()), path
        assert not any("Set test recipient" in a.get_text() for a in first.find_all("a")), path
    assert dom(web, "/settings").select_one('#svc-test-recipient[data-state="ok"]') is not None


def test_going_live_still_needs_the_typed_phrase_and_still_shows_the_live_banner(make_web):
    web = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX, "SENDER_EMAIL": SENDER_EMAIL})
    web.post("/toggle-send-mode", {"mode": "live", "confirm_text": "nope"}, follow=True)
    assert banner_mode(web, "/crm") == "test"
    response = web.post("/toggle-send-mode", {"mode": "live", "confirm_text": "GO LIVE"}, follow=True)
    assert response.status_code == 200
    assert banner_mode(web, "/crm") == "live"


# ---------------------------------------------------------------------------------------------------------------
# P0-4 and P1-6  The partner page: grouping, busy states, one primary button by state
# ---------------------------------------------------------------------------------------------------------------

def partner_page(web, email):
    return dom(web, f"/partners/{db_partner_id(web, email)}")


def test_enrich_is_busy_aware_and_comes_first_in_the_prepare_group(make_web):
    web = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX, "SENDER_EMAIL": SENDER_EMAIL}, sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    s = partner_page(web, "ada@example.com")
    enrich = s.select_one('form[action$="/enrich"]')
    approve = s.select_one('form[action$="/approve"]')
    assert enrich is not None and approve is not None
    assert "30 seconds" in (enrich.get("data-busy") or "")
    assert before(s, enrich, approve)
    prepare, review = s.select_one('[aria-label="Prepare"]'), s.select_one('[aria-label="Review and send"]')
    assert prepare is not None and review is not None and before(s, prepare, review)


def test_the_test_mode_send_form_is_busy_aware(send_web):
    web = send_web([ADA])
    forms = [f for f in partner_page(web, "ada@example.com").select(".pd-actions form")
             if "Send test email" in f.get_text()]
    assert len(forms) == 1 and forms[0].get("data-busy")


def test_live_mode_keeps_the_typed_confirmation_fields(send_web):
    web = send_web([ADA])
    go_live(web)
    text = " ".join(partner_page(web, "ada@example.com").get_text().split())
    assert "SEND TO ada@example.com" in text
    assert "SCHEDULE LIVE FOR" in text
    s = partner_page(web, "ada@example.com")
    assert s.select('input[name="confirm_text"]'), "typed confirmation inputs must remain in LIVE mode"


def test_the_partner_page_explains_the_template_draft_once_and_has_no_inline_script(make_web):
    web = make_web()
    seed_contacts(web, [ADA])
    s = partner_page(web, "ada@example.com")
    assert page_text(str(s)).count("Creates a template draft first if none exists.") == 1
    assert [t for t in s.find_all("script") if not t.get("src")] == []


def test_a_contact_without_facts_points_at_enrich_under_prepare(make_web):
    web = make_web()
    seed_contacts(web, [MINA])
    text = " ".join(partner_page(web, "mina@example.com").get_text().split())
    assert "Use Enrich under Prepare above." in text


def test_a_contact_without_facts_has_enrich_as_the_single_primary_action(make_web):
    web = make_web()
    seed_contacts(web, [MINA])
    s = partner_page(web, "mina@example.com")
    primaries = s.select(".pd-actions .crm-button-primary")
    assert len(primaries) == 1
    form = primaries[0].find_parent("form")
    assert form is not None and form["action"].endswith("/enrich")
    assert primaries[0].get("type", "submit") == "submit"


def test_an_approved_draft_in_test_mode_makes_send_test_email_the_single_primary_action(send_web):
    web = send_web([ADA])
    add_enrichment_fact(web, "ada@example.com")
    s = partner_page(web, "ada@example.com")
    primaries = s.select(".pd-actions .crm-button-primary")
    assert len(primaries) == 1
    form = primaries[0].find_parent("form")
    assert form is not None
    assert "Send test email" in primaries[0].get_text()


# ---------------------------------------------------------------------------------------------------------------
# P1-5, P1-6, P1-7  Overview empty state, toolbar primary, Settings tile order
# ---------------------------------------------------------------------------------------------------------------

def test_the_empty_overview_shows_a_three_step_checklist_and_keeps_the_sample_loader(make_web):
    s = dom(make_web(), "/crm")
    steps = s.select(".crm-empty-state ol.crm-empty-steps > li")
    assert len(steps) == 3
    hrefs = {a["href"] for a in s.select(".crm-empty-state ol.crm-empty-steps a[href]")}
    assert {"/settings#svc-gmail", "/crm/new", "/crm/drafts#writeDrafts"} <= hrefs
    assert s.select_one('form[action="/sample/load"]') is not None


def test_the_empty_state_goes_away_once_a_contact_exists(make_web):
    web = make_web()
    seed_contacts(web, [ADA])
    assert dom(web, "/crm").select_one(".crm-empty-state ol.crm-empty-steps") is None
    assert dom(web, "/crm").select_one('.crm-empty-state form[action="/sample/load"]') is None


def test_the_overview_toolbar_has_one_primary_button_that_follows_the_state(make_web):
    web = make_web()
    fresh = dom(web, "/crm").select(".crm-toolbar .crm-button-primary")
    assert len(fresh) == 1 and fresh[0].get("href") == "/crm/new"
    seed_contacts(web, [ADA])
    after = dom(web, "/crm").select(".crm-toolbar .crm-button-primary")
    assert len(after) == 1 and "Review drafts" in after[0].get_text()


def test_settings_tiles_lead_with_gmail_then_test_recipient_and_every_tile_resolves(make_web):
    s = dom(make_web(), "/settings")
    tiles = [t.get("href") for t in s.select("a.svc-tile")]
    assert tiles[:2] == ["#svc-gmail", "#svc-test-recipient"]
    ids = {e["id"] for e in s.find_all(id=True)}
    for href in tiles:
        assert href.startswith("#") and href[1:] in ids, f"tile link {href} points at nothing"


def test_the_gmail_hint_hands_over_to_the_test_recipient(make_web):
    card = dom(make_web(), "/settings").select_one("#svc-gmail")
    assert "set your test recipient below" in " ".join(card.get_text().split()).lower()


# ---------------------------------------------------------------------------------------------------------------
# P2-8  Navigation: at most nine links, nothing unreachable
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/crm", "/crm/drafts", "/crm/analytics", "/partners", "/replies", "/settings",
                                  "/scheduled", "/candidates"])
def test_the_top_nav_has_at_most_nine_links(make_web, path):
    links = dom(make_web(), path).select(".nav-links a")
    assert 0 < len(links) <= 9, [a.get_text(strip=True) for a in links]


def test_pages_removed_from_the_nav_are_linked_from_their_parent_page_and_still_load(make_web):
    web = make_web()
    partners = {a.get("href") for a in dom(web, "/partners").find_all("a")}
    assert "/candidates" in partners and "/email-verification" in partners
    assert "/crm/tracking" in {a.get("href") for a in dom(web, "/crm/analytics").find_all("a")}
    for path in ("/candidates", "/email-verification", "/crm/tracking"):
        assert web.get(path).status_code == 200, path


@pytest.mark.parametrize("path", ["/crm", "/crm/drafts", "/replies", "/settings"])
def test_the_nav_still_marks_the_current_page(make_web, path):
    current = dom(make_web(), path).select('a.nav-link[aria-current="page"], .nav-links a[aria-current="page"]')
    assert len(current) >= 1
    assert any(path == a["href"].split("#")[0] or path.startswith(a["href"].split("#")[0]) for a in current)


# ---------------------------------------------------------------------------------------------------------------
# P2-9  No inline styles in Drafts, the editor and the deck panel; /crm/deck unchanged
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/crm/drafts", "/crm/drafts/ada"])
def test_drafts_and_editor_use_classes_not_inline_styles(send_web, path):
    s = dom(send_web([ADA]), path)
    assert [str(e)[:80] for e in s.find_all(style=True)] == []


def test_the_empty_drafts_page_has_no_inline_styles(make_web):
    assert dom(make_web(), "/crm/drafts").find_all(style=True) == []


def test_the_deck_panel_links_to_the_package_settings_and_to_a_configured_deck(make_web):
    web = make_web()
    deck = web.ws / "attachments" / "deck.pdf"
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"%PDF-1.4 fake deck")
    panel = dom(web, "/crm/drafts").select_one("#templateDeckPanel")
    assert panel.select_one('a[href="/settings#sec-package"]') is not None
    assert "Edit in Settings" in panel.get_text()
    assert not any("Open deck" in a.get_text() for a in panel.find_all("a"))
    set_deck(web, deck)
    panel = dom(web, "/crm/drafts").select_one("#templateDeckPanel")
    assert panel.select_one('a[href="/settings#sec-package"]') is not None
    assert any("Open deck" in a.get_text() for a in panel.find_all("a"))


def test_crm_deck_is_404_without_a_deck_and_serves_only_files_inside_attachments(make_web, tmp_path):
    web = make_web()
    assert web.get("/crm/deck").status_code == 404
    inside = web.ws / "attachments" / "deck.pdf"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"%PDF-1.4 inside deck")
    set_deck(web, inside)
    served = web.get("/crm/deck")
    assert served.status_code == 200 and served.data == b"%PDF-1.4 inside deck"
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4 outside deck")
    set_deck(web, outside)
    refused = web.get("/crm/deck")
    assert refused.status_code != 200 and b"outside deck" not in refused.data


# ---------------------------------------------------------------------------------------------------------------
# P2-10 and P2-11  Dead links and cache busting
# ---------------------------------------------------------------------------------------------------------------

def test_email_verification_no_longer_links_to_the_knowledge_page(make_web):
    web = make_web()
    s = dom(web, "/email-verification")
    hrefs = [a.get("href", "") for a in s.find_all("a")]
    assert not any("/knowledge#" in h for h in hrefs)
    settings_links = [h for h in hrefs if h.startswith("/settings#")]
    assert settings_links, "the page should send people to the Settings section that holds the key"
    ids = {e["id"] for e in dom(web, "/settings").find_all(id=True)}
    for href in settings_links:
        assert href.split("#", 1)[1] in ids, f"{href} points at nothing on /settings"


@pytest.mark.parametrize("path", ["/crm", "/crm/drafts", "/settings"])
def test_every_stylesheet_and_script_is_cache_busted_and_reachable(make_web, path):
    web = make_web()
    s = dom(web, path)
    urls = [t["href"] for t in s.find_all("link", rel="stylesheet") if t.get("href", "").startswith("/")]
    urls += [t["src"] for t in s.find_all("script", src=True) if t["src"].startswith("/")]
    assert urls, "the page should load local assets"
    for url in urls:
        assert re.search(r"[?&]v=[^&]+", url), f"{url} has no ?v= cache buster"
        assert web.get(url).status_code == 200, url


# ---------------------------------------------------------------------------------------------------------------
# Cross cutting: no dash characters, no live path without typed confirmation
# ---------------------------------------------------------------------------------------------------------------

def test_no_main_page_renders_an_em_or_en_dash(send_web):
    web = send_web([ADA, GRACE])
    paths = MAIN_PAGES + ["/crm/drafts/ada", f"/partners/{db_partner_id(web, 'ada@example.com')}"]
    for path in paths:
        response = web.get(path, follow_redirects=True)
        assert response.status_code < 400, path
        markup = body(response)
        assert EM_DASH not in markup and EN_DASH not in markup, f"{path} renders an em or en dash"


def test_no_dash_characters_on_a_fresh_workspace_either(make_web):
    web = make_web()
    for path in MAIN_PAGES:
        response = web.get(path, follow_redirects=True)
        assert response.status_code < 400, path
        assert EM_DASH not in body(response) and EN_DASH not in body(response), path


def test_a_test_mode_send_never_reaches_a_real_contact_address(send_web):
    web = send_web([ADA, GRACE])
    connect_gmail(web)
    assert send_test(web, "ada").status_code == 200
    web.post(f"/partners/{db_partner_id(web, 'grace@example.com')}/send", {}, follow=True)
    assert web.transport.calls, "the TEST send should have been recorded"
    for call in web.transport.calls:
        assert call.recipients == [TEST_INBOX], call.recipients
    assert not any(e in str(c.recipients) for c in web.transport.calls
                   for e in ("ada@example.com", "grace@example.com"))


@pytest.mark.parametrize("phrase", [None, "", "SEND", "send to ada@example.com", "GO LIVE"])
def test_a_live_send_needs_the_exact_typed_confirmation(send_web, phrase):
    web = send_web([ADA])
    go_live(web)
    before_calls = len(web.transport.calls)
    send_live(web, "ada", phrase)
    assert len(web.transport.calls) == before_calls, f"typed {phrase!r} must not release a live send"
    assert not any("ada@example.com" in str(c.recipients) for c in web.transport.calls)
