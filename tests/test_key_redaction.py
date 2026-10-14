"""SPEC 18.3: API keys never leak through provider errors, for publik, openai and anthropic.

Any text taken from a server response is passed through a redactor that replaces every
occurrence of the configured key (when it is at least 8 characters long) with ``***``
before it reaches an exception message or a log line. Request headers never appear in
errors. A hostile or buggy server that echoes the credential back must therefore not be
able to put the key into str(), repr(), args, tracebacks, the chained exceptions, the
credit top-up URL, log records, stdout or stderr.
"""
from __future__ import annotations

import logging
import traceback

import pytest

from vcoutreach.providers import AnthropicProvider, OpenAICompatProvider, PublikProvider
from vcoutreach.types import (
    ProviderAuthError,
    ProviderCreditError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
)

PROVIDER_NAMES = ["publik", "openai", "anthropic"]

KEYS = {
    "publik": "fk_test_0123456789abcdefZQ4W",
    "openai": "ok_fake_OPENAI00000000000000BBBB",
    "anthropic": "ak_fake_ANTHR00000000000000CCCC",
}

ERROR_STATUSES = [400, 401, 402, 403, 404, 422, 429, 500, 502, 503]
# Statuses whose error.message is documented to travel into the exception message.
MESSAGE_STATUSES = [400, 402, 404, 422]


def build(kit, name, ft, key=None):
    key = key or KEYS[name]
    sleeper, rng = kit.sleeper(), kit.rng(0.0)
    if name == "publik":
        settings = kit.settings(PUBLIK_API_KEY=key, PUBLIK_MAX_RETRIES="1")
        return PublikProvider(settings, transport=ft, sleep=sleeper, rng=rng)
    if name == "openai":
        settings = kit.settings(OPENAI_API_KEY=key, OPENAI_MODEL="gpt-test")
        return OpenAICompatProvider(settings, transport=ft, sleep=sleeper, rng=rng)
    settings = kit.settings(ANTHROPIC_API_KEY=key, ANTHROPIC_MODEL="claude-test")
    return AnthropicProvider(settings, transport=ft, sleep=sleeper, rng=rng)


def script_of(item, count=10):
    """The same scripted item repeated, so retries never run the fake transport dry."""
    return [item] * count


def everything_about(exc):
    """Every textual surface of an exception and of the exceptions chained to it."""
    parts = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        parts.append(repr(current))
        parts.append(repr(current.args))
        parts.append("".join(traceback.format_exception(type(current), current, current.__traceback__)))
        top_up = getattr(current, "top_up_url", "")
        if top_up:
            parts.append(str(top_up))
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


def credit_error_for(name):
    """The SPEC maps 402 to ProviderCreditError for publik and the OpenAI-compatible provider;
    for Anthropic it only promises a ProviderError (a subclass is fine)."""
    return ProviderError if name == "anthropic" else ProviderCreditError


def personalize_error(kit, name, responses, key=None, expected=ProviderError):
    ft = kit.fake_transport(*responses)
    provider = build(kit, name, ft, key=key)
    with pytest.raises(expected) as info:
        provider.personalize(kit.request())
    return info.value, ft


# =====================================================================================================
# Echoed in a JSON error message
# =====================================================================================================

@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("status", ERROR_STATUSES)
def test_a_key_echoed_in_the_error_message_never_appears_in_any_exception_text(kit, name, status):
    key = KEYS[name]
    echo = kit.error_response(status, f"Invalid credentials for Bearer {key}; key {key} was rejected")
    exc, _ = personalize_error(kit, name, script_of(echo))
    assert key not in everything_about(exc)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("status", MESSAGE_STATUSES)
