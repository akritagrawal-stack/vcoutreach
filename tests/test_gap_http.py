"""Gaps found by the second mutation run: the HTTP layer and the three network providers (SPEC sections 10 and 18.3).

Loopback servers here are the suite's LocalGateway, which binds 127.0.0.1 only.
"""
from __future__ import annotations

import dataclasses

import pytest

from vcoutreach.providers import AnthropicProvider, OpenAICompatProvider, PublikProvider
from vcoutreach.providers.http import UrllibTransport, send_with_retries
from vcoutreach.types import ProviderConfigError, ProviderError

URL = "https://gateway.test/api/v1/chat/completions"
HEADERS = {"Authorization": "Bearer abc", "Content-Type": "application/json"}
BODY = b'{"hello": "world"}'

PUBLIK_KEY = "pk_fake_PUBLIK00000000000000AAAA"
OPENAI_KEY = "ok_fake_OPENAI00000000000000BBBB"
ANTHROPIC_KEY = "ak_fake_ANTHR00000000000000CCCC"
KEYS = {"publik": PUBLIK_KEY, "openai": OPENAI_KEY, "anthropic": ANTHROPIC_KEY}
KEY_FIELDS = {"publik": "publik_api_key", "openai": "openai_api_key", "anthropic": "anthropic_api_key"}
PROVIDERS = ["publik", "openai", "anthropic"]
GOOD_TEXT = "Your essay on how small teams ship weekly stayed with me, and I respect the focus."


def provider_for(kit, which, ft, **replacements):
    """A provider whose Settings were adjusted AFTER loading, so only the provider's own checks can object."""
    if which == "publik":
        settings = kit.publik_settings(PUBLIK_API_KEY=PUBLIK_KEY, PUBLIK_MAX_RETRIES="0")
        cls = PublikProvider
    elif which == "openai":
        settings = kit.settings(OPENAI_API_KEY=OPENAI_KEY, OPENAI_MODEL="gpt-test", PUBLIK_MAX_RETRIES="0")
        cls = OpenAICompatProvider
    else:
        settings = kit.settings(ANTHROPIC_API_KEY=ANTHROPIC_KEY, ANTHROPIC_MODEL="claude-test", PUBLIK_MAX_RETRIES="0")
        cls = AnthropicProvider
    if replacements:
        settings = dataclasses.replace(settings, **replacements)
    return cls(settings, transport=ft, sleep=kit.sleeper(), rng=kit.rng(0.0))


def ok_reply(kit, which, text, headers=None):
    return kit.anthropic_ok(text, headers=headers) if which == "anthropic" else kit.chat_ok(text, headers=headers)


# ----- a redirect is an answer, never an instruction (UrllibTransport returns every status without raising) ---------------------------

@pytest.mark.local_http
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_returned_not_followed_and_the_target_is_never_contacted(gateway, status):
    target = type(gateway)()
    try:
        target.reply(200, body=b"you should never get here")
        gateway.reply(status, headers={"Location": target.origin + "/landed"})
        resp = UrllibTransport().request("POST", gateway.base_url + "/chat/completions", HEADERS, BODY, 5.0)
        leaked = list(target.requests)
    finally:
        target.stop()
    assert resp.status == status
    assert b"never get here" not in resp.body
    assert len(gateway.requests) == 1
    assert leaked == [], "the Authorization header must never reach the host a redirect points at"


@pytest.mark.local_http
def test_a_provider_never_sends_its_key_to_the_host_a_redirect_points_at(gateway, kit):
    target = type(gateway)()
    try:
        gateway.reply(302, headers={"Location": target.origin + "/steal"})
        settings = kit.publik_settings(PUBLIK_API_BASE_URL=gateway.base_url, PUBLIK_MAX_RETRIES="0", PUBLIK_TIMEOUT_SECONDS="5")
        try:
            PublikProvider(settings).personalize(kit.request())
        except ProviderError:
            pass  # a 302 is no answer; how it is reported is not the point here
        leaked = list(target.requests)
    finally:
        target.stop()
    assert leaked == []
    assert len(gateway.requests) == 1


# ----- Retry-After is used only when it is a number (SPEC 10: "when numeric") --------------------------------------------------------------

JUNK_RETRY_AFTER = ["5abc", "1.5.2", "5 seconds", "abc", "5,6", "five", "0x10"]


@pytest.mark.parametrize("junk", JUNK_RETRY_AFTER, ids=JUNK_RETRY_AFTER)
def test_a_retry_after_that_is_not_a_number_falls_back_to_the_exponential_delay(kit, junk):
    ft = kit.fake_transport(kit.response(429, headers={"Retry-After": junk}, body=b"{}"), kit.response(200, body=b"ok"))
    sleeper = kit.sleeper()
    resp = send_with_retries(ft, "POST", URL, HEADERS, BODY, timeout=5.0, max_retries=2, sleep=sleeper, rng=kit.rng(0.0))
    assert resp.status == 200
    assert sleeper.calls == [pytest.approx(1.0)]


def test_a_numeric_retry_after_is_still_honoured(kit):
    ft = kit.fake_transport(kit.response(503, headers={"Retry-After": "7"}, body=b"{}"), kit.response(200, body=b"ok"))
    sleeper = kit.sleeper()
    send_with_retries(ft, "POST", URL, HEADERS, BODY, timeout=5.0, max_retries=2, sleep=sleeper, rng=kit.rng(0.0))
    assert sleeper.calls == [pytest.approx(7.0)]


