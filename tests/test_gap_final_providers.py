"""SPEC 22.18: the public helpers configured_providers and missing_variable apply the model rule themselves."""
from __future__ import annotations

import pytest

from vcoutreach.providers import configured_providers, missing_variable

OPENAI_KEY = "ok_fake_OPENAI00000000000000BBBB"
ANTHROPIC_KEY = "ak_fake_ANTHR00000000000000CCCC"
PUBLIK_KEY = "pk_fake_PUBLIK00000000000000AAAA"


def names(settings):
    result = configured_providers(settings)
    if isinstance(result, dict):
        return {key for key, value in result.items() if value}
    return set(result)


# ----- a key without a model is not configured, and the helper says which variable is missing -----------------------------------

@pytest.mark.parametrize("provider,env,variable", [
    ("openai", {"OPENAI_API_KEY": OPENAI_KEY}, "OPENAI_MODEL"),
    ("anthropic", {"ANTHROPIC_API_KEY": ANTHROPIC_KEY}, "ANTHROPIC_MODEL"),
], ids=["openai", "anthropic"])
def test_a_key_without_a_model_is_not_configured_and_the_model_variable_is_named(kit, provider, env, variable):
    settings = kit.settings(**env)
    assert provider not in names(settings)
    assert missing_variable(settings, provider) == variable


@pytest.mark.parametrize("provider,env,variable", [
    ("openai", {"OPENAI_API_KEY": OPENAI_KEY, "OPENAI_MODEL": "my-model"}, "OPENAI_MODEL"),
    ("anthropic", {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "ANTHROPIC_MODEL": "my-model"}, "ANTHROPIC_MODEL"),
], ids=["openai", "anthropic"])
def test_a_key_with_a_model_is_configured_and_nothing_is_missing(kit, provider, env, variable):
    settings = kit.settings(**env)
    assert provider in names(settings)
    assert not missing_variable(settings, provider)


@pytest.mark.parametrize("provider,env,variable", [
    ("openai", {"OPENAI_API_KEY": OPENAI_KEY, "OPENAI_MODEL": ""}, "OPENAI_MODEL"),
    ("anthropic", {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "ANTHROPIC_MODEL": ""}, "ANTHROPIC_MODEL"),
], ids=["openai-blank-model", "anthropic-blank-model"])
def test_a_blank_model_counts_as_empty(kit, provider, env, variable):
    settings = kit.settings(**env)
    assert provider not in names(settings)
    assert missing_variable(settings, provider) == variable


@pytest.mark.parametrize("provider,env,key_name", [
    ("openai", {"OPENAI_MODEL": "my-model"}, "OPENAI_API_KEY"),
    ("anthropic", {"ANTHROPIC_MODEL": "my-model"}, "ANTHROPIC_API_KEY"),
], ids=["openai", "anthropic"])
def test_a_model_without_a_key_is_not_configured_and_the_key_is_named(kit, provider, env, key_name):
    settings = kit.settings(**env)
    assert provider not in names(settings)
    assert key_name in str(missing_variable(settings, provider))


# ----- the other providers and the combinations ---------------------------------------------------------------------------------------------

def test_the_template_provider_is_always_configured(kit):
    settings = kit.settings()
    assert "template" in names(settings)
    assert not missing_variable(settings, "template")


def test_publik_needs_only_its_key(kit):
    assert "publik" not in names(kit.settings())
    assert missing_variable(kit.settings(), "publik") == "PUBLIK_API_KEY"
    with_key = kit.settings(PUBLIK_API_KEY=PUBLIK_KEY)
    assert "publik" in names(with_key) and not missing_variable(with_key, "publik")


def test_each_provider_is_judged_on_its_own_variables(kit):
    settings = kit.settings(PUBLIK_API_KEY=PUBLIK_KEY, OPENAI_API_KEY=OPENAI_KEY, ANTHROPIC_API_KEY=ANTHROPIC_KEY,
                            ANTHROPIC_MODEL="my-model")
    assert names(settings) == {"template", "publik", "anthropic"}
    assert missing_variable(settings, "openai") == "OPENAI_MODEL"


def test_adding_the_model_later_flips_the_answer(kit):
    before = kit.settings(OPENAI_API_KEY=OPENAI_KEY)
    after = kit.settings(OPENAI_API_KEY=OPENAI_KEY, OPENAI_MODEL="my-model")
    assert "openai" not in names(before) and "openai" in names(after)


def test_the_helpers_never_touch_the_network(kit, network_guard):
    settings = kit.settings(OPENAI_API_KEY=OPENAI_KEY, ANTHROPIC_API_KEY=ANTHROPIC_KEY, PUBLIK_API_KEY=PUBLIK_KEY)
    names(settings)
    for provider in ("template", "publik", "openai", "anthropic"):
        missing_variable(settings, provider)
    assert network_guard.attempts == []


def test_the_command_line_repeats_the_same_answer(kit):
    res = kit.cli("providers", env={"OPENAI_API_KEY": OPENAI_KEY})
    line = next(ln for ln in res.out.splitlines() if "openai" in ln.lower())
    assert "configured=no" in line and "OPENAI_MODEL" in line
