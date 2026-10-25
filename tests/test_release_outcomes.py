"""Release outcomes, observed the way a first time user and a connected agent would see them.

Spec only: the release brief (sample figures, OAuth first Gmail card, the seven step banner, the readiness
checklist and test email preview, the sender mini form, the campaign kind picker, the UI contract, the one time
link message, the MCP surface and dash hygiene). A throwaway workspace per test, obviously fake values, no network,
and the injected RecordingTransport as the only way mail could leave. No dash characters in this file (EM_DASH and
EN_DASH come from web_helpers).
"""
from __future__ import annotations

import json
import re

import pytest
from bs4 import BeautifulSoup

from mcp_helpers import assert_ok, mcp, plain  # noqa: F401
from web_helpers import *  # noqa: F401,F403
from web_helpers import (ADA, EM_DASH, EN_DASH, GRACE, SENDER_EMAIL, TEST_INBOX, Web, body, get_token, page_text,
                         partner_id, read_draft_file, seed_contacts)

PUBLIK_LABEL = "Optional: connect the Publik API for AI drafts"
ALT_SUMMARY = "Alternative: use a Gmail app password instead"
PREVIEW_SENTENCE = "This goes only to your test recipient."
USED_LINK_MESSAGE = "This one-time link was already used"
FUNDRAISING_PROOF = "50+ meetings"
RAISE_AMOUNT = "$600K"
SECRET_WORDS = re.compile(r"(?i)(secret|password|passwd|token|api_?key|credential)")
HYGIENE_PAGES = ["/", "/crm", "/crm/drafts", "/settings", "/setup/ai", "/crm/send-plan"]


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------

def soup(web, path):
    response = web.get(path, follow_redirects=True)
    assert response.status_code == 200, f"GET {path} returned {response.status_code}"
    return BeautifulSoup(body(response), "html.parser")


def squash(node):
    return " ".join(node.get_text(" ").split())


def connect_gmail(web):
    web.app.extensions["workspace"].path("data", ".gmail_token.json").write_text(
        json.dumps({"refresh_token": "fake-refresh", "token": "fake-access"}))


def skip_publik(web):
    assert web.post("/settings/publik-recommendation", {"state": "skipped"}, follow=False).status_code in (200, 302)


def set_deck(web, name="mydeck.pdf"):
    deck = web.ws / "attachments" / name
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"%PDF-1.4 fake deck for tests\n")
    settings = web.ws / "data" / "workspace-settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    saved = json.loads(settings.read_text()) if settings.exists() else {}
    saved["pitch_deck_path"] = str(deck)  # keep the other saved choices, such as the campaign type
    settings.write_text(json.dumps(saved))
    return name


def banner(web, path="/crm"):
    return soup(web, path).select_one("#nextStep")


def banner_link(web, path="/crm"):
    node = banner(web, path)
    return None if node is None else node.select_one("a[data-next-step]")


def ready_web(send_web, rows=None, **kw):
    """Connected Gmail, test recipient, sender profile, verified contacts and approved drafts."""
    web = send_web(rows if rows is not None else [ADA], **kw)
    connect_gmail(web)
    return web