# ----- the API key must be fit to travel in a header ("ProviderConfigError: missing or malformed settings") ---------------------------

BAD_KEYS = {
    "space": "abcdefgh ijklmnop",
    "newline": "abcdefgh\nijklmnop",
    "carriage-return": "abcdefgh\rijklmnop",
    "non-ascii": "abcdefgh" + chr(0xE9) + "ijklmnop",
}


@pytest.mark.parametrize("which", PROVIDERS)
@pytest.mark.parametrize("kind", list(BAD_KEYS), ids=list(BAD_KEYS))
def test_a_key_with_spaces_line_breaks_or_non_ascii_is_a_config_error_before_any_request(kit, which, kind):
    ft = kit.fake_transport(ok_reply(kit, which, GOOD_TEXT))
    with pytest.raises(ProviderConfigError):
        provider_for(kit, which, ft, **{KEY_FIELDS[which]: BAD_KEYS[kind]}).personalize(kit.request())
    assert ft.requests == []


@pytest.mark.parametrize("which", PROVIDERS)
def test_an_ordinary_key_still_works(kit, which):
    ft = kit.fake_transport(ok_reply(kit, which, GOOD_TEXT))
    assert provider_for(kit, which, ft).personalize(kit.request()).text == GOOD_TEXT
    assert len(ft.requests) == 1


# ----- the base URL must be http or https -----------------------------------------------------------------------------------------------

BAD_URLS = ["ftp://host/v1", "javascript://x/y", "gopher://host/v1", "ws://host/v1", "file:///etc/passwd"]
URL_FIELDS = [("publik", "publik_api_base_url"), ("openai", "openai_base_url")]


@pytest.mark.parametrize("which,field", URL_FIELDS, ids=[w for w, _ in URL_FIELDS])
@pytest.mark.parametrize("url", BAD_URLS, ids=["ftp", "javascript", "gopher", "ws", "file"])
def test_a_base_url_that_is_not_http_or_https_is_a_config_error_before_any_request(kit, which, field, url):
    ft = kit.fake_transport(ok_reply(kit, which, GOOD_TEXT))
    with pytest.raises(ProviderConfigError):
        provider_for(kit, which, ft, **{field: url}).personalize(kit.request())
    assert ft.requests == []


@pytest.mark.parametrize("which,field", URL_FIELDS, ids=[w for w, _ in URL_FIELDS])
@pytest.mark.parametrize("url", ["https://gateway.test/api/v1", "http://127.0.0.1:8080/v1"], ids=["https", "http"])
def test_http_and_https_base_urls_are_used(kit, which, field, url):
    ft = kit.fake_transport(ok_reply(kit, which, GOOD_TEXT))
    provider_for(kit, which, ft, **{field: url}).personalize(kit.request())
    assert ft.requests[0].url.startswith(url)


# ----- timeout and retry settings that bypassed load_settings (the provider is a second layer) -----------------------------------------

@pytest.mark.parametrize("value", [float("inf"), 0.0, -1.0], ids=["infinite", "zero", "negative"])
def test_a_timeout_that_is_not_a_positive_finite_number_is_a_config_error(kit, value):
    ft = kit.fake_transport(kit.chat_ok(GOOD_TEXT))
    with pytest.raises(ProviderConfigError):
        provider_for(kit, "publik", ft, publik_timeout_seconds=value).personalize(kit.request())
    assert ft.requests == []


def test_a_negative_retry_count_is_a_config_error(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_TEXT))
    with pytest.raises(ProviderConfigError):
        provider_for(kit, "publik", ft, publik_max_retries=-1).personalize(kit.request())
    assert ft.requests == []


def test_zero_retries_means_exactly_one_attempt(kit):
    ft = kit.fake_transport(kit.error_response(503, "busy"))
    with pytest.raises(ProviderError):
        provider_for(kit, "publik", ft, publik_max_retries=0).personalize(kit.request())
    assert len(ft.requests) == 1


# ----- the key never reaches a draft through the model's reply or the served model name (SPEC 18.3 and 10: "never ... a draft file") ---

@pytest.mark.parametrize("which", PROVIDERS)
def test_a_key_echoed_in_the_model_reply_is_redacted_from_the_hook_text(kit, which):
    key = KEYS[which]
    text = f"Your essay on small teams, {key}, stayed with me, and {key} is why I respect it."
    ft = kit.fake_transport(ok_reply(kit, which, text))
    hook = provider_for(kit, which, ft).personalize(kit.request())
    assert key not in hook.text and key not in repr(hook)
    assert "***" in hook.text


def test_a_key_echoed_in_the_served_model_name_is_redacted_from_the_hook_model(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_TEXT, headers={"x-publik-model": f"publik-fast-{PUBLIK_KEY}"}))
    hook = provider_for(kit, "publik", ft).personalize(kit.request())
    assert PUBLIK_KEY not in hook.model and PUBLIK_KEY not in repr(hook)
    assert hook.model.startswith("publik-fast-")


def test_an_ordinary_served_model_name_is_kept(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_TEXT, headers={"x-publik-model": "publik-smart"}))
    assert provider_for(kit, "publik", ft).personalize(kit.request()).model == "publik-smart"
