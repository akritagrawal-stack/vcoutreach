"""OpenAI-compatible, Anthropic and template providers (SPEC section 10 and 17)."""
from __future__ import annotations

import json
import os
import urllib.request

import pytest

from vcoutreach.prompts import build_messages
from vcoutreach.providers import (
    AnthropicProvider,
    OpenAICompatProvider,
    TemplateProvider,
    resolve_provider,
)
from vcoutreach.types import (
    Hook,
    InsufficientFacts,
    ProviderAuthError,
    ProviderCreditError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
)

OPENAI_KEY = "ok_fake_OPENAI00000000000000BBBB"
ANTHROPIC_KEY = "ak_fake_ANTHR00000000000000CCCC"
GOOD_HOOK = "I read your essay on how small teams ship weekly, and I respect the focus."


def err_text(exc):
    return f"{exc!s} | {exc!r} | {exc.args!r}"


# =====================================================================================================
# OpenAI-compatible
# =====================================================================================================

def openai_provider(kit, ft, sleeper=None, **env):
    base = {"OPENAI_API_KEY": OPENAI_KEY, "OPENAI_MODEL": "gpt-test"}
    base.update(env)
    sleeper = sleeper if sleeper is not None else kit.sleeper()
    return OpenAICompatProvider(kit.settings(**base), transport=ft, sleep=sleeper, rng=kit.rng(0.0)), sleeper


def openai_ask(kit, ft, request=None, **env):
    provider, sleeper = openai_provider(kit, ft, **env)
    return provider.personalize(request or kit.request()), sleeper


def test_openai_posts_chat_completions_to_the_default_base_url(kit):
    request = kit.request()
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    hook, _ = openai_ask(kit, ft, request)
    (req,) = ft.requests
    assert req.method == "POST"
    assert req.url == "https://api.openai.com/v1/chat/completions"
    assert req.header("authorization") == f"Bearer {OPENAI_KEY}"
    assert req.header("content-type") == "application/json"
    body = req.json()
    assert body["model"] == "gpt-test"
    assert body["messages"] == build_messages(request)
    assert hook.text == GOOD_HOOK and hook.provider == "openai" and hook.model == "gpt-test"


@pytest.mark.parametrize("base", ["http://localhost:11434/v1", "http://localhost:11434/v1/", "http://localhost:11434/v1//"])
def test_openai_custom_base_url_has_no_doubled_slash(kit, base):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    openai_ask(kit, ft, OPENAI_BASE_URL=base)
    assert ft.requests[0].url == "http://localhost:11434/v1/chat/completions"


def test_openai_provider_name(kit):
    provider, _ = openai_provider(kit, kit.fake_transport())
    assert provider.name == "openai"


def test_openai_sends_the_timeout_from_the_same_settings_family(kit):
    ft = kit.fake_transport(kit.chat_ok(GOOD_HOOK))
    openai_ask(kit, ft)
    assert isinstance(ft.requests[0].timeout, (int, float)) and ft.requests[0].timeout > 0


@pytest.mark.parametrize("content", ["NO_HOOK", " NO_HOOK\n"])
def test_openai_no_hook_is_insufficient_facts(kit, content):
    with pytest.raises(InsufficientFacts):
        openai_ask(kit, kit.fake_transport(kit.chat_ok(content)))


@pytest.mark.parametrize("response_factory", [
    lambda kit: kit.chat_ok(""),
    lambda kit: kit.response(200, json_body={"choices": []}),
    lambda kit: kit.response(200, body=b"<html>captive portal</html>"),
    lambda kit: kit.response(200, json_body={}),
], ids=["empty", "no-choices", "html", "empty-object"])
def test_openai_empty_or_unparseable_bodies_are_response_errors(kit, response_factory):
    with pytest.raises(ProviderResponseError):
        openai_ask(kit, kit.fake_transport(response_factory(kit)))