def sender_figures(web):
    return (web.ws / "sender.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------------------
# 1  The sample sender carries the owner approved figures and nothing stale
# ---------------------------------------------------------------------------------------------------------------

def test_the_sample_sender_carries_the_approved_figures(sample_web):
    text = sender_figures(sample_web)
    for needle in ("25M+ views on our open source videos", "10K users", "2,400 Discord members"):
        assert needle in text, needle


def test_the_sample_sender_has_no_stale_figures_and_no_dollar_amount(sample_web):
    text = sender_figures(sample_web)
    for stale in ("7,500", "2,100", "1,592", "1,500", "10,000 registered"):
        assert stale not in text, stale
    assert "$" not in text


def test_the_sample_sender_keeps_a_calendly_placeholder_not_a_real_link(sample_web):
    text = sender_figures(sample_web)
    assert "calendly.com/your-handle/30min" in text
    links = re.findall(r"calendly\.com/[^\s\"'<>)]*", text)
    assert links and all(link.startswith("calendly.com/your-handle") for link in links), links


# ---------------------------------------------------------------------------------------------------------------
# 2  The Settings Gmail card is OAuth first, the app password is a closed alternative
# ---------------------------------------------------------------------------------------------------------------

def gmail_card(web):
    card = soup(web, "/settings").select_one("#svc-gmail")
    assert card is not None, "Settings must have the Gmail card at #svc-gmail"
    return card


def outside_details(card):
    """The card with every <details> removed: what a user sees without opening anything."""
    copy = BeautifulSoup(str(card), "html.parser")
    for node in copy.find_all("details"):
        node.decompose()
    return copy


def test_the_gmail_card_leads_with_the_oauth_client_file_chooser_and_connect_gmail(web):
    visible = outside_details(gmail_card(web))
    chooser = visible.select_one('input[type="file"]')
    assert chooser is not None, "the OAuth client file chooser must be visible without opening anything"
    text = squash(visible)
    assert "Connect Gmail" in text
    assert "OAuth client file" in text


def test_the_gmail_card_explains_the_connection_in_three_plain_steps(web):
    text = squash(outside_details(gmail_card(web)))
    positions = [text.find(p) for p in ("Download your Google OAuth client file", "Choose it here", "Connect Gmail")]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), "the three steps must read in order: download, choose, connect"
    assert "never sees your password" in text


def test_the_app_password_form_lives_only_inside_one_closed_alternative_details(web):
    card = gmail_card(web)
    details = card.find_all("details")
    assert len(details) == 1
    alt = details[0]
    assert not alt.has_attr("open"), "the alternative must start closed"
    summary = alt.find("summary")
    assert summary is not None and " ".join(summary.get_text().split()) == ALT_SUMMARY
    assert alt.select_one('input[type="password"]') is not None
    visible = outside_details(card)
    assert visible.select_one('input[type="password"]') is None, "a password field must not show outside the alternative"
    assert "app password" not in squash(visible).lower().replace(ALT_SUMMARY.lower(), "")


# ---------------------------------------------------------------------------------------------------------------
# 3  The Next step banner: Gmail first, the optional Publik API last, no mailbox wording, no early LIVE toggle
# ---------------------------------------------------------------------------------------------------------------

def test_a_fresh_workspace_banner_starts_with_gmail_as_step_one_of_seven(make_web):
    web = make_web()
    link = banner_link(web)
    assert link is not None
    assert link["data-next-step"] == "gmail" and link["href"] == "/settings#svc-gmail"
    assert "Step 1 of 7" in squash(banner(web))


def test_the_publik_api_is_the_last_step_and_is_labelled_optional(send_web):
    web = ready_web(send_web)
    assert web.post(f"/partners/{partner_id(web, 'ada')}/send-test", {}, follow=True).status_code == 200
    node = banner(web)
    link = banner_link(web)
    assert link is not None and link["data-next-step"] == "publik_api"
    text = squash(node)
    assert "Step 7 of 7" in text
    assert PUBLIK_LABEL in text
    assert link["href"] == "/settings#svc-publik"


def test_the_banner_has_no_set_up_with_ai_link_and_no_publik_skip_form(send_web):
    for web in (send_web(None, with_recipient=False, drafts=False), ready_web(send_web)):
        node = banner(web)
        assert node is not None
        assert node.find("form") is None, "the banner must not carry a form"
        assert "set up with ai" not in squash(node).lower()


@pytest.mark.parametrize("path", ["/crm", "/crm/drafts", "/settings", "/"])
def test_no_page_says_test_mailbox_it_says_test_recipient(make_web, path):
    web = make_web()
    assert "test mailbox" not in page_text(web.get(path, follow_redirects=True)).lower()


