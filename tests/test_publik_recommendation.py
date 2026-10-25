"""The Publik API recommendation: the optional last banner step, Skip, Undo, the Connections card.

Spec only. Observed through the Flask test client on a throwaway workspace, no network, obviously fake key values.
No dash characters appear in this file.

Spec ambiguities, each noted where it matters:

* Spec 3 names the skip link, the "or set up with AI" link and the card chips by text and ``data-`` attribute. It does
  not say whether the skip control is an anchor or a button, so the tests find it by its data attribute only.
* Spec 3 says a bad ``state`` value is not stored. Whether the route answers 400 or ignores it is not specified, so
  the tests only require that no other value reaches ``publik_recommendation``.
* ``POST /api/publik/test`` is checked for its shape with no key saved. With a key, the provider transport
  (``UrllibTransport.request``) or DNS is faked so these offline tests never reach the Publik host.
"""
from __future__ import annotations

import json
import re

import pytest
from bs4 import BeautifulSoup

from web_helpers import *  # noqa: F401,F403
from web_helpers import ADA, Web, body, create_web_app, page_text, send_test

PUBLIK_LABEL = "Optional: connect the Publik API for AI drafts"
SKIP_TEXT = "Skip, use my own key or the offline template"
AI_LINK_TEXT = "or set up with AI"
CARD_HINT_PARTS = (
    "recommended for AI drafts",
    "connect the Publik API for better personalization",
    "your own OpenAI, Anthropic or OpenRouter key",
    "free offline template",
)
COST_LINE = (
    "Publik API calls cost real money. See the pricing information on https://publikhq.com before you start. "
    "Offline mode is free. A contact with no facts never triggers a request."
)
PRIVACY_LINE = (
    "For each contact: the recipient's name, org, role and facts, and your name and org. Requests go only to the "
    "Publik host you configured."
)
KEY_PLACEHOLDER = "Paste key, never shown again"
FAKE_PUBLIK_KEY = "pk_live_ZZ-FAKE-publik-key-5f3a91c2d7e84b06"
PRICE_PATTERNS = (re.compile(r"\$\s?\d"), re.compile(r"\d+\s?(?:USD|cents)\b", re.I))


def soup(web: Web, path: str) -> BeautifulSoup:
    response = web.get(path)
    assert response.status_code == 200, f"GET {path} returned {response.status_code}"
    return BeautifulSoup(body(response), "html.parser")


def banner(web: Web, path: str = "/crm"):
    return soup(web, path).select_one("#nextStep")


def banner_text(web: Web, path: str = "/crm") -> str:
    node = banner(web, path)
    assert node is not None, f"no Next step banner on {path}"
    return " ".join(node.get_text(" ").split())


def card(web: Web):
    node = soup(web, "/settings").select_one("#svc-publik")
    assert node is not None, "spec 3: the Settings page has the svc-publik card"
    return node


def card_text(web: Web) -> str:
    return " ".join(card(web).get_text(" ").split())


def stored(web: Web) -> dict:
    path = web.ws / "data" / "workspace-settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def skip(web: Web):
    return web.post("/settings/publik-recommendation", {"state": "skipped"})


def undo(web: Web):
    return web.post("/settings/publik-recommendation", {"state": "pending"})


def save_key(web: Web, value: str = FAKE_PUBLIK_KEY):
    return web.post("/api/model-provider/key", {"provider": "publik", "api_key": value})


def clear_key(web: Web):
    return web.post("/api/model-provider/key", {"provider": "publik", "action": "clear"})


# ---------------------------------------------------------------------------------------------------------------
# The banner: Publik is the optional last step, never the first
# ---------------------------------------------------------------------------------------------------------------

def connect_gmail(web):
    web.app.extensions["workspace"].path("data", ".gmail_token.json").write_text(
        json.dumps({"refresh_token": "fake-refresh", "token": "fake-access"}))


def at_publik_step(send_web):
    """A workspace where steps 1 to 6 are done, so the only thing left in the banner is the optional Publik step."""
    web = send_web([ADA])
    connect_gmail(web)
    assert send_test(web, "ada").status_code == 200
    return web


@pytest.mark.parametrize("path", ["/crm", "/partners"])
def test_a_fresh_workspace_is_asked_for_gmail_not_for_publik(make_web, path):
    web = make_web()
    text = banner_text(web, path)
    assert "Step 1 of 7" in text
    assert PUBLIK_LABEL not in text and "Publik API" not in text
    link = banner(web, path).select_one("a[data-next-step]")
    assert (link["data-next-step"], link["href"]) == ("gmail", "/settings#svc-gmail")


@pytest.mark.parametrize("path", ["/crm", "/partners"])
def test_publik_is_the_optional_last_step_once_the_other_six_are_done(send_web, path):
    web = at_publik_step(send_web)
    text = banner_text(web, path)
    assert "Step 7 of 7" in text
    assert PUBLIK_LABEL in text
    link = banner(web, path).select_one("a[data-next-step]")
    assert (link["data-next-step"], link["href"]) == ("publik_api", "/settings#svc-publik")