@pytest.mark.parametrize("status", [401, 403])
def test_openai_auth_failures(kit, status):
    ft = kit.fake_transport(kit.error_response(status, "Incorrect API key provided"))
    with pytest.raises(ProviderAuthError):
        openai_ask(kit, ft)
    assert len(ft.requests) == 1


def test_openai_402_is_a_credit_error_with_the_top_up_url(kit):
    ft = kit.fake_transport(kit.error_response(402, "Out of credit", top_up_url="https://gw.example.test/billing"))
    with pytest.raises(ProviderCreditError) as info:
        openai_ask(kit, ft)
    assert info.value.top_up_url == "https://gw.example.test/billing"
    assert len(ft.requests) == 1


def test_openai_429_then_200_recovers_and_429_forever_is_a_rate_limit_error(kit):
    ft = kit.fake_transport(kit.response(429, headers={"retry-after": "2"}), kit.chat_ok(GOOD_HOOK))
    hook, sleeper = openai_ask(kit, ft)
    assert hook.text == GOOD_HOOK and sleeper.calls == [pytest.approx(2.0)]
    ft = kit.fake_transport(*[kit.response(429) for _ in range(4)])
    with pytest.raises(ProviderRateLimitError):
        openai_ask(kit, ft)
    assert len(ft.requests) == 4


def test_openai_5xx_and_connection_failures_end_as_transport_errors(kit):
    for script in ([kit.response(503)] * 4, [kit.failure("refused")] * 4):
        ft = kit.fake_transport(*script)
        with pytest.raises(ProviderTransportError):
            openai_ask(kit, ft)
        assert len(ft.requests) == 4


def test_openai_other_4xx_mention_status_and_message(kit):
    ft = kit.fake_transport(kit.error_response(400, "unsupported parameter: temperature"))
    with pytest.raises(ProviderError) as info:
        openai_ask(kit, ft)
    assert "400" in str(info.value) and "unsupported parameter" in str(info.value)
    assert len(ft.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 402, 403, 429, 500])
def test_openai_key_never_appears_in_errors_even_when_echoed(kit, status):
    echo = kit.error_response(status, f"bad credentials {OPENAI_KEY}")
    ft = kit.fake_transport(echo, echo, echo, echo)
    with pytest.raises(ProviderError) as info:
        openai_ask(kit, ft, OPENAI_MODEL="gpt-test")
    assert OPENAI_KEY not in err_text(info.value)


def test_openai_provider_repr_hides_the_key(kit):
    provider, _ = openai_provider(kit, kit.fake_transport())
    assert OPENAI_KEY not in repr(provider) and OPENAI_KEY not in str(provider)


@pytest.mark.local_http
def test_openai_provider_resolved_from_settings_works_against_a_real_local_server(kit, gateway):
    gateway.reply_chat(GOOD_HOOK)
    settings = kit.settings(OPENAI_API_KEY=OPENAI_KEY, OPENAI_MODEL="gpt-test", OPENAI_BASE_URL=gateway.base_url)
    hook = resolve_provider(settings, "openai").personalize(kit.request())
    assert hook.text == GOOD_HOOK
    (req,) = gateway.requests
    assert req.path == "/api/v1/chat/completions"
    assert req.header("authorization") == f"Bearer {OPENAI_KEY}"
    assert req.json()["model"] == "gpt-test"


# =====================================================================================================
# Anthropic
# =====================================================================================================

def anthropic_provider(kit, ft, sleeper=None, **env):
    base = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "ANTHROPIC_MODEL": "claude-test"}
    base.update(env)
    sleeper = sleeper if sleeper is not None else kit.sleeper()
    return AnthropicProvider(kit.settings(**base), transport=ft, sleep=sleeper, rng=kit.rng(0.0)), sleeper


def anthropic_ask(kit, ft, request=None, **env):
    provider, sleeper = anthropic_provider(kit, ft, **env)
    return provider.personalize(request or kit.request()), sleeper


def block_text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content)