def test_the_server_message_is_kept_but_the_key_inside_it_becomes_three_asterisks(kit, name, status):
    key = KEYS[name]
    echo = kit.error_response(status, f"quota exhausted for {key} on this plan")
    exc, _ = personalize_error(kit, name, script_of(echo))
    text = str(exc)
    assert key not in everything_about(exc)
    assert "***" in text, "the redactor replaces the key with ***"
    assert "quota exhausted" in text, "the rest of the server message is kept"


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_every_occurrence_is_redacted_not_just_the_first(kit, name):
    key = KEYS[name]
    message = f"first {key} second {key} third {key} done"
    exc, _ = personalize_error(kit, name, script_of(kit.error_response(400, message)))
    assert key not in everything_about(exc)
    assert str(exc).count("***") >= 3


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_a_key_glued_to_other_text_is_still_redacted(kit, name):
    key = KEYS[name]
    message = f"token=({key}) prefix{key}suffix {key}.{key}"
    exc, _ = personalize_error(kit, name, script_of(kit.error_response(422, message)))
    assert key not in everything_about(exc)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_a_key_that_is_exactly_eight_characters_is_redacted(kit, name):
    key = "k3y8ZQ4W"
    assert len(key) == 8
    message = f"rejected {key} for this account"
    exc, _ = personalize_error(kit, name, script_of(kit.error_response(400, message)), key=key)
    assert key not in everything_about(exc)
    assert "***" in str(exc)


# =====================================================================================================
# Echoed in other shapes of body
# =====================================================================================================

PLAIN_BODIES = [
    ("text", lambda key: f"Unauthorized: bad key {key}".encode()),
    ("html", lambda key: f"<html><body><h1>403</h1><p>Key {key} not allowed</p></body></html>".encode()),
    ("bare-json-string", lambda key: ('"' + key + '"').encode()),
    ("json-detail", lambda key: ('{"detail": "bad key ' + key + '"}').encode()),
    ("json-error-string", lambda key: ('{"error": "' + key + ' is invalid"}').encode()),
    ("json-message-only", lambda key: ('{"message": "' + key + '"}').encode()),
    ("json-nested-message", lambda key: ('{"error": {"message": {"detail": "' + key + '"}}}').encode()),
    ("json-message-list", lambda key: ('{"error": {"message": ["' + key + '", "other"]}}').encode()),
    ("json-array", lambda key: ('["' + key + '"]').encode()),
    ("truncated-json", lambda key: ('{"error": {"message": "bad ' + key).encode()),
    ("latin1-bytes", lambda key: b"\xff\xfe bad " + key.encode() + b" \xff"),
]


@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
@pytest.mark.parametrize("shape,make", PLAIN_BODIES, ids=[s for s, _ in PLAIN_BODIES])
def test_a_key_echoed_in_any_body_shape_never_reaches_the_exception(kit, name, status, shape, make):
    key = KEYS[name]
    response = kit.response(status, body=make(key))
    exc, _ = personalize_error(kit, name, script_of(response))
    assert key not in everything_about(exc)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_a_key_echoed_by_a_successful_but_unusable_reply_does_not_leak(kit, name):
    key = KEYS[name]
    for body in (
        f"not json at all {key}".encode(),
        ('{"choices": [], "echo": "' + key + '"}').encode(),
        ('{"content": [], "echo": "' + key + '"}').encode(),
        ('{"choices": [{"message": {"content": ""}}], "debug": "' + key + '"}').encode(),
    ):
        exc, _ = personalize_error(kit, name, script_of(kit.response(200, body=body)),
                                   expected=ProviderResponseError)
        assert key not in everything_about(exc)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_a_key_in_response_headers_never_reaches_the_exception(kit, name):
    key = KEYS[name]
    headers = {"x-debug": f"auth={key}", "www-authenticate": f'Bearer realm="{key}"', "retry-after": "0"}
    for status in (401, 429, 500):
        response = kit.response(status, headers=headers, body=b"no")
        exc, _ = personalize_error(kit, name, script_of(response))
        assert key not in everything_about(exc)


# =====================================================================================================
# Credit top-up URLs
# =====================================================================================================

