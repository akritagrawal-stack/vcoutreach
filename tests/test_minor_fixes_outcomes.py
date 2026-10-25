"""Minor UI fixes, observed the way a person using the app sees them.

Spec only. Every test looks at a rendered page or an HTTP response in a throwaway workspace with obviously fake
values, no network and the injected RecordingTransport as the only way mail could leave. The outcomes covered:
one place for the Publik API key, the Remove key buttons, the services connected count, focusable anchor cards,
the favicon, links that go nowhere, the attachment label for each campaign type, the theme, and the phone-width
tap targets. No dash characters in this file.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from werkzeug.exceptions import MethodNotAllowed, NotFound

from web_helpers import *  # noqa: F401,F403
from web_helpers import ADA, BASE, EM_DASH, EN_DASH, GRACE, SENDER_EMAIL, TEST_INBOX, Web, body, page_text

FAKE_KEY = "fake-key-for-tests-0000"
PAGES = ["/", "/crm", "/crm/drafts", "/partners", "/replies", "/settings", "/crm/send-plan", "/setup/ai"]
CONNECTED_COUNT = re.compile(r"(\d+)\s+of\s+(\d+)\s+services\s+connected", re.I)


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


def set_kind(web, kind):
    response = web.post("/settings/campaign-kind", {"kind": kind})
    assert response.status_code in (200, 302, 303)


def set_deck(web, name="mydeck.pdf"):
    deck = web.ws / "attachments" / name
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(b"%PDF-1.4 fake attachment for tests\n")
    settings = web.ws / "data" / "workspace-settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    saved = json.loads(settings.read_text()) if settings.exists() else {}
    saved["pitch_deck_path"] = str(deck)  # keep the other saved choices, such as the campaign type
    settings.write_text(json.dumps(saved))
    return name


def is_hidden(node):
    """True when a person cannot see or press the element as served: the hidden attribute, or an inline hide."""
    if node.has_attr("hidden"):
        return True
    style = (node.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def served_assets(web, kind, pages=("/crm", "/settings", "/partners", "/replies", "/crm/drafts", "/enrichment")):
    """{published path without query: text} of every stylesheet or script the given pages link."""
    attr = {"css": ("link", "href", r"\.css"), "js": ("script", "src", r"\.js")}[kind]
    found = {}
    for path in pages:
        page = soup(web, path)
        for tag in page.find_all(attr[0]):
            ref = tag.get(attr[1]) or ""
            if not ref.startswith("/") or not re.search(attr[2] + r"(\?|$)", ref):
                continue
            key = urlsplit(ref).path
            if key not in found:
                response = web.get(ref)
                assert response.status_code == 200, f"{ref} (linked from {path}) returned {response.status_code}"
                found[key] = body(response)
    assert found, f"no {kind} files were linked from {pages}"
    return found


def media_blocks(css, query_pattern):
    """The inner text of every @media block whose query matches the pattern (braces matched)."""
    blocks = []
    for match in re.finditer(r"@media[^{]*" + query_pattern + r"[^{]*\{", css):
        depth, i = 1, match.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        blocks.append(css[match.end():i - 1])
    return blocks


def rules_with(block, declaration_pattern):
    """(selector, declarations) of each flat rule in the block whose declarations match the pattern."""
    out = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        if re.search(declaration_pattern, match.group(2)):
            out.append((" ".join(match.group(1).split()), match.group(2)))
    return out


# ---------------------------------------------------------------------------------------------------------------
# 1. The Publik API key is enterable in exactly one place
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key_saved", [False, True], ids=["no_key_yet", "key_already_saved"])
def test_the_publik_api_key_can_be_typed_only_in_the_publik_api_card(make_web, key_saved):
    web = make_web({"PUBLIK_API_KEY": FAKE_KEY} if key_saved else {})
    s = soup(web, "/settings")
    card = s.select_one("#svc-publik")
    assert card is not None, "the Publik API card must be on the settings page"
    typed_here = [i for i in card.find_all("input") if (i.get("type") or "text") in ("password", "text")]
    assert len(typed_here) == 1, "the Publik API card has exactly one key field"
    assert "Publik API key" in squash(card)

    more = s.select_one("#sec-more")
    assert more is not None, "the More settings section is still on the page"
    assert more.find_all(attrs={"name": "PUBLIK_API_KEY"}) == [], "no second Publik key field under More settings"
    assert s.find_all(attrs={"name": "PUBLIK_API_KEY"}) == [], "no field named PUBLIK_API_KEY anywhere on the page"
    assert "PUBLIK_API_KEY" not in squash(more)


@pytest.mark.parametrize("key_saved", [False, True], ids=["no_key_yet", "key_already_saved"])
def test_there_is_no_show_publik_api_key_button(make_web, key_saved):
    web = make_web({"PUBLIK_API_KEY": FAKE_KEY} if key_saved else {})
    s = soup(web, "/settings")
    labels = [squash(b) for b in s.find_all(["button", "a", "label", "summary"])]
    labels += [(n.get("aria-label") or "") + " " + (n.get("title") or "") for n in s.find_all(True)]
    assert not [text for text in labels if "Show PUBLIK_API_KEY" in text], "the Show PUBLIK_API_KEY button is gone"
    assert "Show PUBLIK_API_KEY" not in page_text(body(web.get("/settings")))


# ---------------------------------------------------------------------------------------------------------------
# 2. Remove key only shows when there is a key to remove
# ---------------------------------------------------------------------------------------------------------------

SERVICE_KEYS = [
    ("svc-enrow", "ENROW_API_KEY", "[data-svc-remove]"),
    ("svc-apollo", "APOLLO_API_KEY", "[data-svc-remove]"),
    ("svc-gemini", "GEMINI_API_KEY", "[data-svc-remove]"),
    ("svc-openai", "OPENAI_API_KEY", "[data-svc-remove]"),
    ("svc-anthropic", "ANTHROPIC_API_KEY", "[data-svc-remove]"),
    ("svc-openrouter", "OPENROUTER_API_KEY", "[data-svc-remove]"),
    ("svc-publik", "PUBLIK_API_KEY", "[data-pk-remove]"),
]


def remove_buttons(card, selector):
    found = card.select(selector)
    found += [b for b in card.find_all("button") if squash(b) == "Remove key" and b not in found]
    return found


@pytest.mark.parametrize("card_id,env_name,selector", SERVICE_KEYS, ids=[k[0] for k in SERVICE_KEYS])
def test_remove_key_is_not_offered_when_the_service_has_no_key(make_web, card_id, env_name, selector):
    s = soup(make_web(), "/settings")
    card = s.select_one("#" + card_id)
    assert card is not None, f"{card_id} must be on the settings page"
    visible = [b for b in remove_buttons(card, selector) if not is_hidden(b)]
    assert visible == [], f"{card_id} offers Remove key although no key is saved"


@pytest.mark.parametrize("card_id,env_name,selector", SERVICE_KEYS, ids=[k[0] for k in SERVICE_KEYS])
def test_remove_key_is_offered_once_the_service_has_a_key(make_web, card_id, env_name, selector):
    s = soup(make_web({env_name: FAKE_KEY}), "/settings")
    card = s.select_one("#" + card_id)
    assert card is not None
    buttons = remove_buttons(card, selector)
    assert buttons, f"{card_id} must render a Remove key button when {env_name} is saved"
    assert any(not is_hidden(b) for b in buttons), f"{card_id} must show Remove key when {env_name} is saved"


def test_a_saved_key_on_one_service_does_not_reveal_remove_key_on_the_others(make_web):
    s = soup(make_web({"ENROW_API_KEY": FAKE_KEY}), "/settings")
    shown = {card_id for card_id, _env, selector in SERVICE_KEYS
             if [b for b in remove_buttons(s.select_one("#" + card_id), selector) if not is_hidden(b)]}
    assert shown == {"svc-enrow"}


def test_removing_a_key_makes_its_remove_button_go_away(make_web):
    web = make_web({"ENROW_API_KEY": FAKE_KEY})
    card = soup(web, "/settings").select_one("#svc-enrow")
    assert [b for b in remove_buttons(card, "[data-svc-remove]") if not is_hidden(b)]
    form = next(f for f in card.find_all("form") if remove_buttons(f, "[data-svc-remove]"))
    fields = {i.get("name"): i.get("value") or "" for i in form.find_all("input") if i.get("name")
              and i.get("type") not in ("password", "submit")}
    fields.pop("csrf_token", None)
    fields.update({"action": "remove", "api_key": ""})
    assert web.post(form.get("action"), fields, follow=True).status_code == 200
    after = soup(web, "/settings").select_one("#svc-enrow")
    assert [b for b in remove_buttons(after, "[data-svc-remove]") if not is_hidden(b)] == []


# ---------------------------------------------------------------------------------------------------------------
# 3. "N of M services connected" matches the tiles on the page
# ---------------------------------------------------------------------------------------------------------------

def summary_counts(s):
    node = s.select_one("[data-svc-summary]")
    assert node is not None, "the settings header must show the services summary"
    match = CONNECTED_COUNT.search(squash(node))
    assert match, f"the summary should read 'N of M services connected', got {squash(node)!r}"
    return int(match.group(1)), int(match.group(2))


def tile_counts(s):
    tiles = s.select("a.svc-tile")
    assert tiles, "the settings page must render service tiles"
    return sum(1 for t in tiles if t.get("data-state") == "ok"), len(tiles)


def test_the_header_total_is_the_number_of_service_tiles_on_the_page(make_web):
    s = soup(make_web(), "/settings")
    connected, total = summary_counts(s)
    assert total == len(s.select("a.svc-tile"))
    assert total != 13, "the old hard coded total of 13 is gone"
    assert 1 <= connected <= total


def test_the_header_connected_number_is_the_tiles_that_are_connected_on_a_blank_workspace(make_web):
    s = soup(make_web(), "/settings")
    assert summary_counts(s) == tile_counts(s)


def test_the_header_count_follows_the_tiles_as_services_get_connected(make_web):
    web = make_web({"SENDER_EMAIL": SENDER_EMAIL, "TEST_RECIPIENT_EMAIL": TEST_INBOX,
                    "ENROW_API_KEY": FAKE_KEY, "APOLLO_API_KEY": FAKE_KEY, "OPENAI_API_KEY": FAKE_KEY},
                   sender=True)
    blank = summary_counts(soup(make_web(), "/settings"))
    connect_gmail(web)
    s = soup(web, "/settings")
    shown = summary_counts(s)
    assert shown == tile_counts(s), "the number and the tiles must agree after connecting several services"
    assert shown[0] > blank[0], "connecting services must raise the connected number"
    assert shown[1] == blank[1], "connecting services does not change how many services there are"


def test_the_connected_tiles_say_so_in_their_own_status_chip(make_web):
    web = make_web()
    s = soup(web, "/settings")
    idle = s.select_one('a.svc-tile[href="#svc-gmail"]')
    assert idle is not None and idle.get("data-state") != "ok"
    connect_gmail(web)
    tile = soup(web, "/settings").select_one('a.svc-tile[href="#svc-gmail"]')
    assert tile is not None
    assert tile.get("data-state") == "ok", "a Gmail token on disk makes the Gmail tile connected"
    assert squash(tile.select_one("[data-svc-label]")), "a connected tile carries a status word"


# ---------------------------------------------------------------------------------------------------------------
# 4. Cards that tiles link to can take focus
# ---------------------------------------------------------------------------------------------------------------

def test_every_service_card_a_tile_links_to_can_take_script_focus(make_web):
    s = soup(make_web(), "/settings")
    targets = []
    for tile in s.select("a.svc-tile"):
        href = tile.get("href") or ""
        if href.startswith("#") and len(href) > 1:
            node = s.find(id=href[1:])
            assert node is not None, f"tile {href} points at nothing"
            if "svc-card" in (node.get("class") or []):
                targets.append(node)
    assert len(targets) >= 3, "several tiles link to a service card"
    for node in targets:
        assert node.get("tabindex") == "-1", f"#{node.get('id')} is an anchor target and needs tabindex=-1"


def test_the_named_service_cards_have_tabindex_minus_one(make_web):
    s = soup(make_web(), "/settings")
    for anchor in ("svc-gmail", "svc-test-recipient", "svc-enrow", "svc-apollo", "svc-publik"):
        node = s.find(id=anchor)
        assert node is not None, f"#{anchor} should exist"
        assert node.get("tabindex") == "-1", f"#{anchor} needs tabindex=-1"


# ---------------------------------------------------------------------------------------------------------------
# 5. Favicon
# ---------------------------------------------------------------------------------------------------------------

def test_the_favicon_is_served_to_a_browser_with_no_session(make_web):
    web = make_web()
    stranger = web.app.test_client(use_cookies=False)
    response = stranger.get("/favicon.ico", base_url=BASE)
    assert response.status_code == 200
    assert (response.headers.get("Content-Type") or "").lower().startswith("image/")
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert len(response.get_data()) > 20, "the icon has content"


def test_the_favicon_is_also_served_to_a_logged_in_browser(make_web):
    response = make_web().get("/favicon.ico")
    assert response.status_code == 200
    assert (response.headers.get("Content-Type") or "").lower().startswith("image/")
    assert response.headers.get("X-Frame-Options") == "DENY"


def test_the_favicon_cannot_be_posted_to(make_web):
    web = make_web()
    anonymous = web.app.test_client(use_cookies=False).post("/favicon.ico", base_url=BASE)
    assert anonymous.status_code >= 400, "POST /favicon.ico must be refused"
    assert web.post("/favicon.ico").status_code >= 400, "POST /favicon.ico with a valid csrf token is still refused"


@pytest.mark.parametrize("path", ["/crm", "/settings", "/partners", "/replies", "/setup/ai"])
def test_pages_with_the_base_layout_link_an_icon_that_loads(make_web, path):
    web = make_web()
    s = soup(web, path)
    icons = [link for link in s.find_all("link") if "icon" in (link.get("rel") or [])]
    assert icons, f"{path} must link a rel=icon"
    href = icons[0].get("href") or ""
    assert href, "the icon link has an address"
    response = web.get(href)
    assert response.status_code == 200
    assert (response.headers.get("Content-Type") or "").lower().startswith("image/")


# ---------------------------------------------------------------------------------------------------------------
# 6. No dead links, and the two retired pages are not linked
# ---------------------------------------------------------------------------------------------------------------

RETIRED = ("/crm/review-preview", "/inbox")


def same_origin_paths(s):
    paths = []
    for a in s.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "//", "mailto:", "tel:", "javascript:")):
            continue
        parts = urlsplit(href)
        if parts.scheme or parts.netloc:
            continue
        paths.append(parts.path)
    return paths


@pytest.fixture(params=["blank", "sample", "ready_to_send"])
def crawl_web(request):
    if request.param == "blank":
        return request.getfixturevalue("make_web")()
    if request.param == "sample":
        return request.getfixturevalue("sample_web")
    return request.getfixturevalue("send_web")([ADA, GRACE])


@pytest.mark.parametrize("path", PAGES)
def test_no_page_links_to_the_retired_review_preview_or_inbox_pages(crawl_web, path):
    s = soup(crawl_web, path)
    links = same_origin_paths(s)
    assert links, f"{path} should have links to other pages"
    for link in links:
        assert link.rstrip("/") not in RETIRED, f"{path} still links to {link}"
        assert not link.startswith("/crm/review-preview"), f"{path} still links to {link}"
    raw = [a["href"] for a in s.find_all("a", href=True)]
    assert not [h for h in raw if urlsplit(h).path.rstrip("/") in RETIRED]


def test_every_same_origin_link_on_the_main_pages_goes_somewhere(crawl_web):
    app = crawl_web.app
    adapter = app.url_map.bind("localhost")
    seen, checked = {}, 0
    for path in PAGES:
        for link in same_origin_paths(soup(crawl_web, path)):
            if link in seen or link.endswith((".csv", ".zip", ".json")):
                continue
            try:
                adapter.match(link, method="GET")
            except MethodNotAllowed:
                seen[link] = "post-only"
                continue  # a form style route, not something a link can open
            except NotFound:
                seen[link] = 404
                continue
            response = crawl_web.get(link, follow_redirects=True)
            seen[link] = response.status_code
            checked += 1
    dead = {link: code for link, code in seen.items() if code == 404}
    assert not dead, f"links that lead to a missing page: {dead}"
    assert checked >= 8, f"the crawl should have opened real pages, only {checked} were checked"
    assert {"/settings", "/crm"} <= set(seen), "the crawl must have reached the main navigation"
    assert all(code != 500 for code in seen.values()), f"a linked page crashed: {seen}"


# ---------------------------------------------------------------------------------------------------------------
# 7. Attachment label follows the campaign type
# ---------------------------------------------------------------------------------------------------------------

def attachment_web(send_web, kind, name="mydeck.pdf"):
    web = send_web([ADA])
    set_kind(web, kind)
    if name:
        set_deck(web, name)
    return web


@pytest.mark.parametrize("path", ["/crm/drafts", "/crm/drafts/ada"])
def test_a_fundraising_attachment_is_labelled_attached_file_with_its_name(send_web, path):
    web = attachment_web(send_web, "fundraising", "acme-deck.pdf")
    text = page_text(body(web.get(path, follow_redirects=True)))
    assert "Attached file: acme-deck.pdf" in text
    assert "Attached file (your resume)" not in text


@pytest.mark.parametrize("path", ["/crm/drafts", "/crm/drafts/ada"])
def test_a_job_search_attachment_is_labelled_as_your_resume_with_its_name(send_web, path):
    web = attachment_web(send_web, "job_search", "jane-cv.pdf")
    text = page_text(body(web.get(path, follow_redirects=True)))
    assert "Attached file (your resume): jane-cv.pdf" in text
    assert "Attached file: jane-cv.pdf" not in text, "the plain fundraising wording is not used for a resume"
    assert "resume" in text.lower()


def test_switching_the_campaign_type_switches_the_attachment_label(send_web):
    web = attachment_web(send_web, "fundraising", "same-file.pdf")
    assert "Attached file: same-file.pdf" in page_text(body(web.get("/crm/drafts", follow_redirects=True)))
    set_kind(web, "job_search")
    job = page_text(body(web.get("/crm/drafts", follow_redirects=True)))
    assert "Attached file (your resume): same-file.pdf" in job
    set_kind(web, "fundraising")
    assert "Attached file: same-file.pdf" in page_text(body(web.get("/crm/drafts", follow_redirects=True)))


@pytest.mark.parametrize("path", ["/crm/drafts", "/crm/drafts/ada"])
def test_the_bundled_example_deck_is_not_shown_as_the_job_search_attachment(send_web, path):
    web = attachment_web(send_web, "job_search", None)
    html = body(web.get(path, follow_redirects=True))
    text = page_text(html)
    assert "Publik pitch deck" not in text
    assert "Publik_Pitch_Deck_Example" not in html
    assert "Attached file" not in text, "with nothing attached there is no attached file line"
    assert not BeautifulSoup(html, "html.parser").select('a[href^="/crm/deck"], iframe[src^="/crm/deck"]')


# ---------------------------------------------------------------------------------------------------------------
# 8. Theme: follows the system, remembers a choice, adds no inline script
# ---------------------------------------------------------------------------------------------------------------

def test_the_theme_follows_the_system_setting_and_a_saved_choice(make_web):
    web = make_web()
    scripts = "\n".join(served_assets(web, "js").values())
    styles = "\n".join(served_assets(web, "css").values())
    assert "prefers-color-scheme" in scripts + styles, "the theme must react to the system light or dark setting"
    assert "localStorage" in scripts, "a saved theme choice lives in the browser"
    assert re.search(r"localStorage\s*\.\s*getItem\(\s*['\"][A-Za-z0-9_.:-]+['\"]", scripts), \
        "the saved choice is read back from a named key"
    assert re.search(r"localStorage\s*\.\s*setItem\(\s*['\"][A-Za-z0-9_.:-]+['\"]", scripts), \
        "a chosen theme is saved under a named key"
    key = re.search(r"localStorage\s*\.\s*getItem\(\s*['\"]([A-Za-z0-9_.:-]+)['\"]", scripts).group(1)
    assert re.search(r"setItem\(\s*['\"]" + re.escape(key) + r"['\"]", scripts), "the key it reads is the key it saves"


@pytest.mark.parametrize("path", PAGES)
def test_no_page_has_an_inline_script_or_inline_event_handler(crawl_web, path):
    html = body(crawl_web.get(path, follow_redirects=True))
    s = BeautifulSoup(html, "html.parser")
    inline = [t for t in s.find_all("script") if not t.get("src") and (t.get("type") or "").lower() not in
              ("application/json", "application/ld+json", "text/template")]
    assert inline == [], f"{path} carries inline script code"
    handlers = [(t.name, a) for t in s.find_all(True) for a in t.attrs if a.lower().startswith("on")]
    assert handlers == [], f"{path} has inline event handlers: {handlers[:3]}"


@pytest.mark.parametrize("path", ["/", "/crm", "/settings", "/setup/ai", "/favicon.ico"])
def test_the_page_security_policy_does_not_allow_inline_scripts(make_web, path):
    response = make_web().get(path)
    policy = response.headers.get("Content-Security-Policy") or ""
    script_src = next((d.strip() for d in policy.split(";") if d.strip().startswith("script-src")), "")
    assert script_src, f"{path} must carry a script-src policy"
    assert "unsafe-inline" not in script_src
    assert "unsafe-eval" not in script_src


# ---------------------------------------------------------------------------------------------------------------
# 9. Phone width: tap targets and fresh assets
# ---------------------------------------------------------------------------------------------------------------

def phone_rules(web):
    rules = []
    for path, css in served_assets(web, "css").items():
        for block in media_blocks(css, r"max-width:\s*768px"):
            rules += [(path, sel, decl) for sel, decl in rules_with(block, r"min-height:\s*40px")]
    return rules


def test_on_a_phone_the_filter_chips_are_at_least_forty_pixels_tall(sample_web):
    rules = phone_rules(sample_web)
    assert [r for r in rules if re.search(r"\.crm-chip\b", r[1])], "filter chips need min-height: 40px at 768px and below"
    assert soup(sample_web, "/crm").select(".crm-chip"), "the CRM page really uses those chips"


def test_on_a_phone_the_sort_buttons_are_at_least_forty_pixels_tall(sample_web):
    rules = phone_rules(sample_web)
    sort_selectors = [r for r in rules if "sort" in r[1]]
    assert sort_selectors, "sort buttons need min-height: 40px at 768px and below"
    classes = {c for sel in sort_selectors for c in re.findall(r"\.([A-Za-z0-9_-]*sort[A-Za-z0-9_-]*)", sel[1])}
    used = set()
    for path in ("/crm", "/partners", "/replies", "/enrichment"):
        for node in soup(sample_web, path).find_all(True):
            used |= set(node.get("class") or [])
    assert classes & used, f"the sort button class rules {sorted(classes)} must match something on a page"


def test_the_phone_rules_apply_only_at_phone_width(sample_web):
    for path, css in served_assets(sample_web, "css").items():
        outside = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
        flat = [r for r in rules_with(outside, r"min-height:\s*40px") if re.search(r"\.crm-chip\b", r[0])]
        assert flat == [], f"{path} forces 40px chips at every width"


def test_the_stylesheets_and_script_are_not_served_with_the_stale_cache_buster(make_web):
    web = make_web()
    html = body(web.get("/crm"))
    tags = {name: re.search(r'(?:href|src)="/static/(?:pages/)?' + re.escape(name) + r'\?v=([^"]+)"', html)
            for name in ("style.css", "crm.css", "app.js")}
    for name, match in tags.items():
        assert match, f"{name} must be linked with a ?v= cache buster"
        assert not match.group(1).endswith("ux8"), f"{name} still carries the old cache buster {match.group(1)}"
    assert "ux8" not in {m.group(1) for m in tags.values()}


def test_no_page_still_links_an_asset_with_the_stale_cache_buster(crawl_web):
    for path in ("/crm", "/settings", "/crm/drafts", "/partners"):
        html = body(crawl_web.get(path, follow_redirects=True))
        stale = re.findall(r'/static/[^"]*\?v=[^"]*ux8"', html)
        assert stale == [], f"{path} links {stale}"


# ---------------------------------------------------------------------------------------------------------------
# Dash hygiene for what these pages show
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/settings", "/crm/drafts"])
def test_the_touched_pages_carry_no_em_or_en_dash(make_web, path):
    text = page_text(body(make_web().get(path, follow_redirects=True)))
    assert EM_DASH not in text and EN_DASH not in text
