"""providers.PublikProvider (SPEC section 10), through a FakeTransport and a real local gateway."""
from __future__ import annotations

import json
import time

import pytest

import vcoutreach
from vcoutreach.prompts import build_messages
from vcoutreach.providers import PublikProvider
from vcoutreach.types import (
    Hook,
    InsufficientFacts,
    ProviderAuthError,
    ProviderConfigError,
    ProviderCreditError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
)

KEY = "fk_test_0123456789abcdefZQ4W"
GOOD_HOOK = "I read your essay on how small teams ship weekly, and I respect the focus."
DASHBOARD = "https://publikhq.com/dashboard/api"


def build(kit, ft, *, sleeper=None, rng=None, **env):
    sleeper = sleeper if sleeper is not None else kit.sleeper()
    settings = kit.publik_settings(**env)
    provider = PublikProvider(settings, transport=ft, sleep=sleeper, rng=rng or kit.rng(0.0))
    return provider, sleeper


def ask(kit, ft, request=None, **env):
    provider, sleeper = build(kit, ft, **env)
    return provider.personalize(request or kit.request()), sleeper


def err_text(exc):
    return f"{exc!s} | {exc!r} | {exc.args!r}"


# ----- the request on the wire ---------------------------------------------------------------------------------

def test_posts_to_chat_completions_on_the_default_base_url_with_exact_headers(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    ask(kit, ft)
    (req,) = ft.requests
    assert req.method == "POST"
    assert req.url == "https://publikhq.com/api/v1/chat/completions"
    assert req.header("authorization") == f"Bearer {KEY}"
    assert req.header("content-type") == "application/json"
    assert req.header("accept") == "application/json"
    assert req.header("user-agent") == f"vcoutreach/{vcoutreach.__version__}"
    assert req.timeout == 30.0


def test_body_is_exactly_model_messages_temperature_and_max_tokens(kit):
    request = kit.request()
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    ask(kit, ft, request)
    body = ft.requests[0].json()
    assert set(body) == {"model", "messages", "temperature", "max_tokens"}
    assert body["model"] == "publik-balanced"
    assert body["temperature"] == 0.4
    assert body["max_tokens"] == 300
    assert body["messages"] == build_messages(request)
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_body_is_valid_utf8_json_bytes_even_with_unicode_facts(kit):
    contact = kit.contact(facts=("wrote about caf\u00e9 culture in \u65e5\u672c\u8a9e",))
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    ask(kit, ft, kit.request(contact=contact))
    raw = ft.requests[0].body
    assert isinstance(raw, bytes)
    parsed = json.loads(raw.decode("utf-8"))
    assert "caf\u00e9 culture" in parsed["messages"][1]["content"]


@pytest.mark.parametrize("base", ["https://gw.example.test/v9", "https://gw.example.test/v9/", "https://gw.example.test/v9///"])
def test_trailing_slashes_on_the_base_url_are_stripped(kit, base):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    ask(kit, ft, PUBLIK_API_BASE_URL=base)
    assert ft.requests[0].url == "https://gw.example.test/v9/chat/completions"


@pytest.mark.parametrize("model", ["publik-fast", "publik-balanced", "publik-smart", "publik-default"])
def test_every_allowed_model_is_sent_as_requested(kit, model):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    hook, _ = ask(kit, ft, PUBLIK_MODEL=model)
    assert ft.requests[0].json()["model"] == model
    assert hook.model == model


@pytest.mark.parametrize("model", ["gpt-4o", "publik-ultra", "publik fast", "publik"])
def test_unknown_model_names_are_a_config_error_before_any_request(kit, model):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    with pytest.raises(ProviderConfigError):
        provider, _ = build(kit, ft, PUBLIK_MODEL=model)
        provider.personalize(kit.request())
    assert ft.requests == []


def test_missing_key_is_a_config_error_before_any_request(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    with pytest.raises(ProviderConfigError):
        provider = PublikProvider(kit.settings(), transport=ft, sleep=kit.sleeper(), rng=kit.rng(0.0))
        provider.personalize(kit.request())
    assert ft.requests == []


def test_timeout_setting_is_passed_to_the_transport(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    ask(kit, ft, PUBLIK_TIMEOUT_SECONDS="12.5")
    assert ft.requests[0].timeout == 12.5


def test_default_max_retries_is_three_extra_attempts(kit):
    ft = kit.fake_transport(*[kit.response(503) for _ in range(4)])
    with pytest.raises(ProviderTransportError):
        ask(kit, ft)
    assert len(ft.requests) == 4


def test_max_retries_setting_is_honoured(kit):
    ft = kit.fake_transport(*[kit.response(503) for _ in range(2)])
    with pytest.raises(ProviderTransportError):
        ask(kit, ft, PUBLIK_MAX_RETRIES="1")
    assert len(ft.requests) == 2


def test_request_body_over_four_million_bytes_is_rejected_before_any_request(kit):
    contact = kit.contact(facts=("a" * 4_100_000,))
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    with pytest.raises(ProviderConfigError):
        ask(kit, ft, kit.request(contact=contact))
    assert ft.requests == []


def test_large_but_allowed_request_body_is_sent(kit):
    contact = kit.contact(facts=("a" * 3_900_000,))
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    hook, _ = ask(kit, ft, kit.request(contact=contact))
    assert hook.text == GOOD_HOOK
    assert len(ft.requests[0].body) <= 4_000_000


# ----- success parsing ----------------------------------------------------------------------------------------------------

def test_success_returns_a_stripped_hook_from_the_publik_provider(kit):
    ft = kit.fake_transport(kit.chat_ok(f"  \n{GOOD_HOOK}\n  "))
    hook, sleeper = ask(kit, ft)
    assert isinstance(hook, Hook)
    assert hook.text == GOOD_HOOK
    assert hook.provider == "publik"
    assert hook.model == "publik-balanced"
    assert dict(hook.meta) == {}
    assert len(ft.requests) == 1 and sleeper.calls == []


def test_provider_name_is_publik(kit):
    provider, _ = build(kit, kit.fake_transport())
    assert provider.name == "publik"


def test_hook_model_prefers_the_x_publik_model_response_header(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK, headers={"X-Publik-Model": "publik-balanced-2026-10"}))
    hook, _ = ask(kit, ft)
    assert hook.model == "publik-balanced-2026-10"


def test_balance_and_charge_headers_land_in_meta_as_integers(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK, headers={
        "x-publik-balance": "12345678", "x-publik-charge-micros": "4200",
    }))
    hook, _ = ask(kit, ft)
    assert int(hook.meta["balance_micros"]) == 12345678
    assert int(hook.meta["charge_micros"]) == 4200
    assert set(hook.meta) == {"balance_micros", "charge_micros"}


@pytest.mark.parametrize("garbled", ["12abc", "-5", "1.5", "", " ", "NaN", "1e3", "0x10", "1_000", "\u00b2", "+7", "twelve"])
def test_garbled_balance_header_is_ignored_never_guessed(kit, garbled):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK, headers={
        "x-publik-balance": garbled, "x-publik-charge-micros": "4200",
    }))
    hook, _ = ask(kit, ft)
    assert "balance_micros" not in hook.meta
    assert int(hook.meta["charge_micros"]) == 4200