@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("field", ["top_up_url", "claim_url", "add_credit_url"])
def test_a_key_echoed_inside_the_credit_url_is_redacted(kit, name, field):
    key = KEYS[name]
    response = kit.error_response(402, f"Out of credit for {key}", **{field: f"https://pay.example.test/top-up?k={key}"})
    exc, _ = personalize_error(kit, name, script_of(response), expected=credit_error_for(name))
    assert key not in everything_about(exc)
    assert key not in getattr(exc, "top_up_url", "")


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_a_402_whose_type_and_code_fields_echo_the_key_does_not_leak(kit, name):
    key = KEYS[name]
    response = kit.error_response(402, "Out of credit", type=f"model_requires_claim_{key}", code=key,
                                  top_up_url="https://pay.example.test/top-up")
    exc, _ = personalize_error(kit, name, script_of(response), expected=credit_error_for(name))
    assert key not in everything_about(exc)


# =====================================================================================================
# Exception types and chained exceptions
# =====================================================================================================

@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("status,expected", [
    (401, ProviderAuthError),
    (403, ProviderAuthError),
    (429, ProviderRateLimitError),
    (503, ProviderTransportError),
], ids=["401", "403", "429", "503"])
def test_the_error_class_is_unchanged_by_redaction(kit, name, status, expected):
    key = KEYS[name]
    exc, _ = personalize_error(kit, name, script_of(kit.error_response(status, f"echo {key}")), expected=expected)
    assert key not in everything_about(exc)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_a_transport_failure_that_mentions_the_key_does_not_leak(kit, name):
    key = KEYS[name]
    failure = kit.failure(f"proxy refused the request, credential {key} not accepted")
    exc, _ = personalize_error(kit, name, script_of(failure), expected=ProviderTransportError)
    assert key not in everything_about(exc)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_the_key_is_not_in_the_chained_exception_either(kit, name):
    key = KEYS[name]
    exc, _ = personalize_error(kit, name, script_of(kit.error_response(500, f"boom {key}")))
    chain = []
    current = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    for item in chain:
        assert key not in str(item) and key not in repr(item) and key not in repr(item.args)


# =====================================================================================================
# Logs, stdout, stderr
# =====================================================================================================

@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_no_log_record_stdout_or_stderr_ever_contains_the_key(kit, name, caplog, capsys):
    key = KEYS[name]
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    echoes = [
        kit.error_response(401, f"bad key {key}"),
        kit.error_response(402, f"no credit for {key}", top_up_url=f"https://pay.example.test/?k={key}"),
        kit.error_response(429, f"slow down {key}"),
        kit.response(500, body=f"upstream said {key}".encode()),
        kit.response(200, body=f"garbage {key}".encode()),
    ]
    for echo in echoes:
        provider = build(kit, name, kit.fake_transport(*script_of(echo)))
        try:
            provider.personalize(kit.request())
        except ProviderError:
            pass
    out = capsys.readouterr()
    assert key not in caplog.text
    for record in caplog.records:
        assert key not in record.getMessage()
        assert key not in repr(record.args)
    assert key not in out.out and key not in out.err


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_the_key_stays_out_of_logs_on_a_successful_call_too(kit, name, caplog, capsys):
    key = KEYS[name]
    caplog.set_level(logging.DEBUG)
    text = "I read your essay on how small teams ship weekly, and I respect the focus."
    response = kit.anthropic_ok(text) if name == "anthropic" else kit.chat_ok(text)
    hook = build(kit, name, kit.fake_transport(response)).personalize(kit.request())
    out = capsys.readouterr()
    assert hook.text == text
    assert key not in caplog.text
    assert key not in out.out and key not in out.err
    assert key not in repr(hook)


# =====================================================================================================
# Request headers never appear in errors
# =====================================================================================================

@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_request_headers_never_appear_in_errors(kit, name, status):
    key = KEYS[name]
    exc, ft = personalize_error(kit, name, script_of(kit.response(status, body=b"nope")))
    text = everything_about(exc)
    assert key not in text
    # the key did go out on the wire in the proper header, so the redaction is not trivially true
    sent = ft.requests[0]
    carried = {h: v for h, v in sent.headers.items() if key in v}
    assert carried, "the configured key must be sent in a request header"