@pytest.mark.parametrize("where", ["fresh", "last_step"])
def test_the_banner_has_no_skip_form_and_no_set_up_with_ai_link(make_web, send_web, where):
    web = make_web() if where == "fresh" else at_publik_step(send_web)
    node = banner(web, "/crm")
    assert node is not None
    assert node.select_one("form") is None, "the banner must not carry a skip form"
    assert node.select_one("[data-next-step-skip]") is None
    assert node.select_one("[data-next-step-ai]") is None
    text = banner_text(web, "/crm")
    assert SKIP_TEXT not in text and AI_LINK_TEXT not in text


# ---------------------------------------------------------------------------------------------------------------
# Skip, Undo, persistence (the Skip and Undo controls live on the Settings card)
# ---------------------------------------------------------------------------------------------------------------

def test_skipping_publik_ends_the_banner_once_the_other_six_steps_are_done(send_web):
    web = at_publik_step(send_web)
    assert skip(web).status_code in (200, 302, 303)
    for path in ("/crm", "/crm/drafts", "/partners"):
        assert banner(web, path) is None, f"the banner must be gone on {path} after the last step is skipped"


def test_skipping_on_a_fresh_workspace_does_not_hide_the_gmail_step(make_web):
    web = make_web()
    assert skip(web).status_code in (200, 302, 303)
    text = banner_text(web)
    assert "Step 1 of 7" in text
    link = banner(web).select_one("a[data-next-step]")
    assert (link["data-next-step"], link["href"]) == ("gmail", "/settings#svc-gmail")


def test_undo_brings_the_last_step_back(send_web):
    web = at_publik_step(send_web)
    skip(web)
    assert undo(web).status_code in (200, 302, 303)
    assert "Step 7 of 7" in banner_text(web)
    assert PUBLIK_LABEL in banner_text(web)
    assert stored(web).get("publik_recommendation") == "pending"


def test_a_skip_survives_a_restart_of_the_app(send_web):
    web = at_publik_step(send_web)
    skip(web)
    reopened = Web(create_web_app(web.ws, None), web.ws, None)
    reopened.login()
    assert banner(reopened) is None
    assert "Skipped" in card_text(reopened)


def test_a_skip_without_a_csrf_token_is_a_400_and_stores_nothing(make_web):
    web = make_web()
    response = web.post("/settings/publik-recommendation", {"state": "skipped"}, csrf=False)
    assert response.status_code == 400
    assert stored(web).get("publik_recommendation", "pending") == "pending"
    assert "Step 1 of 7" in banner_text(web)


def test_an_unknown_state_is_never_stored(make_web):
    web = make_web()
    web.post("/settings/publik-recommendation", {"state": "hacked"})
    assert stored(web).get("publik_recommendation", "pending") in ("pending", "skipped")
    assert stored(web).get("publik_recommendation", "pending") != "hacked"
    assert "Step 1 of 7" in banner_text(web)


# ---------------------------------------------------------------------------------------------------------------
# A saved key
# ---------------------------------------------------------------------------------------------------------------

def test_a_saved_key_makes_the_card_connected_and_ends_the_banner_at_the_last_step(send_web):
    web = at_publik_step(send_web)
    save_key(web)
    assert "Connected" in card_text(web)
    assert banner(web) is None


def test_a_saved_key_on_a_fresh_workspace_leaves_gmail_as_step_one(make_web):
    web = make_web()
    save_key(web)
    assert "Connected" in card_text(web)
    assert "Step 1 of 7" in banner_text(web)
    assert banner(web).select_one("a[data-next-step]")["data-next-step"] == "gmail"


def test_removing_the_key_brings_the_optional_step_back(send_web):
    web = at_publik_step(send_web)
    save_key(web)
    clear_key(web)
    assert "Connected" not in card_text(web)
    assert "Not connected" in card_text(web)
    assert "Optional" in card_text(web)
    assert "Step 7 of 7" in banner_text(web)
    assert PUBLIK_LABEL in banner_text(web)


def test_a_saved_key_never_appears_in_any_page(make_web):
    web = make_web()
    save_key(web)
    for path in ("/settings", "/setup/ai", "/crm", "/partners", "/crm/drafts"):
        assert FAKE_PUBLIK_KEY not in web.html(path), path
        assert FAKE_PUBLIK_KEY[:16] not in web.html(path), path


def test_removing_the_key_brings_the_recommendation_back(make_web):
    web = make_web()
    save_key(web)
    clear_key(web)
    assert "Connected" not in card_text(web)
    assert "Not connected" in card_text(web)
    assert "Optional" in card_text(web)
    assert "Step 1 of 7" in banner_text(web)


# ---------------------------------------------------------------------------------------------------------------
# The Connections card
# ---------------------------------------------------------------------------------------------------------------