def test_the_banner_says_test_recipient_when_a_recipient_is_the_next_thing_to_set(make_web):
    web = make_web()
    connect_gmail(web)
    link = banner_link(web)
    assert link["data-next-step"] == "test_recipient"
    text = squash(banner(web))
    assert "test recipient" in text.lower() and "test mailbox" not in text.lower()


def test_the_live_mode_toggle_is_hidden_while_any_step_remains_and_returns_when_all_are_done(send_web):
    web = send_web([ADA], with_recipient=False, drafts=False)
    assert banner_link(web) is not None
    assert "switch to live" not in page_text(web.get("/crm")).lower()

    web = ready_web(send_web)
    assert "switch to live" not in page_text(web.get("/crm")).lower(), "step 6 still remains"
    web.post(f"/partners/{partner_id(web, 'ada')}/send-test", {}, follow=True)
    assert banner_link(web)["data-next-step"] == "publik_api"
    assert "switch to live" not in page_text(web.get("/crm")).lower(), "the optional Publik step still remains"

    skip_publik(web)
    assert banner(web) is None, "all seven steps are answered, the banner goes away"
    assert "switch to live" in page_text(web.get("/crm")).lower()


# ---------------------------------------------------------------------------------------------------------------
# 4  /crm/drafts on a fresh workspace: a four row readiness checklist and no send test form
# ---------------------------------------------------------------------------------------------------------------

def readiness(web):
    panel = soup(web, "/crm/drafts").select_one("#campaignReadiness")
    assert panel is not None, "the Drafts page must carry the readiness checklist at #campaignReadiness"
    return panel


def test_a_fresh_drafts_page_lists_the_four_readiness_rows_in_order(make_web):
    panel = readiness(make_web())
    rows = panel.select("li")
    assert len(rows) == 4
    labels = ["Gmail connected", "Test recipient set", "Sender profile saved", "One draft approved"]
    for row, label in zip(rows, labels):
        assert label in squash(row), (label, squash(row))


def test_a_fresh_drafts_page_badge_says_to_do_four_items(make_web):
    panel = readiness(make_web())
    assert "To do: 4 items" in squash(panel)


def test_a_fresh_drafts_page_offers_no_send_test_form(make_web):
    s = soup(make_web(), "/crm/drafts")
    assert [f for f in s.find_all("form") if "send-test" in (f.get("action") or "")] == []


def test_the_gmail_and_recipient_rows_link_to_the_settings_cards(make_web):
    panel = readiness(make_web())
    hrefs = [a.get("href") for a in panel.find_all("a")]
    assert "/settings#svc-gmail" in hrefs
    assert "/settings#svc-test-recipient" in hrefs
    rows = panel.select("li")
    assert any(a.get("href") == "/settings#svc-gmail" for a in rows[0].find_all("a"))
    assert any(a.get("href") == "/settings#svc-test-recipient" for a in rows[1].find_all("a"))


# ---------------------------------------------------------------------------------------------------------------
# 5  Everything in place: the page shows exactly what a test email will contain
# ---------------------------------------------------------------------------------------------------------------

def preview_of(web):
    node = soup(web, "/crm/drafts").select_one("#testPreview")
    assert node is not None, "with everything in place the Drafts page must preview the test email"
    return node


def dd_for(node, term):
    for dt in node.find_all("dt"):
        if " ".join(dt.get_text().split()).rstrip(":") == term:
            return " ".join(dt.find_next_sibling("dd").get_text().split())
    pytest.fail(f"the preview has no {term!r} row")


def test_the_badge_reads_ready_to_send_a_test_once_everything_is_in_place(send_web):
    web = ready_web(send_web)
    assert "Ready to send a test" in squash(readiness(web))
    assert "To do" not in squash(readiness(web))


def test_the_preview_shows_the_test_recipient_the_subject_and_the_body_never_the_contact(send_web):
    web = ready_web(send_web)
    node = preview_of(web)
    assert dd_for(node, "To") == TEST_INBOX
    assert "ada@example.com" not in dd_for(node, "To")
    draft = read_draft_file(web.ws, "ada")
    assert draft.subject in squash(node)
    first_line = next(line.strip() for line in draft.body.splitlines() if line.strip())
    assert first_line in squash(node)