@pytest.mark.parametrize("garbled", ["12abc", "-1", "0.5", "n/a"])
def test_garbled_charge_header_is_ignored_while_balance_survives(kit, garbled):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK, headers={
        "x-publik-balance": "900", "x-publik-charge-micros": garbled,
    }))
    hook, _ = ask(kit, ft)
    assert "charge_micros" not in hook.meta
    assert int(hook.meta["balance_micros"]) == 900


def test_zero_is_a_valid_balance(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK, headers={"x-publik-balance": "0"}))
    hook, _ = ask(kit, ft)
    assert int(hook.meta["balance_micros"]) == 0


@pytest.mark.parametrize("content", ["NO_HOOK", "  NO_HOOK  ", "\nNO_HOOK\n"])
def test_no_hook_answer_means_insufficient_facts(kit, content):
    ft = kit.fake_transport(kit.chat_ok(content))
    with pytest.raises(InsufficientFacts):
        ask(kit, ft)


def test_a_hook_that_merely_mentions_no_hook_inside_a_sentence_is_still_a_hook(kit):
    text = "I respect that you never say NO_HOOK to a good essay."
    hook, _ = ask(kit, kit.fake_transport(kit.chat_ok(text)))
    assert hook.text == text


@pytest.mark.parametrize(
    "response_factory",
    [
        lambda kit: kit.chat_ok(""),
        lambda kit: kit.chat_ok("   \n  "),
        lambda kit: kit.chat_ok(None),
        lambda kit: kit.response(200, json_body={"choices": []}),
        lambda kit: kit.response(200, json_body={"choices": [{}]}),
        lambda kit: kit.response(200, json_body={"choices": [{"message": {}}]}),
        lambda kit: kit.response(200, json_body={"choices": [{"message": {"content": ""}}]}),
        lambda kit: kit.response(200, json_body={}),
        lambda kit: kit.response(200, json_body=[]),
        lambda kit: kit.response(200, json_body="a bare string"),
        lambda kit: kit.response(200, json_body=None, body=b"null"),
        lambda kit: kit.response(200, body=b""),
        lambda kit: kit.response(200, body=b"<html><body>Sign in to the hotel wifi</body></html>"),
        lambda kit: kit.response(200, body=b"{not json"),
        lambda kit: kit.response(200, body=b"\xff\xfe\x00garbage"),
    ],
    ids=["empty", "blank", "null-content", "no-choices", "empty-choice", "no-content",
         "empty-content", "empty-object", "list", "string", "json-null", "empty-body",
         "html", "broken-json", "binary"],
)
def test_empty_or_unparseable_success_bodies_raise_response_error(kit, response_factory):
    ft = kit.fake_transport(response_factory(kit))
    with pytest.raises(ProviderResponseError):
        ask(kit, ft)
    assert len(ft.requests) == 1, "a malformed 200 must not be retried"


