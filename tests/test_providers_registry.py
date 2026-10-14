"""providers.resolve_provider (SPEC sections 10 and 17)."""
from __future__ import annotations

import pytest

from vcoutreach.providers import (
    AnthropicProvider,
    OpenAICompatProvider,
    PublikProvider,
    TemplateProvider,
    resolve_provider,
)
from vcoutreach.types import ProviderConfigError

PUBLIK = {"PUBLIK_API_KEY": "pk_fake_PUBLIK00000000000000AAAA"}
OPENAI = {"OPENAI_API_KEY": "ok_fake_OPENAI00000000000000BBBB", "OPENAI_MODEL": "gpt-test"}
ANTHROPIC = {"ANTHROPIC_API_KEY": "ak_fake_ANTHR00000000000000CCCC", "ANTHROPIC_MODEL": "claude-test"}


def settings(kit, *groups, **extra):
    env = {}
    for g in groups:
        env.update(g)
    env.update(extra)
    return kit.settings(**env)


# ----- explicit names ----------------------------------------------------------------------------------

def test_template_needs_no_configuration(kit):
    p = resolve_provider(settings(kit), "template")
    assert isinstance(p, TemplateProvider)
    assert p.name == "template"


def test_template_ignores_keys_that_happen_to_be_set(kit):
    p = resolve_provider(settings(kit, PUBLIK, OPENAI, ANTHROPIC), "template")
    assert isinstance(p, TemplateProvider)


def test_publik_with_a_key_resolves_to_the_publik_provider(kit):
    p = resolve_provider(settings(kit, PUBLIK), "publik")
    assert isinstance(p, PublikProvider)
    assert p.name == "publik"


def test_publik_without_a_key_explains_exactly_how_to_fix_it(kit):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit), "publik")
    msg = str(info.value)
    assert "PUBLIK_API_KEY" in msg
    assert "https://publikhq.com/dashboard/api" in msg
    assert "--provider template" in msg


def test_publik_error_does_not_leak_other_providers_keys(kit):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit, OPENAI, ANTHROPIC), "publik")
    for secret in (OPENAI["OPENAI_API_KEY"], ANTHROPIC["ANTHROPIC_API_KEY"]):
        assert secret not in str(info.value) and secret not in repr(info.value)


def test_blank_publik_key_counts_as_missing(kit):
    with pytest.raises(ProviderConfigError):
        resolve_provider(settings(kit, PUBLIK_API_KEY=""), "publik")


def test_openai_with_a_key_resolves_to_the_openai_compatible_provider(kit):
    p = resolve_provider(settings(kit, OPENAI), "openai")
    assert isinstance(p, OpenAICompatProvider)
    assert p.name == "openai"


def test_openai_without_a_key_names_the_variable(kit):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit), "openai")
    assert "OPENAI_API_KEY" in str(info.value)


def test_anthropic_with_a_key_resolves_to_the_anthropic_provider(kit):
    p = resolve_provider(settings(kit, ANTHROPIC), "anthropic")
    assert isinstance(p, AnthropicProvider)
    assert p.name == "anthropic"


def test_anthropic_without_a_key_names_the_variable(kit):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit), "anthropic")
    assert "ANTHROPIC_API_KEY" in str(info.value)


def test_unknown_provider_name_lists_the_valid_names(kit):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit, PUBLIK), "gemini")
    msg = str(info.value)
    assert "gemini" in msg
    for valid in ("template", "publik", "openai", "anthropic"):
        assert valid in msg


# ----- auto -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "groups,expected_cls,expected_name",
    [
        ((PUBLIK, OPENAI, ANTHROPIC), PublikProvider, "publik"),
        ((PUBLIK,), PublikProvider, "publik"),
        ((OPENAI, ANTHROPIC), OpenAICompatProvider, "openai"),
        ((OPENAI,), OpenAICompatProvider, "openai"),
        ((ANTHROPIC,), AnthropicProvider, "anthropic"),
        ((), TemplateProvider, "template"),
    ],
    ids=["all-three", "publik-only", "openai-and-anthropic", "openai-only", "anthropic-only", "none"],
)
def test_auto_picks_publik_then_openai_then_anthropic_then_template(kit, groups, expected_cls, expected_name):
    p = resolve_provider(settings(kit, *groups), "auto")
    assert isinstance(p, expected_cls)
    assert p.name == expected_name


def test_auto_is_the_default_name(kit):
    s = settings(kit, ANTHROPIC)
    assert type(resolve_provider(s)) is type(resolve_provider(s, "auto"))
    assert isinstance(resolve_provider(settings(kit)), TemplateProvider)


def test_blank_keys_do_not_count_for_auto(kit):
    s = settings(kit, PUBLIK_API_KEY="", OPENAI_API_KEY="", ANTHROPIC_API_KEY="")
    assert isinstance(resolve_provider(s, "auto"), TemplateProvider)


def test_a_blank_publik_key_falls_through_to_the_next_provider(kit):
    s = settings(kit, OPENAI, PUBLIK_API_KEY="")
    assert isinstance(resolve_provider(s, "auto"), OpenAICompatProvider)