def test_the_preview_names_the_deck_file_as_an_attachment_chip(send_web):
    web = ready_web(send_web)
    name = set_deck(web)
    assert f"Attachment: {name}" in squash(preview_of(web))


def test_the_preview_says_the_email_goes_only_to_the_test_recipient(send_web):
    assert PREVIEW_SENTENCE in squash(preview_of(ready_web(send_web)))


def test_the_preview_has_exactly_one_send_test_form_that_returns_to_the_drafts_page(send_web):
    web = ready_web(send_web)
    pid = partner_id(web, "ada")
    s = soup(web, "/crm/drafts")
    forms = [f for f in s.find_all("form") if "send-test" in (f.get("action") or "")]
    assert len(forms) == 1
    form = forms[0]
    assert (form.get("method") or "get").lower() == "post"
    assert form["action"] == f"/partners/{pid}/send-test"
    nxt = form.find("input", attrs={"name": "next"})
    assert nxt is not None and nxt.get("value") == "/crm/drafts"
    assert form.find("input", attrs={"name": "csrf_token"}) is not None


# ---------------------------------------------------------------------------------------------------------------
# 6  POST /partners/<id>/send-test: where it returns to, and the refusal when recipient equals contact
# ---------------------------------------------------------------------------------------------------------------

def test_send_test_with_next_drafts_sends_to_the_test_recipient_and_returns_to_the_drafts_page(send_web):
    web = ready_web(send_web)
    response = web.post(f"/partners/{partner_id(web, 'ada')}/send-test", {"next": "/crm/drafts"}, follow=False)
    assert response.status_code in (302, 303)
    assert response.headers["Location"].split("?")[0].endswith("/crm/drafts")
    assert len(web.transport.calls) == 1
    assert web.transport.calls[0].recipients == [TEST_INBOX]


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "http://evil.example/crm/drafts",
                                    "https:evil.example"])
def test_send_test_never_redirects_off_site(send_web, target):
    web = ready_web(send_web)
    response = web.post(f"/partners/{partner_id(web, 'ada')}/send-test", {"next": target}, follow=False)
    location = response.headers.get("Location", "")
    assert "evil.example" not in location
    assert response.status_code in (200, 302, 303)
    if location:
        assert location.startswith("/") and not location.startswith("//"), location


def test_send_test_is_refused_when_the_test_recipient_is_the_contact(send_web):
    web = ready_web(send_web, env={"TEST_RECIPIENT_EMAIL": "ada@example.com"})
    response = web.post(f"/partners/{partner_id(web, 'ada')}/send-test", {"next": "/crm/drafts"}, follow=True)
    text = page_text(response)
    assert web.transport.calls == [], "nothing may be sent when the recipient is the contact"
    assert "Test recipient must differ from the contact email" in text
    assert "Test recipient card" in text


# ---------------------------------------------------------------------------------------------------------------
# 7  The sender mini form on the Drafts page, and the refusal that links back to it
# ---------------------------------------------------------------------------------------------------------------

def sender_form(web):
    forms = [f for f in soup(web, "/crm/drafts").find_all("form") if (f.get("action") or "") == "/settings/sender"]
    assert len(forms) == 1, "a workspace with no sender profile must offer the mini form on the Drafts page"
    return forms[0]


def test_the_drafts_page_offers_a_labelled_name_email_and_signature_form_when_no_sender_exists(make_web):
    form = sender_form(make_web())
    s_form = BeautifulSoup(str(form), "html.parser")
    for field, label in (("name", "name"), ("sender_email", "email"), ("signature", "signature")):
        el = s_form.find(attrs={"name": field})
        assert el is not None, field
        assert el.get("id"), f"{field} needs an id so its label can point at it"
        tag = s_form.find("label", attrs={"for": el["id"]})
        assert tag is not None and label in tag.get_text().lower(), (field, tag)
    assert s_form.find("input", attrs={"name": "csrf_token"}) is not None