# ----- auth errors (401, 403) ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(
    "body",
    [b"", b"Unauthorized", b"<html>nope</html>", b'{"detail": "bad key"}', b'{"error": "bad key"}',
     b'{"error": {"message": "key revoked"}}', b"\x00\x01\x02"],
    ids=["empty", "text", "html", "detail", "error-string", "error-object", "binary"],
)
def test_revoked_key_gives_an_auth_error_whatever_the_body_looks_like(kit, status, body):
    ft = kit.fake_transport(kit.response(status, body=body))
    with pytest.raises(ProviderAuthError) as info:
        ask(kit, ft)
    msg = str(info.value)
    assert "reject" in msg.lower()
    assert DASHBOARD in msg
    assert KEY not in err_text(info.value)
    assert len(ft.requests) == 1, "auth failures are never retried"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 429, 500, 503])
def test_the_key_never_appears_in_any_error_even_if_the_server_echoes_it(kit, status):
    echo = kit.response(status, json_body={"error": {
        "message": f"Invalid credentials for Bearer {KEY}; key {KEY} was rejected",
        "top_up_url": "https://example.test/top-up",
    }})
    ft = kit.fake_transport(echo, echo, echo, echo)
    with pytest.raises(ProviderError) as info:
        ask(kit, ft, PUBLIK_MAX_RETRIES="1")
    exc = info.value
    assert KEY not in err_text(exc)
    assert KEY not in getattr(exc, "top_up_url", "")


def test_the_key_never_reaches_logs_stdout_or_stderr(kit, caplog, capsys):
    import logging

    caplog.set_level(logging.DEBUG)
    for factory in (
        lambda: kit.chat_ok(GOOD_HOOK, headers={"x-publik-balance": "100"}),
        lambda: kit.response(401, body=f"bad {KEY}".encode()),
        lambda: kit.response(402, json_body={"error": {"message": "no credit"}}),
        lambda: kit.response(503, body=b"down"),
    ):
        ft = kit.fake_transport(factory(), factory(), factory(), factory())
        try:
            ask(kit, ft)
        except ProviderError:
            pass
    out = capsys.readouterr()
    assert KEY not in caplog.text
    assert KEY not in out.out and KEY not in out.err


