"""SPEC 20.9 (Lane B): the retry wording in provider errors is `after 1 retry` / `after <n> retries`."""
from __future__ import annotations

import re

import pytest

from vcoutreach.providers import AnthropicProvider, OpenAICompatProvider, PublikProvider
from vcoutreach.types import ProviderError

PHRASE = re.compile(r"after (\d+) (retry|retries)\b")
KEYS = {
    "publik": "fk_test_0123456789abcdefZQ4W",
    "openai": "ok_fake_OPENAI00000000000000BBBB",
    "anthropic": "ak_fake_ANTHR00000000000000CCCC",
}
PROVIDER_NAMES = list(KEYS)


def build(kit, name, ft, retries=None):
    sleeper, rng = kit.sleeper(), kit.rng(0.0)
    if name == "publik":
        env = {"PUBLIK_API_KEY": KEYS[name]}
        if retries is not None:
            env["PUBLIK_MAX_RETRIES"] = str(retries)
        return PublikProvider(kit.settings(**env), transport=ft, sleep=sleeper, rng=rng)
    if name == "openai":
        return OpenAICompatProvider(kit.settings(OPENAI_API_KEY=KEYS[name], OPENAI_MODEL="gpt-test"),
                                    transport=ft, sleep=sleeper, rng=rng)
    return AnthropicProvider(kit.settings(ANTHROPIC_API_KEY=KEYS[name], ANTHROPIC_MODEL="claude-test"),
                             transport=ft, sleep=sleeper, rng=rng)


SCENARIOS = {
    "rate-limit": lambda kit: kit.response(429, body=b"slow down"),
    "server-error": lambda kit: kit.response(503, body=b"down"),
    "bad-gateway": lambda kit: kit.response(502, body=b"bad gateway"),
    "connection-failure": lambda kit: kit.failure("simulated connection failure"),
}


def fail_forever(kit, name, scenario, retries=None):
    ft = kit.fake_transport(*[SCENARIOS[scenario](kit) for _ in range(12)])
    provider = build(kit, name, ft, retries=retries)
    with pytest.raises(ProviderError) as info:
        provider.personalize(kit.request())
    return str(info.value), ft


def check_wording(message, requests_made):
    match = PHRASE.search(message)
    assert match, f"the error should say how many retries were used: {message!r}"
    n = int(match.group(1))
    assert n == requests_made - 1, f"{n} retries claimed but {requests_made} requests were made"
    assert match.group(2) == ("retry" if n == 1 else "retries"), match.group(0)
    assert "after 1 retries" not in message and not re.search(r"after [2-9]\d* retry\b", message)


@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("retries", [1, 2, 3])
def test_publik_retry_wording_matches_the_configured_budget(kit, scenario, retries):
    message, ft = fail_forever(kit, "publik", scenario, retries=retries)
    check_wording(message, len(ft.requests))
    assert f"after {retries} " + ("retry" if retries == 1 else "retries") in message


def test_publik_with_one_retry_says_retry_in_the_singular(kit):
    message, ft = fail_forever(kit, "publik", "server-error", retries=1)
    assert "after 1 retry" in message and "after 1 retries" not in message
    assert len(ft.requests) == 2


def test_publik_with_the_default_budget_says_retries_in_the_plural(kit):
    message, ft = fail_forever(kit, "publik", "server-error")
    assert len(ft.requests) == 4
    assert "after 3 retries" in message


def test_publik_with_no_retries_never_claims_a_singular_retry(kit):
    message, ft = fail_forever(kit, "publik", "server-error", retries=0)
    assert len(ft.requests) == 1
    assert "after 0 retry" not in message and "after 1 retr" not in message
    match = PHRASE.search(message)
    if match:
        assert match.group(1) == "0" and match.group(2) == "retries"


@pytest.mark.parametrize("name", ["openai", "anthropic"])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_the_bring_your_own_providers_use_the_same_wording(kit, name, scenario):
    message, ft = fail_forever(kit, name, scenario)
    check_wording(message, len(ft.requests))


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_the_retry_wording_never_leaks_the_key(kit, name):
    message, _ = fail_forever(kit, name, "server-error")
    assert KEYS[name] not in message
