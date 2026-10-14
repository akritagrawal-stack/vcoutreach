"""SPEC 23.3 (contractions count as sentences for the offline template) and 23.5 (the unreachable-endpoint wording)."""
from __future__ import annotations

import re

import pytest

from vcoutreach.pipeline import draft_all
from vcoutreach.providers import AnthropicProvider, OpenAICompatProvider, PublikProvider, TemplateProvider
from vcoutreach.types import Hook, InsufficientFacts, ProviderTransportError

NOUN_PHRASE_MESSAGE = 'the offline template needs facts written as noun phrases (for example "your 2024 essay on X"), not sentences'
OPENAI_KEY = "ok_fake_OPENAI00000000000000BBBB"
ANTHROPIC_KEY = "ak_fake_ANTHR00000000000000CCCC"
PUBLIK_KEY = "pk_fake_PUBLIK00000000000000AAAA"

CONTRACTIONS = ["you've", "you're", "you'll", "you'd", "i'm", "i've", "i'll", "i'd", "we're", "we've", "we'll",
                "they're", "they've", "he's", "she's", "it's"]
SPELLINGS = {"capitalized": str.capitalize, "lower": str.lower, "upper": str.upper}


def hook_for(kit, *facts, **contact_over):
    return TemplateProvider().personalize(kit.request(contact=kit.contact(facts=facts, **contact_over)))


# ----- 23.3 -------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fact",
    ["You've been writing about queues", "I'm following your blog", "It's a great essay", "They're building X"],
)
def test_the_examples_from_the_spec_are_blocked_with_the_noun_phrase_message(kit, fact):
    with pytest.raises(InsufficientFacts) as info:
        hook_for(kit, fact)
    assert NOUN_PHRASE_MESSAGE in str(info.value)


@pytest.mark.parametrize("how", list(SPELLINGS))
@pytest.mark.parametrize("opening", CONTRACTIONS)
def test_every_listed_contraction_opening_is_a_sentence_in_any_letter_case(kit, opening, how):
    fact = f"{SPELLINGS[how](opening)} been writing about queues for years"
    with pytest.raises(InsufficientFacts) as info:
        hook_for(kit, fact)
    assert NOUN_PHRASE_MESSAGE in str(info.value)


@pytest.mark.parametrize("fact", ["You've been writing about queues", "They're building X"])
def test_a_contraction_fact_ends_as_a_blocked_draft_with_insufficient_facts_and_the_message(kit, fact):
    contact = kit.contact(id="bob", name="Bob Byte", email="bob@example.com", facts=(fact,))
    (draft,) = draft_all([contact], kit.sender(), TemplateProvider(), kit.workdir / "drafts")
    assert draft.status == "blocked"
    found = [issue for issue in draft.issues if issue.code == "insufficient_facts"]
    assert found and all(NOUN_PHRASE_MESSAGE in issue.message for issue in found), draft.issues
    assert (kit.workdir / "drafts" / "blocked" / "bob.md").exists() and not (kit.workdir / "drafts" / "bob.md").exists()


def test_a_noun_phrase_fact_still_works(kit):
    hook = hook_for(kit, "Your essay on queues")
    assert isinstance(hook, Hook)
    assert hook.text == "I came across your essay on queues, and I found it interesting."


@pytest.mark.parametrize(
    "fact",
    ["Your essay on queues", "your essay on why it's hard to queue", "Your talk on what we're building next",
     "Its role in queue theory", "Ill-posed problems in scheduling", "Id and ego in startup culture",
     "Shell scripts for queue design", "Youve Got Mail, a retrospective"],
)
def test_facts_that_only_resemble_a_contraction_or_hold_one_later_are_accepted(kit, fact):
    hook = hook_for(kit, fact)
    expected = "your" + fact[4:] if fact.startswith("Your ") else fact
    assert hook.text == f"I came across {expected}, and I found it interesting."
    assert hook.provider == "template"


def test_only_the_first_fact_is_checked(kit):
    assert hook_for(kit, "your essay on queues", "You've been writing about queues").text == \
        "I came across your essay on queues, and I found it interesting."
    with pytest.raises(InsufficientFacts):
        hook_for(kit, "You've been writing about queues", "your essay on queues")


def test_the_earlier_sentence_openings_are_still_sentences(kit):
    for fact in ("You wrote a great essay on queues", "I read your essay on queues", "We met at Strange Loop",
                 "They built a queue service", "He wrote about queues", "She runs a queue conference", "It was a great talk"):
        with pytest.raises(InsufficientFacts) as info:
            hook_for(kit, fact)
        assert NOUN_PHRASE_MESSAGE in str(info.value)


# ----- 23.5 -------------------------------------------------------------------------------------------------------------------------

def unreachable(kit, provider):
    ft = kit.fake_transport(*[kit.failure("connection refused") for _ in range(4)])
    sleeper, rng = kit.sleeper(), kit.rng(0.0)
    if provider == "openai":
        built = OpenAICompatProvider(kit.settings(OPENAI_API_KEY=OPENAI_KEY, OPENAI_MODEL="gpt-test"), transport=ft, sleep=sleeper, rng=rng)
    elif provider == "anthropic":
        built = AnthropicProvider(kit.settings(ANTHROPIC_API_KEY=ANTHROPIC_KEY, ANTHROPIC_MODEL="claude-test"), transport=ft, sleep=sleeper, rng=rng)
    else:
        built = PublikProvider(kit.settings(PUBLIK_API_KEY=PUBLIK_KEY), transport=ft, sleep=sleeper, rng=rng)
    with pytest.raises(ProviderTransportError) as info:
        built.personalize(kit.request())
    return str(info.value)


def test_the_openai_unreachable_message_uses_a_lower_case_article(kit):
    message = unreachable(kit, "openai")
    assert "Could not reach the OpenAI-compatible endpoint" in message, message


def test_the_openai_unreachable_message_never_has_a_capitalised_article_mid_sentence(kit):
    message = unreachable(kit, "openai")
    assert "The OpenAI" not in message, message
    assert not re.search(r"[a-z,;:] The OpenAI", message)
    assert message.startswith("Could not reach"), message


def test_the_openai_unreachable_message_does_not_leak_the_key(kit):
    assert OPENAI_KEY not in unreachable(kit, "openai")


@pytest.mark.parametrize("provider", ["publik", "anthropic"])
def test_the_other_providers_do_not_use_the_openai_wording(kit, provider):
    message = unreachable(kit, provider)
    assert "OpenAI-compatible" not in message, message