# ----- behaviour --------------------------------------------------------------------------------------------------

def test_resolving_never_touches_the_network(kit, network_guard):
    for name in ("auto", "template", "publik", "openai", "anthropic"):
        try:
            resolve_provider(settings(kit, PUBLIK, OPENAI, ANTHROPIC), name)
        except ProviderConfigError:
            pass
    assert network_guard.attempts == []


def test_an_injected_transport_is_used_for_requests(kit):
    ft = kit.fake_transport(kit.chat_ok("I read your essay on small teams, and I found it interesting."))
    p = resolve_provider(settings(kit, PUBLIK), "publik", transport=ft)
    hook = p.personalize(kit.request())
    assert hook.provider == "publik"
    assert len(ft.requests) == 1


def test_every_resolved_provider_exposes_name_and_personalize(kit):
    s = settings(kit, PUBLIK, OPENAI, ANTHROPIC)
    for name in ("template", "publik", "openai", "anthropic"):
        p = resolve_provider(s, name)
        assert isinstance(p.name, str) and callable(p.personalize)


def test_template_provider_is_constructible_without_arguments():
    assert TemplateProvider().name == "template"


# ----- SPEC 21.6: a key without a model is a configuration error, never a guess ---------------------------------------------

OPENAI_KEY_ONLY = {"OPENAI_API_KEY": OPENAI["OPENAI_API_KEY"]}
ANTHROPIC_KEY_ONLY = {"ANTHROPIC_API_KEY": ANTHROPIC["ANTHROPIC_API_KEY"]}


def missing_model_message(variable):
    return f"{variable} is not set. Set it to the model name you want to use (see your provider's model list)."


@pytest.mark.parametrize("name,group,variable", [("openai", OPENAI_KEY_ONLY, "OPENAI_MODEL"),
                                                 ("anthropic", ANTHROPIC_KEY_ONLY, "ANTHROPIC_MODEL")])
def test_a_key_without_a_model_raises_the_exact_config_error(kit, name, group, variable):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit, group), name)
    assert missing_model_message(variable) in str(info.value)


@pytest.mark.parametrize("name,group,variable", [("openai", OPENAI_KEY_ONLY, "OPENAI_MODEL"),
                                                 ("anthropic", ANTHROPIC_KEY_ONLY, "ANTHROPIC_MODEL")])
def test_a_blank_model_counts_as_not_set(kit, name, group, variable):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit, group, **{variable: ""}), name)
    assert variable in str(info.value)


@pytest.mark.parametrize("group,variable", [(OPENAI_KEY_ONLY, "OPENAI_MODEL"), (ANTHROPIC_KEY_ONLY, "ANTHROPIC_MODEL")])
def test_auto_surfaces_the_missing_model_error_and_does_not_fall_back_to_the_template(kit, group, variable):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit, group), "auto")
    assert variable in str(info.value)
    with pytest.raises(ProviderConfigError):
        resolve_provider(settings(kit, group))


def test_auto_with_an_openai_key_without_a_model_never_quietly_becomes_the_template(kit):
    try:
        provider = resolve_provider(settings(kit, OPENAI_KEY_ONLY, ANTHROPIC), "auto")
    except ProviderConfigError as exc:
        assert "OPENAI_MODEL" in str(exc)
    else:
        assert not isinstance(provider, TemplateProvider)


def test_publik_still_works_with_only_its_key_next_to_unfinished_byo_keys(kit):
    p = resolve_provider(settings(kit, PUBLIK, OPENAI_KEY_ONLY, ANTHROPIC_KEY_ONLY), "auto")
    assert isinstance(p, PublikProvider)
    assert isinstance(resolve_provider(settings(kit, PUBLIK), "publik"), PublikProvider)


def test_the_template_is_not_affected_by_a_key_without_a_model(kit):
    assert isinstance(resolve_provider(settings(kit, OPENAI_KEY_ONLY, ANTHROPIC_KEY_ONLY), "template"), TemplateProvider)


def test_a_model_without_a_key_still_asks_for_the_key(kit):
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit, OPENAI_MODEL="my-model"), "openai")
    assert "OPENAI_API_KEY" in str(info.value)
    with pytest.raises(ProviderConfigError) as info:
        resolve_provider(settings(kit, ANTHROPIC_MODEL="my-model"), "anthropic")
    assert "ANTHROPIC_API_KEY" in str(info.value)


def test_the_missing_model_error_never_leaks_the_key(kit):
    for name, group in (("openai", OPENAI_KEY_ONLY), ("anthropic", ANTHROPIC_KEY_ONLY)):
        with pytest.raises(ProviderConfigError) as info:
            resolve_provider(settings(kit, group), name)
        secret = next(iter(group.values()))
        assert secret not in str(info.value) and secret not in repr(info.value)


def test_a_model_without_a_key_does_not_make_auto_pick_that_provider(kit):
    assert isinstance(resolve_provider(settings(kit, OPENAI_MODEL="m1", ANTHROPIC_MODEL="m2"), "auto"), TemplateProvider)