def test_repr_and_str_of_the_provider_do_not_contain_the_key(kit):
    provider, _ = build(kit, kit.fake_transport())
    assert KEY not in repr(provider) and KEY not in str(provider)


def test_the_hook_returned_to_callers_never_contains_the_key(kit):
    hook, _ = ask(kit, kit.fake_transport(kit.chat_ok(GOOD_HOOK, headers={"x-publik-balance": "5"})))
    assert KEY not in repr(hook)


# ----- 402 credit errors ---------------------------------------------------------------------------------------------------------

def credit(kit, status_body, **env):
    ft = kit.fake_transport(status_body)
    with pytest.raises(ProviderCreditError) as info:
        ask(kit, ft, **env)
    assert len(ft.requests) == 1, "402 must not be retried"
    return info.value


def test_402_uses_the_server_message_and_top_up_url(kit):
    exc = credit(kit, kit.error_response(402, "Balance is empty", top_up_url="https://publikhq.com/top-up"))
    assert "Balance is empty" in str(exc)
    assert exc.top_up_url == "https://publikhq.com/top-up"


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"top_up_url": "https://t.example/top", "claim_url": "https://c.example/claim", "add_credit_url": "https://a.example/add"}, "https://t.example/top"),
        ({"top_up_url": "", "claim_url": "https://c.example/claim", "add_credit_url": "https://a.example/add"}, "https://c.example/claim"),
        ({"top_up_url": None, "claim_url": "https://c.example/claim", "add_credit_url": "https://a.example/add"}, "https://c.example/claim"),
        ({"claim_url": "https://c.example/claim", "add_credit_url": "https://a.example/add"}, "https://c.example/claim"),
        ({"add_credit_url": "https://a.example/add"}, "https://a.example/add"),
        ({"top_up_url": "", "claim_url": "", "add_credit_url": "https://a.example/add"}, "https://a.example/add"),
        ({}, ""),
        ({"top_up_url": "", "claim_url": "", "add_credit_url": ""}, ""),
    ],
    ids=["all-three", "empty-top-up", "null-top-up", "claim-first", "add-credit-only", "last-resort", "none", "all-empty"],
)
def test_402_top_up_url_precedence(kit, fields, expected):
    exc = credit(kit, kit.error_response(402, "Out of credit", **fields))
    assert exc.top_up_url == expected


def test_402_model_requires_claim_is_handled_like_any_other_402(kit):
    exc = credit(kit, kit.error_response(
        402, "Claim your account to use this model", type="model_requires_claim",
        claim_url="https://publikhq.com/claim/abc",
    ))
    assert exc.top_up_url == "https://publikhq.com/claim/abc"
    assert "Claim your account" in str(exc)


def test_402_without_a_message_still_has_a_default_sentence(kit):
    exc = credit(kit, kit.response(402, json_body={"error": {"top_up_url": "https://t.example/top"}}))
    assert str(exc).strip()
    assert exc.top_up_url == "https://t.example/top"


@pytest.mark.parametrize(
    "body",
    [b"Payment Required", b"<html><h1>402</h1></html>", b"", b"{broken", b'{"error": "out of credit"}',
     b'["not", "an", "object"]', b'{"error": null}', b'{"error": {"message": null}}', b"\xff\xfe"],
    ids=["text", "html", "empty", "broken-json", "error-string", "list", "null-error", "null-message", "binary"],
)
def test_402_with_an_unexpected_body_still_raises_a_credit_error(kit, body):
    exc = credit(kit, kit.response(402, body=body))
    assert str(exc).strip()
    assert exc.top_up_url == ""


# ----- 429, 5xx and network failures ------------------------------------------------------------------------------------------------

def test_429_then_200_succeeds_after_one_backoff(kit):
    ft = kit.fake_transport(kit.response(429, body=b"slow down"), kit.chat_ok(GOOD_HOOK))
    hook, sleeper = ask(kit, ft)
    assert hook.text == GOOD_HOOK
    assert len(ft.requests) == 2 and len(sleeper.calls) == 1