def test_anthropic_request_shape(kit):
    request = kit.request()
    ft = kit.fake_transport(kit.anthropic_ok(GOOD_HOOK))
    hook, _ = anthropic_ask(kit, ft, request)
    (req,) = ft.requests
    assert req.method == "POST"
    assert req.url == "https://api.anthropic.com/v1/messages"
    assert req.header("x-api-key") == ANTHROPIC_KEY
    assert req.header("anthropic-version") == "2023-06-01"
    assert req.header("content-type") == "application/json"
    assert req.header("authorization") is None, "Anthropic uses x-api-key, not a Bearer token"
    body = req.json()
    assert {"model", "max_tokens", "system", "messages"} <= set(body)
    assert body["model"] == "claude-test"
    assert isinstance(body["max_tokens"], int) and body["max_tokens"] > 0
    shared = build_messages(request)
    assert body["system"] == shared[0]["content"] or block_text(body["system"]) == shared[0]["content"]
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert block_text(body["messages"][0]["content"]) == shared[1]["content"]
    assert hook.text == GOOD_HOOK and hook.provider == "anthropic" and hook.model == "claude-test"


def test_anthropic_provider_name(kit):
    provider, _ = anthropic_provider(kit, kit.fake_transport())
    assert provider.name == "anthropic"


def test_anthropic_uses_the_first_content_block(kit):
    ft = kit.fake_transport(kit.response(200, json_body={
        "content": [{"type": "text", "text": f"  {GOOD_HOOK}  "}, {"type": "text", "text": "second block"}],
    }))
    hook, _ = anthropic_ask(kit, ft)
    assert hook.text == GOOD_HOOK


@pytest.mark.parametrize("text", ["NO_HOOK", "\n NO_HOOK  "])
def test_anthropic_no_hook_is_insufficient_facts(kit, text):
    with pytest.raises(InsufficientFacts):
        anthropic_ask(kit, kit.fake_transport(kit.anthropic_ok(text)))


@pytest.mark.parametrize("response_factory", [
    lambda kit: kit.anthropic_ok(""),
    lambda kit: kit.response(200, json_body={"content": []}),
    lambda kit: kit.response(200, json_body={"content": [{"type": "tool_use", "id": "x"}]}),
    lambda kit: kit.response(200, json_body={}),
    lambda kit: kit.response(200, body=b"<html>proxy error</html>"),
    lambda kit: kit.response(200, body=b""),
], ids=["empty-text", "no-blocks", "no-text-block", "empty-object", "html", "empty-body"])
def test_anthropic_empty_or_unparseable_bodies_are_response_errors(kit, response_factory):
    with pytest.raises(ProviderResponseError):
        anthropic_ask(kit, kit.fake_transport(response_factory(kit)))