def test_saving_the_mini_form_stores_the_profile_and_the_form_goes_away(make_web):
    web = make_web()
    form = sender_form(web)
    fields = {i["name"]: i.get("value", "") for i in form.find_all("input", attrs={"type": "hidden"}) if i.get("name")}
    fields.pop("csrf_token", None)
    fields.update({"name": "Pat Doe", "sender_email": "pat@example.com", "signature": "Pat Doe\nFounder"})
    response = web.post("/settings/sender", fields, follow=False)
    assert response.status_code in (302, 303)
    s = soup(web, "/crm/drafts")
    assert [f for f in s.find_all("form") if (f.get("action") or "") == "/settings/sender"] == []
    assert "Pat Doe" in squash(s.select_one("#campaignReadiness"))
    settings = soup(web, "/settings")
    assert "Pat Doe" in settings.get_text(" ") or "pat@example.com" in settings.get_text(" ") or \
        any((i.get("value") or "") in ("Pat Doe", "pat@example.com") for i in settings.find_all("input"))


def test_writing_drafts_without_a_sender_profile_is_refused_with_a_link_to_the_write_drafts_section(make_web):
    web = make_web()
    seed_contacts(web, [ADA])
    response = web.post(web.url("drafts_write"), {"provider": "template"}, follow=True)
    s = BeautifulSoup(body(response), "html.parser")
    assert read_draft_file(web.ws, "ada") is None, "no draft may be written without a sender profile"
    refusal = next((n for n in s.select(".flash") if "sender" in n.get_text().lower()), None)
    assert refusal is not None, "the refusal must be shown as a message mentioning the sender profile"
    assert any((a.get("href") or "").endswith("#writeDrafts") for a in refusal.find_all("a"))


# ---------------------------------------------------------------------------------------------------------------
# 8  Campaign kind: raising money or looking for a role
# ---------------------------------------------------------------------------------------------------------------

def set_kind(web, kind, **kw):
    return web.post("/settings/campaign-kind", {"kind": kind}, **kw)


def test_a_fresh_drafts_page_asks_which_kind_of_campaign_this_is(make_web):
    s = soup(make_web(), "/crm/drafts")
    text = squash(s)
    assert "I am raising money" in text and "I am looking for a role" in text
    forms = [f for f in s.find_all("form") if (f.get("action") or "") == "/settings/campaign-kind"]
    values = {(e.get("value") or "") for f in forms for e in f.find_all(attrs={"name": "kind"})}
    assert {"fundraising", "job_search"} <= values


def test_choosing_a_kind_requires_the_csrf_token(make_web):
    web = make_web()
    assert set_kind(web, "job_search", csrf=False).status_code == 400
    assert "hiring manager" not in page_text(web.get("/crm/drafts")).lower()


def test_an_invalid_kind_is_rejected_and_keeps_the_saved_one(make_web):
    web = make_web()
    assert set_kind(web, "job_search").status_code in (302, 303)
    response = set_kind(web, "bogus", follow=True)
    assert "Choose one of the two campaign types" in page_text(response)
    text = page_text(web.get("/crm/drafts"))
    assert "Resume" in text and "hiring manager" in text.lower()


def test_the_job_search_page_says_resume_and_hiring_manager_not_pitch_deck(make_web):
    web = make_web()
    set_kind(web, "job_search")
    text = page_text(web.get("/crm/drafts"))
    assert "Resume" in text
    assert "hiring manager" in text.lower()
    assert "Pitch deck" not in text


def test_the_fundraising_campaign_page_shows_the_fifty_meetings_proof(make_web):
    web = make_web()
    set_kind(web, "fundraising")
    assert FUNDRAISING_PROOF in page_text(web.get("/crm/drafts"))