def test_429_forever_ends_in_a_rate_limit_error_after_the_retry_budget(kit):
    ft = kit.fake_transport(*[kit.response(429, body=b"") for _ in range(4)])
    with pytest.raises(ProviderRateLimitError):
        ask(kit, ft)
    assert len(ft.requests) == 4


@pytest.mark.parametrize("body", [b"", b"Too Many Requests", b"<html>429</html>", b'{"unexpected": true}', b'{"error": "slow"}', b"\x00"])
def test_429_never_depends_on_the_body_shape(kit, body):
    ft = kit.fake_transport(*[kit.response(429, body=body) for _ in range(4)])
    with pytest.raises(ProviderRateLimitError):
        ask(kit, ft)


def test_retry_after_on_429_is_honoured_and_capped_at_thirty_seconds(kit):
    ft = kit.fake_transport(
        kit.response(429, headers={"retry-after": "5"}),
        kit.response(429, headers={"retry-after": "100"}),
        kit.chat_ok(GOOD_HOOK),
    )
    _, sleeper = ask(kit, ft)
    assert sleeper.calls == [pytest.approx(5.0), pytest.approx(30.0)]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_exhausted_is_a_transport_error(kit, status):
    ft = kit.fake_transport(*[kit.response(status, body=b"upstream trouble") for _ in range(4)])
    with pytest.raises(ProviderTransportError):
        ask(kit, ft)
    assert len(ft.requests) == 4


def test_5xx_then_200_succeeds(kit):
    ft = kit.fake_transport(kit.response(503), kit.response(502), kit.chat_ok(GOOD_HOOK))
    hook, sleeper = ask(kit, ft)
    assert hook.text == GOOD_HOOK and len(sleeper.calls) == 2


def test_connection_failures_exhausted_are_a_transport_error(kit):
    ft = kit.fake_transport(*[kit.failure("connection refused") for _ in range(4)])
    with pytest.raises(ProviderTransportError):
        ask(kit, ft)
    assert len(ft.requests) == 4


def test_connection_failure_then_success_recovers(kit):
    ft = kit.fake_transport(kit.failure("timed out"), kit.chat_ok(GOOD_HOOK))
    hook, _ = ask(kit, ft)
    assert hook.text == GOOD_HOOK


@pytest.mark.parametrize("status", [400, 404, 405, 409, 413, 422])
def test_other_4xx_raise_a_provider_error_with_status_and_message_and_are_not_retried(kit, status):
    ft = kit.fake_transport(kit.error_response(status, "model is not available to this account"), kit.chat_ok(GOOD_HOOK))
    with pytest.raises(ProviderError) as info:
        ask(kit, ft)
    exc = info.value
    assert not isinstance(exc, (ProviderAuthError, ProviderCreditError, ProviderRateLimitError,
                                ProviderTransportError, InsufficientFacts))
    assert str(status) in str(exc)
    assert "model is not available to this account" in str(exc)
    assert len(ft.requests) == 1


def test_other_4xx_with_a_non_json_body_still_raises_cleanly(kit):
    ft = kit.fake_transport(kit.response(400, body=b"<html>bad request</html>"))
    with pytest.raises(ProviderError) as info:
        ask(kit, ft)
    assert "400" in str(info.value)


# ----- the same provider against a real local HTTP server ---------------------------------------------------------------------------------

def gw_provider(kit, gateway, sleeper=None, **env):
    settings = kit.publik_settings(PUBLIK_API_BASE_URL=gateway.base_url, **env)
    return PublikProvider(settings, sleep=sleeper or kit.sleeper(), rng=kit.rng(0.0))


