"""F9 to F16: the P2 and P3 audit fixes (spec: outputs/ux_pass_20261021/fix_spec.md).

A new user imports a CSV, clicks Write drafts, adds replies, opens Settings and the verification page, and sends a test
in TEST mode with Gmail not connected. Layout checks (F9, F10) read the CSS and markup the app serves, because a test
client cannot paint a page: they evaluate the served stylesheets at 390 and 768 pixels wide.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures + autouse env isolation)
from web_helpers import (
    ADA, MINA, TEST_INBOX, body, contact_ref, csv_row, page_text, post_keys, read_draft_file, seed_contacts,
)

from audit_fix_helpers import dom, flashes, form_data, has_dash, soup, write_drafts

BO = csv_row(id="bo", name="Bo Lee", email="bo@example.com", org="Northwind Labs", role="Engineer",
             facts="Maintains an open source scheduling library")
ELI = csv_row(id="eli", name="Eli Stone", email="eli@example.com", org="Stone Labs", role="Founder",
              facts="Wrote an essay on accessibility in developer tools", source_urls="https://example.com/a11y")


def _env():
    return {"SENDER_EMAIL": "sender@example.com", "TEST_RECIPIENT_EMAIL": TEST_INBOX}


def _lower(text: str) -> str:
    return " ".join(text.split()).lower()


def _double_period(text: str) -> bool:
    """True for '..' that is not part of an ellipsis."""
    return re.search(r"(?<!\.)\.\.(?!\.)", text) is not None


# =================================================================================================================
# A tiny CSS reader for the layout checks (F9, F10)
# =================================================================================================================

Rule = Tuple[Optional[str], str, Dict[str, str]]


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _block_end(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1


def _declarations(block: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for part in block.split(";"):
        if ":" in part:
            name, _, value = part.partition(":")
            found[name.strip().lower()] = value.replace("!important", "").strip().lower()
    return found


def parse_css(text: str, media: Optional[str] = None) -> List[Rule]:
    text = _strip_comments(text)
    rules: List[Rule] = []
    index = 0
    while True:
        open_at = text.find("{", index)
        if open_at < 0:
            return rules
        prelude = text[index:open_at].strip()
        close_at = _block_end(text, open_at)
        inner = text[open_at + 1:close_at]
        if prelude.lower().startswith("@media"):
            rules.extend(parse_css(inner, prelude[6:].strip().lower()))
        elif prelude.startswith("@"):
            pass
        else:
            decls = _declarations(inner)
            for selector in prelude.split(","):
                rules.append((media, " ".join(selector.split()), decls))
        index = close_at + 1


def media_matches(media: Optional[str], width: int) -> bool:
    if media is None:
        return True
    for query in media.split(","):
        query = re.sub(r"^(only\s+)?(screen|all)\s*(and)?", "", query.strip())
        features = re.findall(r"\(([^)]*)\)", query)
        if not features:
            continue
        ok = True
        for feature in features:
            match = re.fullmatch(r"\s*(min|max)-width\s*:\s*(\d+)px\s*", feature)
            if not match:
                ok = False
                break
            limit = int(match.group(2))
            if match.group(1) == "max" and width > limit:
                ok = False
            if match.group(1) == "min" and width < limit:
                ok = False
        if ok:
            return True
    return False


def effective(rules: List[Rule], selector: str, width: int) -> Dict[str, str]:
    """Declarations that apply to a selector at a viewport width (source order wins, specificity is ignored)."""
    merged: Dict[str, str] = {}
    for media, rule_selector, decls in rules:
        if not media_matches(media, width):
            continue
        if ":" in rule_selector.replace("::", ":") and ":" not in selector:
            continue
        if rule_selector == selector or rule_selector.endswith(" " + selector) or rule_selector.endswith(">" + selector):
            merged.update(decls)
    return merged


def served_css(web, path: str) -> List[Rule]:
    rules: List[Rule] = []
    for link in dom(web, path).select('link[rel="stylesheet"][href]'):
        href = link["href"]
        if href.startswith("/"):
            response = web.get(href)
            assert response.status_code == 200, href
            rules.extend(parse_css(body(response)))
    assert rules, "no local stylesheet found"
    return rules


# =================================================================================================================
# F9: /replies has no horizontal page overflow at 390px
# =================================================================================================================

WIDTH_PHONE = 390
WIDTH_TABLET = 768


def _scrolls(decls: Dict[str, str]) -> bool:
    return decls.get("overflow-x") in ("auto", "scroll") or decls.get("overflow") in ("auto", "scroll")


def _with_replies(make_web, count: int = 1):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    for number in range(count):
        web.post("/replies/add", {"from_email": "ada@example.com", "subject": f"Re: hello {number}",
                                  "body": "sounds good, happy to talk"}, follow=True)
    return web


def test_replies_page_declares_a_mobile_viewport(make_web):
    web = _with_replies(make_web)
    meta = dom(web, "/replies").select_one('meta[name="viewport"]')
    assert meta is not None and "width=device-width" in meta.get("content", "")


def test_replies_table_sits_in_a_wrapper_that_scrolls_instead_of_widening_the_page(make_web):
    web = _with_replies(make_web)
    page = dom(web, "/replies")
    table = page.select_one("table")
    assert table is not None
    wrapper = next((p for p in table.parents if p.name == "div" and "crm-table-wrap" in (p.get("class") or [])), None)
    assert wrapper is not None, "the replies table is not inside a .crm-table-wrap"
    rules = served_css(web, "/replies")
    decls = effective(rules, ".crm-table-wrap", WIDTH_PHONE)
    assert _scrolls(decls), f"the wrapper must scroll sideways, got {decls}"
    assert decls.get("max-width") == "100%" or decls.get("width") == "100%", decls


def test_replies_page_ancestors_can_shrink_below_the_table_width(make_web):
    """Flex and grid children default to min-width:auto, so a 850px table would widen every ancestor."""
    web = _with_replies(make_web)
    rules = served_css(web, "/replies")
    for selector in (".crm-panel", ".crm-shell", ".main"):
        decls = effective(rules, selector, WIDTH_PHONE)
        shrinks = decls.get("min-width") in ("0", "0px") or decls.get("overflow-x") in ("hidden", "auto", "scroll", "clip")
        assert shrinks, f"{selector} can grow with its content at {WIDTH_PHONE}px: {decls}"


# =================================================================================================================
# F10: the top navigation is reachable at 390 and 768 px
# =================================================================================================================


def _nav_toggle(page):
    nav = page.select_one("nav.nav") or page.select_one("nav")
    for button in nav.select("button[aria-controls]"):
        if page.find(id=button["aria-controls"]) is not None:
            return button
    return None


@pytest.mark.parametrize("width", [WIDTH_PHONE, WIDTH_TABLET])
def test_nav_wraps_or_collapses_behind_a_menu_button(make_web, width):
    web = make_web(_env(), sender=True)
    page = dom(web, "/crm")
    if _nav_toggle(page) is not None:
        return
    rules = served_css(web, "/crm")
    links = effective(rules, ".nav-links", width)
    assert links.get("flex-wrap") in ("wrap", "wrap-reverse"), (
        f"at {width}px the nav neither wraps nor sits behind a menu button: {links}")
    inner = effective(rules, ".nav-inner", width)
    assert inner.get("flex-wrap") in ("wrap", "wrap-reverse"), inner


def test_every_primary_destination_is_present_in_the_nav_markup(make_web):
    web = make_web(_env(), sender=True)
    nav = dom(web, "/crm").select_one("nav.nav")
    hrefs = {a.get("href") for a in nav.select("a[href]")}
    for needed in ("/crm", "/partners", "/enrichment", "/crm/drafts", "/crm/send-plan", "/scheduled", "/replies",
                   "/crm/analytics", "/settings"):
        assert needed in hrefs, f"{needed} is not reachable from the nav"


# =================================================================================================================
# F11: vague "check your settings" errors name the setting and link to its Settings card
# =================================================================================================================


def _settings_link(links: List[str]) -> Optional[str]:
    return next((href for href in links if href.startswith("/settings") and "#" in href), None)


def test_poll_replies_without_gmail_names_gmail_and_links_to_its_card(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    response = web.post("/poll-replies", {}, follow=True)
    assert response.status_code == 200
    shown = flashes(body(response))
    assert shown, "no message after polling"
    joined = _lower(" ".join(text for text, _ in shown))
    assert "check your settings" not in joined, joined
    assert "gmail" in joined, joined
    assert any(_settings_link(links) for _, links in shown), f"no link to the Gmail settings card: {shown}"


def test_poll_replies_link_points_at_a_card_that_exists(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    shown = flashes(body(web.post("/poll-replies", {}, follow=True)))
    href = next((_settings_link(links) for _, links in shown if _settings_link(links)), None)
    assert href, f"no link to a Settings card in the poll message: {shown}"
    anchor = href.split("#", 1)[1]
    assert dom(web, "/settings").find(id=anchor) is not None, f"#{anchor} is not an element on /settings"


def test_provider_lookup_without_a_key_names_the_provider_and_links_to_its_card(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    response = web.post(f"/email-verification/{ref}/run", {"provider": "apollo", "operation": "person_enrich"},
                        follow=True)
    shown = flashes(body(response))
    assert shown, "no message after the lookup"
    joined = _lower(" ".join(text for text, _ in shown))
    assert "check your settings" not in joined, joined
    assert "apollo" in joined, joined
    link = next((_settings_link(links) for _, links in shown if _settings_link(links)), None)
    assert link, f"no link to the Apollo settings card: {shown}"
    assert dom(web, "/settings").find(id=link.split("#", 1)[1]) is not None


# =================================================================================================================
# F12: unset optional services are gray "Not set", never red "Failed"
# =================================================================================================================


def _card(page, heading: str):
    for article in page.select("article.svc-card"):
        title = article.select_one("h2")
        if title and heading.lower() in title.get_text(" ", strip=True).lower():
            return article
    raise AssertionError(f"no Settings card titled {heading!r}")


@pytest.mark.parametrize("heading", ["Gemini", "Cloudflare tracking"])
def test_unset_optional_service_card_says_not_set_and_never_failed(make_web, heading):
    web = make_web(_env(), sender=True)
    card = _card(dom(web, "/settings"), heading)
    label = card.select_one("[data-svc-label]")
    assert label is not None and label.get_text(strip=True) == "Not set", str(card)[:400]
    assert card.get("data-state") == "unset"
    assert "failed" not in card.get_text(" ", strip=True).lower()


def test_testing_gemini_with_no_key_reports_unset_not_a_failure(make_web):
    web = make_web(_env(), sender=True)
    response = web.post("/api/gemini/test", {})
    payload = response.get_json()
    assert payload.get("unset") is True or payload.get("error_code") == "provider_key_not_configured", payload


def test_testing_apollo_with_no_key_reports_unset_not_a_failure(make_web):
    web = make_web(_env(), sender=True)
    payload = web.post("/api/data-provider/test", {"provider": "apollo"}).get_json()
    assert payload.get("unset") is True or payload.get("error_code") == "provider_key_not_configured", payload


@pytest.mark.parametrize("heading", ["OpenAI"])
def test_model_provider_cards_offer_a_test_or_say_none_is_available(make_web, heading):
    web = make_web(_env(), sender=True)
    card = _card(dom(web, "/settings"), heading)
    has_button = any("test" in b.get_text(" ", strip=True).lower() for b in card.select("button"))
    says_none = re.search(r"no\b[^.]*\btest\b[^.]*available", _lower(card.get_text(" ", strip=True)))
    assert has_button or says_none, card.get_text(" ", strip=True)


def test_verification_buttons_are_disabled_with_a_hint_when_no_key_is_saved(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    page = dom(web, "/email-verification")
    buttons = [b for b in page.select("td button")
               if any(w in b.get_text(" ", strip=True).lower() for w in ("apollo", "enrow"))]
    assert buttons, "no provider action buttons on the verification page"
    for button in buttons:
        assert button.has_attr("disabled"), f"{button.get_text(' ', strip=True)} is clickable without a key"
    hint = _lower(page.get_text(" ", strip=True))
    assert re.search(r"add an? (apollo|enrow)[^.]*key[^.]*settings", hint), "no hint about adding a key"


# =================================================================================================================
# F13: the test recipient message
# =================================================================================================================


@pytest.mark.parametrize("bad", ["not-an-email", "a@example.com, b@example.com"])
def test_invalid_test_recipient_says_enter_one_valid_email_address(make_web, bad):
    web = make_web(_env(), sender=True)
    response = post_keys(web, {"TEST_RECIPIENT_EMAIL": bad})
    text = body(response)
    assert "Enter one valid email address." in page_text(text) or "Enter one valid email address." in text, text[:500]
    assert "TEST_RECIPIENT_EMAIL must be" not in text


def test_invalid_test_recipient_is_not_saved(make_web):
    web = make_web(_env(), sender=True)
    post_keys(web, {"TEST_RECIPIENT_EMAIL": "not-an-email"})
    env_text = (web.ws / ".env").read_text(encoding="utf-8") if (web.ws / ".env").exists() else ""
    assert "not-an-email" not in env_text


def test_valid_test_recipient_still_saves(make_web):
    web = make_web(_env(), sender=True)
    response = post_keys(web, {"TEST_RECIPIENT_EMAIL": "me@example.com"})
    assert "Enter one valid email address." not in body(response)


# =================================================================================================================
# F14: no legacy note or role placeholder on new contacts; the credibility line stays in the reference panel
# =================================================================================================================

LEGACY = "Legacy contact. Current Publik fit and relationship status require review."


def test_contact_added_by_csv_import_has_no_legacy_note(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    text = page_text(body(web.get(f"/crm/{ref}")))
    assert LEGACY not in text, "a freshly imported contact is told it is a legacy contact"
    assert "Legacy contact" not in text


def test_other_investors_page_shows_the_role_when_it_is_set(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA, BO])
    ref = contact_ref(web, "ada@example.com")
    text = page_text(body(web.get(f"/crm/{ref}/alternate-investors")))
    assert "Bo Lee" in text, "setup: the colleague at the same firm is not listed"
    assert "Engineer" in text
    assert "Role needs research" not in text


def _credibility(text: str) -> bool:
    low = _lower(text)
    return "50+ meetings" in low or "$600k" in low


def test_credibility_line_is_never_in_a_draft_the_editor_or_the_test_email(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA, ELI])
    write_drafts(web)
    for slug, email in (("ada", "ada@example.com"), ("eli", "eli@example.com")):
        draft = read_draft_file(web.ws, slug)
        assert not _credibility(draft.subject + " " + draft.body), slug
        editor = dom(web, f"/crm/drafts/{slug}")
        fields = [t.get_text() for t in editor.select("textarea")] + [i.get("value", "") for i in editor.select("input")]
        assert not _credibility(" ".join(fields)), slug
    ref = contact_ref(web, "ada@example.com")
    web.post(f"/partners/{ref}/send-test", {}, follow=True)
    assert web.transport.calls, "setup: the test send did not go out"
    for call in web.transport.calls:
        assert not _credibility(call.subject + " " + call.text)
        assert call.recipients == [TEST_INBOX], call.recipients


@pytest.mark.parametrize("path", ["/crm/drafts"])
def test_credibility_line_where_shown_is_labelled_as_the_template_origin(make_web, path):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    page = dom(web, path)
    holders = [el for el in page.find_all(string=re.compile(r"50\+ meetings|\$600K", re.I))]
    for text_node in holders:
        parent = text_node.parent
        assert parent.name not in ("textarea", "input", "option"), "credibility line inside a form field"
        assert "template" in _lower(parent.get_text(" ", strip=True)), f"unlabelled credibility line: {parent}"


# =================================================================================================================
# F15: pluralisation and double periods
# =================================================================================================================


def test_one_reply_is_not_called_one_replies(make_web):
    web = _with_replies(make_web, count=1)
    text = page_text(body(web.get("/replies")))
    assert "1 replies" not in text
    assert re.search(r"\b1 reply\b", text), text[:600]


def test_writing_one_draft_says_wrote_1_draft(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    shown = flashes(body(write_drafts(web)))
    joined = " ".join(text for text, _ in shown)
    assert "Wrote 1 draft" in joined, shown
    assert "1 drafts" not in joined


def _draft_ids(web) -> List[str]:
    html = body(web.get("/partners"))
    found = re.findall(r'name="draft_ids"[^>]*value="(\d+)"|value="(\d+)"[^>]*name="draft_ids"', html)
    return [first or second for first, second in found]


def test_one_refused_draft_says_1_draft_was_refused(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    ids = _draft_ids(web)
    assert len(ids) == 1, "setup: one draft id expected on /partners"
    shown = flashes(body(web.post("/send-batch", {"draft_ids": ids[0]}, follow=True)))
    joined = " ".join(text for text, _ in shown)
    assert "1 draft was refused" in joined, joined
    assert "1 drafts" not in joined


def test_first_run_flashes_have_no_double_periods(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA, MINA])
    messages: List[str] = []
    for response in (write_drafts(web), web.post("/poll-replies", {}, follow=True),
                     web.post("/crm/analytics/sync-tracking", {}, follow=True)):
        messages.extend(text for text, _ in flashes(body(response)))
    assert messages
    assert not [m for m in messages if _double_period(m)], messages


# =================================================================================================================
# F16: SearXNG chip, default URL and the PitchBook hint
# =================================================================================================================


def _searxng_card(page):
    return _card(page, "SearXNG")


def test_searxng_with_only_a_url_does_not_say_key_saved(make_web):
    web = make_web(_env(), sender=True)
    post_keys(web, {"SEARXNG_URL": "http://localhost:8080"})
    page = dom(web, "/settings")
    card = _searxng_card(page)
    assert "key saved" not in _lower(card.get_text(" ", strip=True))
    label = card.select_one("[data-svc-label]")
    assert label is not None and "key" not in label.get_text(strip=True).lower(), label
    for chip in page.select('[data-key-chip^="SEARXNG"]'):
        assert "key saved" not in chip.get_text(strip=True).lower(), chip


def test_searxng_default_is_not_a_dead_local_port(make_web):
    web = make_web(_env(), sender=True)
    html = body(web.get("/settings"))
    assert not re.search(r"127\.0\.0\.1:9(?!\d)", html), "the dead default http://127.0.0.1:9 is back"
    card = _searxng_card(soup(html))
    values = [i.get("value", "") for i in card.select("input[name]") if i.get("type") != "hidden"]
    assert all("127.0.0.1:9" not in v for v in values), values


def test_pitchbook_card_hint_has_no_env_variable_jargon(make_web):
    web = make_web(_env(), sender=True)
    text = _card(dom(web, "/settings"), "PitchBook").get_text(" ", strip=True)
    assert "PITCHBOOK_ENABLED" not in text
    assert ".env" not in text


def test_pitchbook_disabled_message_has_no_env_variable_jargon(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, "ada@example.com")
    response = web.post(f"/partners/{ref}/pitchbook-enrich", {}, follow=True)
    shown = flashes(body(response))
    assert shown, "no message when PitchBook is off"
    joined = " ".join(text for text, _ in shown)
    assert "PITCHBOOK_ENABLED" not in joined and ".env" not in joined, joined
    assert "pitchbook" in joined.lower() and "settings" in joined.lower()


def test_settings_page_text_has_no_dashes(make_web):
    web = make_web(_env(), sender=True)
    assert not has_dash(page_text(body(web.get("/settings"))))
    assert not has_dash(page_text(body(web.get("/email-verification"))))