def test_the_job_search_campaign_page_never_shows_the_fundraising_proof(make_web):
    web = make_web()
    set_kind(web, "job_search")
    text = page_text(web.get("/crm/drafts"))
    assert FUNDRAISING_PROOF not in text and RAISE_AMOUNT not in text


DECK_VIEW = 'a[href^="/crm/deck"], iframe[src^="/crm/deck"], embed[src^="/crm/deck"], object[data^="/crm/deck"]'


def pitch_stage(s):
    """Stage 2 of the Campaign page: the section whose heading starts with the number 2."""
    for heading in s.find_all(["h2", "h3"]):
        if re.match(r"^2\b", squash(heading)):
            return heading.find_parent("section")
    raise AssertionError("the Campaign page must have a stage 2 section")


def stage_is_done(stage):
    return bool(re.search(r"\bDone\b", squash(stage)))


def kind_switch(s):
    form = next((f for f in s.find_all("form") if (f.get("action") or "") == "/settings/campaign-kind"
                 and "Campaign type" in squash(f)), None)
    assert form is not None, "the Campaign page needs a small Campaign type control"
    return form


def switch_kind_on_the_page(web, kind):
    """Use the Campaign type control the way a person would: its own fields, its own return address."""
    form = kind_switch(soup(web, "/crm/drafts"))
    fields = {e.get("name"): e.get("value") for e in form.find_all("input", attrs={"name": True})
              if e.get("type") == "hidden" and e.get("name") != "csrf_token"}
    fields["kind"] = kind
    response = web.post("/settings/campaign-kind", fields, follow=False)
    assert response.status_code in (302, 303)
    return response


def test_the_campaign_type_can_be_switched_from_the_campaign_page_and_the_example_follows(make_web):
    web = make_web()
    set_kind(web, "fundraising")
    kind_switch(soup(web, "/crm/drafts"))
    assert "Texas A&M dropout" in squash(pitch_stage(soup(web, "/crm/drafts")))

    response = switch_kind_on_the_page(web, "job_search")
    assert (response.headers.get("Location") or "").split("?")[0].endswith("/crm/drafts")
    s = soup(web, "/crm/drafts")
    stage = squash(pitch_stage(s))
    assert "Texas A&M dropout" not in stage and FUNDRAISING_PROOF not in squash(s)
    assert "resume" in stage.lower()
    kind_switch(s)

    switch_kind_on_the_page(web, "fundraising")
    again = squash(pitch_stage(soup(web, "/crm/drafts")))
    assert "Texas A&M dropout" in again and "resume" not in again.lower()


def test_with_no_campaign_type_chosen_stage_two_only_asks_and_is_not_done(make_web):
    web = make_web()
    set_deck(web)
    s = soup(web, "/crm/drafts")
    stage = pitch_stage(s)
    text = squash(stage)
    assert "Choose what you are doing" in text
    assert not stage_is_done(stage), "a pitch cannot be Done before the person says what they are doing"
    assert "Texas A&M dropout" not in text, "no example pitch before a type is chosen"
    assert FUNDRAISING_PROOF not in squash(s) and RAISE_AMOUNT not in squash(s)
    assert s.select_one(DECK_VIEW) is None, "no deck panel before a type is chosen"
    assert "mydeck.pdf" not in squash(s)


def test_a_fundraising_campaign_previews_the_configured_deck(make_web):
    web = make_web()
    set_deck(web)
    set_kind(web, "fundraising")
    s = soup(web, "/crm/drafts")
    assert s.select_one(DECK_VIEW), "the fundraising Campaign page must show the pitch deck"
    assert s.select_one('iframe[src^="/crm/deck"]') or s.select_one('a[href^="/crm/deck"]'), "deck is previewed or openable"