@pytest.mark.parametrize("status", [401, 403])
def test_anthropic_auth_failures(kit, status):
    ft = kit.fake_transport(kit.response(status, json_body={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    with pytest.raises(ProviderAuthError):
        anthropic_ask(kit, ft)
    assert len(ft.requests) == 1


def test_anthropic_429_forever_is_a_rate_limit_error(kit):
    ft = kit.fake_transport(*[kit.response(429, body=b"") for _ in range(4)])
    with pytest.raises(ProviderRateLimitError):
        anthropic_ask(kit, ft)
    assert len(ft.requests) == 4


def test_anthropic_503_then_200_recovers(kit):
    ft = kit.fake_transport(kit.response(503), kit.anthropic_ok(GOOD_HOOK))
    hook, sleeper = anthropic_ask(kit, ft)
    assert hook.text == GOOD_HOOK and len(sleeper.calls) == 1


def test_anthropic_5xx_and_connection_failures_end_as_transport_errors(kit):
    for script in ([kit.response(500)] * 4, [kit.failure("timed out")] * 4):
        with pytest.raises(ProviderTransportError):
            anthropic_ask(kit, kit.fake_transport(*script))


def test_anthropic_other_4xx_is_a_provider_error_naming_the_status(kit):
    ft = kit.fake_transport(kit.response(400, json_body={"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens too large"}}))
    with pytest.raises(ProviderError) as info:
        anthropic_ask(kit, ft)
    assert "400" in str(info.value)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_anthropic_key_never_appears_in_errors_even_when_echoed(kit, status):
    echo = kit.response(status, json_body={"type": "error", "error": {"message": f"invalid x-api-key {ANTHROPIC_KEY}"}})
    ft = kit.fake_transport(echo, echo, echo, echo)
    with pytest.raises(ProviderError) as info:
        anthropic_ask(kit, ft)
    assert ANTHROPIC_KEY not in err_text(info.value)


def test_anthropic_provider_repr_hides_the_key(kit):
    provider, _ = anthropic_provider(kit, kit.fake_transport())
    assert ANTHROPIC_KEY not in repr(provider) and ANTHROPIC_KEY not in str(provider)


# =====================================================================================================
# Template provider
# =====================================================================================================

def hook_for(kit, *facts, **contact_over):
    return TemplateProvider().personalize(kit.request(contact=kit.contact(facts=facts, **contact_over)))


def test_template_hook_is_built_from_the_first_fact_only(kit):
    hook = hook_for(kit, "your essay on how small teams ship weekly", "a second fact that must not appear")
    assert isinstance(hook, Hook)
    assert hook.text == "I came across your essay on how small teams ship weekly, and I found it interesting."
    assert hook.provider == "template"
    assert hook.model == "none"
    assert "second fact" not in hook.text


def test_template_removes_one_trailing_period_from_the_fact(kit):
    hook = hook_for(kit, "your essay on small teams.")
    assert hook.text == "I came across your essay on small teams, and I found it interesting."


def test_template_keeps_other_punctuation_and_interior_case(kit):
    hook = hook_for(kit, "your NASA talk: why \"small\" teams win?")
    assert hook.text == "I came across your NASA talk: why \"small\" teams win?, and I found it interesting."


def test_template_with_no_facts_raises_insufficient_facts(kit):
    with pytest.raises(InsufficientFacts):
        hook_for(kit)


def test_template_is_deterministic_and_ignores_everything_but_the_first_fact(kit):
    a = TemplateProvider().personalize(kit.request(
        contact=kit.contact(facts=("your essay on small teams",)), sender=kit.sender(name="Sam Rivera")))
    b = TemplateProvider().personalize(kit.request(
        contact=kit.contact(name="Someone Else", org="Other Co", role="CEO", facts=("your essay on small teams", "x")),
        sender=kit.sender(name="Totally Different", org="Elsewhere"), instructions="be warm"))
    assert a == b
    p = TemplateProvider()
    request = kit.request()
    assert p.personalize(request) == p.personalize(request) == p.personalize(request)


def test_template_is_unaffected_by_provider_keys_in_the_environment(kit, monkeypatch):
    before = hook_for(kit, "your essay on small teams")
    monkeypatch.setenv("PUBLIK_API_KEY", "should-not-matter")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-matter")
    assert hook_for(kit, "your essay on small teams") == before


def test_template_makes_zero_network_calls_even_if_errors_would_be_swallowed(kit, network_guard, monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(("urlopen", a)) or (_ for _ in ()).throw(AssertionError("urlopen called")))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda *a, **k: calls.append(("open", a)) or (_ for _ in ()).throw(AssertionError("opener called")))
    provider = resolve_provider(kit.settings(), "template")
    for _ in range(3):
        provider.personalize(kit.request())
    TemplateProvider().personalize(kit.request())
    assert calls == []
    assert network_guard.attempts == []


class _ExplodingEnviron:
    def _boom(self, *a, **k):
        raise AssertionError("TemplateProvider must not read the environment")

    def __getattr__(self, name):
        raise AssertionError(f"TemplateProvider must not read the environment (os.environ.{name})")

    __getitem__ = __contains__ = __iter__ = __len__ = __setitem__ = __delitem__ = _boom


def test_template_reads_no_environment_variables(kit, monkeypatch):
    request = kit.request()
    TemplateProvider().personalize(request)  # warm any lazy imports before the trap is set
    with monkeypatch.context() as m:
        m.setattr(os, "environ", _ExplodingEnviron())
        provider = TemplateProvider()
        hook = provider.personalize(request)
    assert hook.provider == "template"
    assert json.dumps(hook.text)


def test_template_hook_passes_lint_and_grounding_for_ordinary_facts(kit):
    from vcoutreach.grounding import check_grounding
    from vcoutreach.lint import lint_draft

    contact = kit.contact(facts=("your essay on how small teams ship weekly",))
    sender = kit.sender()
    hook = TemplateProvider().personalize(kit.request(contact=contact, sender=sender))
    assert check_grounding(hook.text, contact, sender) == []
    body = kit.clean_body(hook=hook.text)
    assert [i for i in lint_draft("a note for Ada", body, sender, contact, hook.text) if i.severity == "error"] == []


# =====================================================================================================
# Template provider: noun-phrase facts only (SPEC 21.5)
# =====================================================================================================

NOUN_PHRASE_MESSAGE = 'the offline template needs facts written as noun phrases (for example "your 2024 essay on X"), not sentences'


def test_a_leading_capital_your_is_lower_cased_in_the_hook(kit):
    hook = hook_for(kit, "Your essay on queue design.")
    assert hook.text == "I came across your essay on queue design, and I found it interesting."


def test_only_the_leading_your_is_lower_cased(kit):
    hook = hook_for(kit, "Your talk with Ada at NASA")
    assert hook.text == "I came across your talk with Ada at NASA, and I found it interesting."


def test_a_fact_that_is_already_lower_case_your_is_unchanged(kit):
    assert hook_for(kit, "your essay on queue design").text == "I came across your essay on queue design, and I found it interesting."


def test_a_noun_phrase_that_does_not_start_with_your_keeps_its_case(kit):
    hook = hook_for(kit, "Queue Design in Practice, a talk at Strange Loop")
    assert hook.text == "I came across Queue Design in Practice, a talk at Strange Loop, and I found it interesting."


@pytest.mark.parametrize(
    "fact",
    ["You wrote a great essay on queues", "you wrote a great essay on queues", "YOU WROTE ABOUT QUEUES",
     "I read your essay on queues", "We met at Strange Loop", "They built a queue service", "He wrote about queues",
     "She runs a queue conference", "It was a great talk on queues"],
    ids=["you", "you-lower", "you-upper", "i", "we", "they", "he", "she", "it"],
)
def test_a_sentence_style_fact_raises_insufficient_facts_with_the_exact_message(kit, fact):
    with pytest.raises(InsufficientFacts) as info:
        hook_for(kit, fact)
    assert NOUN_PHRASE_MESSAGE in str(info.value)


@pytest.mark.parametrize(
    "fact",
    ["Italy trip notes on queues", "Interview with Ada on queues", "Weekly notes on queue design", "Hesitation patterns in teams",
     "Sheltered queues: a field guide", "Youth sports and queue theory", "Wetware essays", "Theory of queues", "Itinerary for 2024"],
    ids=["it-prefix", "i-prefix", "we-prefix", "he-prefix", "she-prefix", "you-prefix", "we-prefix-2", "they-prefix", "it-prefix-2"],
)
def test_a_noun_phrase_that_merely_starts_with_the_same_letters_is_fine(kit, fact):
    hook = hook_for(kit, fact)
    assert hook.text == f"I came across {fact}, and I found it interesting."


def test_the_sentence_check_looks_only_at_the_first_fact(kit):
    with pytest.raises(InsufficientFacts):
        hook_for(kit, "You wrote a great essay on queues", "your essay on queue design")
    hook = hook_for(kit, "your essay on queue design", "You wrote a great essay on queues")
    assert hook.text == "I came across your essay on queue design, and I found it interesting."


def test_the_noun_phrase_message_is_not_an_unhandled_error_type(kit):
    with pytest.raises(ProviderError):
        hook_for(kit, "You wrote a great essay on queues")