@pytest.mark.local_http
def test_gateway_happy_path_sends_the_documented_request(kit, gateway):
    gateway.reply_chat(GOOD_HOOK, headers={
        "x-publik-model": "publik-balanced-live", "x-publik-balance": "777", "x-publik-charge-micros": "11",
    })
    request = kit.request()
    hook = gw_provider(kit, gateway).personalize(request)
    assert hook.text == GOOD_HOOK and hook.provider == "publik" and hook.model == "publik-balanced-live"
    assert int(hook.meta["balance_micros"]) == 777 and int(hook.meta["charge_micros"]) == 11
    (req,) = gateway.requests
    assert req.method == "POST" and req.path == "/api/v1/chat/completions"
    assert req.header("authorization") == f"Bearer {KEY}"
    assert req.header("content-type") == "application/json"
    assert req.header("accept") == "application/json"
    assert req.header("user-agent") == f"vcoutreach/{vcoutreach.__version__}"
    body = req.json()
    assert body["model"] == "publik-balanced" and body["max_tokens"] == 300 and body["temperature"] == 0.4
    assert body["messages"] == build_messages(request)


@pytest.mark.local_http
def test_gateway_402_with_a_claim_url(kit, gateway):
    gateway.reply(402, json_body={"error": {
        "type": "model_requires_claim", "message": "Claim this key to continue",
        "claim_url": "https://publikhq.com/claim/xyz",
    }})
    with pytest.raises(ProviderCreditError) as info:
        gw_provider(kit, gateway).personalize(kit.request())
    assert info.value.top_up_url == "https://publikhq.com/claim/xyz"
    assert len(gateway.requests) == 1


@pytest.mark.local_http
def test_gateway_429_then_200_with_retry_after_zero(kit, gateway):
    gateway.reply(429, headers={"Retry-After": "0"}, body=b"")
    gateway.reply_chat(GOOD_HOOK)
    hook = gw_provider(kit, gateway).personalize(kit.request())
    assert hook.text == GOOD_HOOK
    assert len(gateway.requests) == 2


@pytest.mark.local_http
def test_gateway_503_then_200(kit, gateway):
    gateway.reply(503, body=b"maintenance")
    gateway.reply_chat(GOOD_HOOK)
    sleeper = kit.sleeper()
    hook = gw_provider(kit, gateway, sleeper).personalize(kit.request())
    assert hook.text == GOOD_HOOK and len(sleeper.calls) == 1


@pytest.mark.local_http
def test_gateway_5xx_forever_is_a_transport_error(kit, gateway):
    for _ in range(2):
        gateway.reply(500, body=b"boom")
    with pytest.raises(ProviderTransportError):
        gw_provider(kit, gateway, PUBLIK_MAX_RETRIES="1").personalize(kit.request())
    assert len(gateway.requests) == 2


@pytest.mark.local_http
def test_gateway_revoked_key_is_an_auth_error_and_the_echoed_key_stays_out_of_the_message(kit, gateway):
    gateway.reply(401, json_body={"error": {"message": f"Invalid API key {KEY}"}})
    with pytest.raises(ProviderAuthError) as info:
        gw_provider(kit, gateway).personalize(kit.request())
    assert KEY not in err_text(info.value)
    assert DASHBOARD in str(info.value)
    assert len(gateway.requests) == 1


@pytest.mark.local_http
def test_gateway_garbled_balance_header_is_ignored(kit, gateway):
    gateway.reply_chat(GOOD_HOOK, headers={"x-publik-balance": "12abc", "x-publik-charge-micros": "9"})
    hook = gw_provider(kit, gateway).personalize(kit.request())
    assert "balance_micros" not in hook.meta
    assert int(hook.meta["charge_micros"]) == 9


@pytest.mark.local_http
def test_gateway_stopped_means_connection_refused_means_transport_error(kit, gateway):
    provider = gw_provider(kit, gateway, PUBLIK_MAX_RETRIES="1")
    gateway.stop()
    with pytest.raises(ProviderTransportError):
        provider.personalize(kit.request())


@pytest.mark.local_http
def test_gateway_slow_response_beyond_the_timeout_is_a_transport_error(kit, gateway):
    gateway.reply(200, json_body={"choices": [{"message": {"content": GOOD_HOOK}}]}, delay=3.0)
    provider = gw_provider(kit, gateway, PUBLIK_TIMEOUT_SECONDS="0.3", PUBLIK_MAX_RETRIES="0")
    started = time.monotonic()
    with pytest.raises(ProviderTransportError):
        provider.personalize(kit.request())
    assert time.monotonic() - started < 2.5
    assert len(gateway.requests) == 1
