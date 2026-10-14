"""SPEC 18.6: NO_HOOK detection is exact, for all three model providers and in the pipeline.

A reply counts as NO_HOOK only if, after stripping whitespace, surrounding quotes or
backticks and one trailing period, it equals NO_HOOK. Anything else is an ordinary hook.
"""
from __future__ import annotations

import pytest

from vcoutreach.pipeline import draft_all
from vcoutreach.providers import AnthropicProvider, OpenAICompatProvider, PublikProvider
from vcoutreach.types import Hook, InsufficientFacts

PROVIDER_NAMES = ["publik", "openai", "anthropic"]

NO_HOOK_REPLIES = [
    "NO_HOOK",
    "  NO_HOOK",
    "NO_HOOK  ",
    "\n\tNO_HOOK\n",
    '"NO_HOOK"',
    "'NO_HOOK'",
    "`NO_HOOK`",
    "NO_HOOK.",
    '  "NO_HOOK"  \n',
    "  `NO_HOOK`\n",
]

ORDINARY_REPLIES = [
    "no_hook",
    "No_Hook",
    "no_hook.",
    "NO_HOOKS",
    "NO HOOK",
    "NO-HOOK",
    "NO_HOOK..",
    "NO_HOOK!",
    "NO_HOOK because your facts are thin",
    "I would answer NO_HOOK if I had nothing, but your essay on small teams stayed with me.",
    "Your essay on small teams stayed with me, so this is not a NO_HOOK situation.",
    "Reply NO_HOOK.",
    "xNO_HOOK",
]


def build(kit, name, ft):
    sleeper, rng = kit.sleeper(), kit.rng(0.0)
    if name == "publik":
        return PublikProvider(kit.publik_settings(), transport=ft, sleep=sleeper, rng=rng)
    if name == "openai":
        return OpenAICompatProvider(kit.settings(OPENAI_API_KEY="ok_fake_OPENAI00000000000000BBBB", OPENAI_MODEL="gpt-test"),
                                    transport=ft, sleep=sleeper, rng=rng)
    return AnthropicProvider(kit.settings(ANTHROPIC_API_KEY="ak_fake_ANTHR00000000000000CCCC", ANTHROPIC_MODEL="claude-test"),
                             transport=ft, sleep=sleeper, rng=rng)


def reply(kit, name, text):
    return kit.anthropic_ok(text) if name == "anthropic" else kit.chat_ok(text)


@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("text", NO_HOOK_REPLIES, ids=[repr(t) for t in NO_HOOK_REPLIES])
def test_exact_no_hook_variants_mean_insufficient_facts(kit, name, text):
    provider = build(kit, name, kit.fake_transport(reply(kit, name, text)))
    with pytest.raises(InsufficientFacts):
        provider.personalize(kit.request())


@pytest.mark.parametrize("name", PROVIDER_NAMES)
@pytest.mark.parametrize("text", ORDINARY_REPLIES, ids=[repr(t) for t in ORDINARY_REPLIES])
def test_lookalikes_and_mentions_inside_a_sentence_are_ordinary_hooks(kit, name, text):
    provider = build(kit, name, kit.fake_transport(reply(kit, name, text)))
    hook = provider.personalize(kit.request())
    assert isinstance(hook, Hook)
    assert hook.text == text.strip()


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_lowercase_no_hook_is_not_a_match(kit, name):
    provider = build(kit, name, kit.fake_transport(reply(kit, name, "no_hook")))
    assert provider.personalize(kit.request()).text == "no_hook"


def two_people(kit):
    return [
        kit.contact(id="ada", name="Ada Lovelace", email="ada@northwind.example.com",
                    facts=("your essay on how small teams ship weekly",)),
        kit.contact(id="grace", name="Grace Hopper", email="grace@compiler.example.org", org="Compiler Co",
                    facts=("your talk on making software readable",)),
    ]


GOOD_HOOK = "Your talk on making software readable made me rethink how I name things, and I respect that."


@pytest.mark.parametrize("text", NO_HOOK_REPLIES, ids=[repr(t) for t in NO_HOOK_REPLIES])
def test_in_the_pipeline_an_exact_no_hook_blocks_only_that_row(kit, text):
    ft = kit.fake_transport(kit.chat_ok(text), kit.chat_ok(GOOD_HOOK))
    out = kit.workdir / "drafts"
    drafts = draft_all(two_people(kit), kit.sender(), build(kit, "publik", ft), out)
    assert [d.status for d in drafts] == ["blocked", "ok"]
    assert "insufficient_facts" in [i.code for i in drafts[0].issues]
    assert (out / "blocked" / "ada.md").exists() and (out / "grace.md").exists()
    assert len(ft.requests) == 2, "the run must continue after a NO_HOOK row"


def test_in_the_pipeline_a_hook_that_only_mentions_no_hook_is_not_treated_as_no_hook(kit):
    text = "NO_HOOK is what I would answer with no facts, but your essay on small teams stayed with me."
    ft = kit.fake_transport(kit.chat_ok(text))
    drafts = draft_all(two_people(kit)[:1], kit.sender(), build(kit, "publik", ft), kit.workdir / "drafts")
    (d,) = drafts
    assert "insufficient_facts" not in [i.code for i in d.issues]
    assert d.hook is not None and d.hook.text == text