def test_the_card_chip_says_skipped_with_an_undo_link_after_a_skip(make_web):
    web = make_web()
    skip(web)
    node = card(web)
    assert "Skipped" in " ".join(node.get_text(" ").split())
    assert any(t.strip() == "Undo" or "Undo" in t for t in node.stripped_strings), "spec 3: an Undo control"


def test_the_card_carries_the_exact_hint_cost_and_privacy_lines(make_web):
    web = make_web()
    text = card_text(web)
    for part in CARD_HINT_PARTS:
        assert part in text, part
    assert "Recommended" not in text, "the card is marked Optional, not Recommended"
    assert COST_LINE in text
    assert PRIVACY_LINE in text


def test_the_publik_card_is_the_last_service_card_and_marked_optional(make_web):
    web = make_web()
    cards = soup(web, "/settings").select("article.svc-card")
    assert cards and cards[-1].get("id") == "svc-publik", "the Publik API card must be the last service card"
    text = card_text(web)
    assert "Optional" in text and "Recommended" not in text


def test_the_card_status_word_is_connected_not_connected_or_skipped(make_web):
    web = make_web()
    assert "Not connected" in card_text(web)
    skip(web)
    assert "Skipped" in card_text(web) and "Connected" not in card_text(web)
    undo(web)
    save_key(web)
    text = card_text(web)
    assert "Connected" in text and "Not connected" not in text


def test_the_card_has_a_password_field_for_the_key_and_the_save_and_test_buttons(make_web):
    web = make_web()
    node = card(web)
    field = node.select_one('input[name="api_key"]')
    assert field is not None
    assert field.get("type") == "password"
    assert field.get("placeholder") == KEY_PLACEHOLDER
    labels = {" ".join(b.get_text(" ").split()) for b in node.select("button")}
    assert "Save key" in labels
    assert "Test" in labels


# ---------------------------------------------------------------------------------------------------------------
# No numeric price, anywhere
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/settings", "/crm"])
def test_no_page_states_a_numeric_price_for_the_publik_api(make_web, path):
    web = make_web()
    text = page_text(web.html(path))
    for pattern in PRICE_PATTERNS:
        for match in pattern.finditer(text):
            context = text[max(0, match.start() - 40): match.end() + 40]
            assert "Publik" not in context and "API" not in context, f"{path}: {context!r}"


# ---------------------------------------------------------------------------------------------------------------
# POST /api/publik/test
# ---------------------------------------------------------------------------------------------------------------

def test_the_test_route_returns_only_ok_and_message(make_web):
    web = make_web()
    response = web.post("/api/publik/test", {})
    assert response.status_code == 200
    data = response.get_json()
    assert set(data) == {"ok", "message"}, data
    assert data["ok"] is False, "no key is saved, so the test cannot pass"
    assert isinstance(data["message"], str) and data["message"]
    assert web.transport is None or not getattr(web.transport, "calls", [])


def test_the_test_route_never_echoes_a_saved_key_when_the_network_fails(make_web, monkeypatch):
    """The suite blocks real DNS, so the probe is made to fail like an offline laptop (an ordinary OSError, which a
    route has to survive). Whatever the route answers, the saved key is not in it (spec 4.3)."""
    import socket

    def offline(*args, **kwargs):
        raise OSError("simulated offline: name resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", offline)
    web = make_web()
    save_key(web)
    response = web.post("/api/publik/test", {})
    assert FAKE_PUBLIK_KEY not in response.get_data(as_text=True)


def test_the_test_route_never_echoes_a_saved_key_when_the_provider_echoes_it(make_web, monkeypatch):
    """A hostile or buggy upstream that reflects the Authorization header in its error body must not make the
    route reflect the key to the browser (spec 4.3). The existing provider transport (UrllibTransport.request, the
    seam every provider call in the app goes through) is faked."""
    from vcoutreach.providers import http as _http

    body = ('{"error":"bad key %s"}' % FAKE_PUBLIK_KEY).encode()
    calls = []

    def reflect(self, method, url, headers, payload, timeout):
        calls.append(url)
        return _http.HttpResponse(401, {"content-type": "application/json"}, body)

    # Spec 5.2: one probe per 30 s per service, then a cached answer. Earlier tests in this file may already have
    # probed in this process, so the clock is moved past the window to get a real probe.
    import time

    real_monotonic, real_time = time.monotonic, time.time
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 100000.0)
    monkeypatch.setattr(time, "time", lambda: real_time() + 100000.0)
    monkeypatch.setattr(_http.UrllibTransport, "request", reflect)
    web = make_web()
    save_key(web)
    response = web.post("/api/publik/test", {})
    assert FAKE_PUBLIK_KEY not in response.get_data(as_text=True)
    assert calls, "the probe must have reached the faked transport (otherwise the seam assumption is wrong)"


# ---------------------------------------------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------------------------------------------