def test_a_job_search_with_no_resume_says_so_and_shows_no_deck(make_web):
    web = make_web()
    set_kind(web, "job_search")
    s = soup(web, "/crm/drafts")
    stage = pitch_stage(s)
    assert "no resume" in squash(stage).lower()
    assert any((a.get("href") or "") == "/settings#sec-package" and "resume" in squash(a).lower()
               for a in stage.find_all("a")), "a link to attach a resume in Settings"
    assert s.select_one(DECK_VIEW) is None, "no deck is shown when no resume is attached"
    text = squash(s)
    assert "Pitch deck" not in text and "Publik pitch deck" not in text
    assert FUNDRAISING_PROOF not in text and RAISE_AMOUNT not in text


def test_a_job_search_with_a_file_attached_calls_it_the_resume(make_web):
    web = make_web()
    set_kind(web, "job_search")
    set_deck(web, "my-resume.pdf")
    s = soup(web, "/crm/drafts")
    stage = squash(pitch_stage(s))
    assert "no resume" not in stage.lower()
    assert "my-resume.pdf" in stage and "resume" in stage.lower()
    assert "Pitch deck" not in squash(s) and FUNDRAISING_PROOF not in squash(s)


@pytest.mark.parametrize("kind", ["fundraising", "job_search"])
def test_the_send_plan_never_shows_the_fundraising_proof(send_web, kind):
    web = ready_web(send_web)
    set_kind(web, kind)
    assert FUNDRAISING_PROOF not in page_text(web.get("/crm/send-plan"))


def test_the_job_search_draft_editor_never_shows_the_fundraising_proof(send_web):
    web = ready_web(send_web)
    set_kind(web, "job_search")
    response = web.get("/crm/drafts/ada", follow_redirects=True)
    assert response.status_code == 200
    text = page_text(response)
    assert FUNDRAISING_PROOF not in text and RAISE_AMOUNT not in text


@pytest.mark.parametrize("kind", ["fundraising", "job_search"])
def test_the_proof_never_gets_into_the_email_the_user_edits_previews_or_copies(send_web, kind):
    """The read-only example panel may carry the fundraising note; the email itself never does."""
    web = ready_web(send_web)
    set_kind(web, kind)
    s = soup(web, "/crm/drafts/ada")
    email_parts = {
        "subject box": (s.select_one('input[name="subject"]') or {}).get("value", ""),
        "body box": squash(s.select_one('textarea[name="body"]')),
        "email preview": squash(s.select_one("#draftPreview")),
        "example copy box": squash(s.select_one("#examplePitchCopy")) if s.select_one("#examplePitchCopy") else "",
    }
    assert email_parts["subject box"] and email_parts["body box"], "the editor must show the real draft"
    for name, text in email_parts.items():
        assert FUNDRAISING_PROOF not in text and RAISE_AMOUNT not in text, name


@pytest.mark.parametrize("kind", ["fundraising", "job_search"])
def test_no_draft_text_and_no_sent_email_ever_carries_the_raise_amount(send_web, kind):
    web = ready_web(send_web, [ADA, GRACE])
    set_kind(web, kind)
    for slug in ("ada", "grace"):
        draft = read_draft_file(web.ws, slug)
        assert draft is not None
        assert RAISE_AMOUNT not in draft.subject + "\n" + draft.body
    web.post(f"/partners/{partner_id(web, 'ada')}/send-test", {"next": "/crm/drafts"}, follow=True)
    assert web.transport.calls, "the test email should have been recorded"
    for call in web.transport.calls:
        assert RAISE_AMOUNT not in call.subject + "\n" + call.text
        assert FUNDRAISING_PROOF not in call.text


# ---------------------------------------------------------------------------------------------------------------
# 9  The CSS and UI contract: Menu button, shell.js, skip link
# ---------------------------------------------------------------------------------------------------------------

UI_PAGES = ["/crm", "/crm/drafts", "/settings"]


@pytest.mark.parametrize("path", UI_PAGES)
def test_the_primary_nav_has_a_menu_button_that_controls_the_links_container(make_web, path):
    s = soup(make_web(), path)
    nav = s.select_one('nav[aria-label="Primary"]') or s.find("nav")
    assert nav is not None
    button = next((b for b in nav.find_all("button") if b.get_text(strip=True) == "Menu"), None)
    assert button is not None, "the responsive nav needs a Menu button"
    target = button.get("aria-controls")
    assert target, "the Menu button must say which element it controls"
    controlled = s.find(id=target)
    assert controlled is not None and controlled.find("a") is not None, "aria-controls must point at the links"


@pytest.mark.parametrize("path", UI_PAGES)
def test_pages_load_shell_js_and_it_is_served(make_web, path):
    web = make_web()
    s = soup(web, path)
    srcs = [t.get("src") for t in s.find_all("script") if (t.get("src") or "").split("?")[0].endswith("shell.js")]
    assert srcs, "every page loads shell.js"
    response = web.get(srcs[0])
    assert response.status_code == 200 and len(response.get_data()) > 0


@pytest.mark.parametrize("path", UI_PAGES)
def test_pages_have_a_skip_to_content_link_that_lands_on_the_main_element(make_web, path):
    s = soup(make_web(), path)
    link = s.select_one('a[href="#main"]')
    assert link is not None and "Skip to content" in link.get_text()
    assert s.find(id="main") is not None


# ---------------------------------------------------------------------------------------------------------------
# 10  A one time link used a second time says so
# ---------------------------------------------------------------------------------------------------------------

def test_opening_the_one_time_link_a_second_time_is_refused_with_a_plain_explanation(make_web):
    first = make_web(login=False)
    token = get_token(first.app)
    assert first.get(f"/?token={token}").status_code in (302, 303)
    other = Web(first.app, first.ws, first.transport)
    response = other.get(f"/?token={token}")
    assert response.status_code == 403
    assert USED_LINK_MESSAGE in page_text(response)


# ---------------------------------------------------------------------------------------------------------------
# 11  The MCP surface: sixteen tools, no secrets in, seven setup steps, an honest example pitch
# ---------------------------------------------------------------------------------------------------------------

def test_there_are_still_sixteen_tools(mcp):
    assert len(mcp().list_tools()) == 16


def test_no_tool_accepts_a_secret_password_or_token_argument(mcp):
    for tool in mcp().list_tools():
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        bad = [name for name in props if SECRET_WORDS.search(name)]
        assert not bad, f"{tool['name']} accepts {bad}"


def test_setup_status_lists_seven_steps_gmail_first_and_the_publik_api_last(mcp):
    data = assert_ok(mcp().call("setup_status"))
    keys = [s["key"] for s in data["steps"]]
    assert len(keys) == 7
    assert keys[0] == "gmail" and keys[-1] == "publik_api"
    assert PUBLIK_LABEL in [s["label"] for s in data["steps"]][-1]


def test_the_example_pitch_has_the_real_subject_and_no_fundraising_proof(mcp):
    data = assert_ok(mcp().call("setup_status"))
    pitch = json.dumps(data["example_pitch"])
    assert "Subject: Texas A&M dropout working on Publik" in data["example_pitch"]["example_email"]["text"]
    assert FUNDRAISING_PROOF not in pitch
    assert RAISE_AMOUNT not in pitch


# ---------------------------------------------------------------------------------------------------------------
# 12  Hygiene: no em dash and no en dash in the pages a first time user sees
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", HYGIENE_PAGES)
def test_fresh_workspace_pages_carry_no_em_or_en_dash(make_web, path):
    html = make_web().get(path, follow_redirects=True)
    assert html.status_code == 200
    markup = body(html)
    assert EM_DASH not in markup and EN_DASH not in markup


@pytest.mark.parametrize("path", HYGIENE_PAGES + ["/crm/drafts/ada"])
def test_ready_workspace_pages_carry_no_em_or_en_dash(send_web, path):
    web = ready_web(send_web)
    set_deck(web)
    response = web.get(path, follow_redirects=True)
    assert response.status_code == 200
    markup = body(response)
    assert EM_DASH not in markup and EN_DASH not in markup



