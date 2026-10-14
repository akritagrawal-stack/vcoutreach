"""grounding.check_grounding (SPEC section 9): a model cannot invent a company or a number."""
from __future__ import annotations

import copy

import pytest

from vcoutreach.grounding import check_grounding
from vcoutreach.types import LintIssue


@pytest.fixture
def contact(kit):
    return kit.contact(
        name="Ada Lovelace", org="Northwind Labs", role="Partner",
        facts=(
            "your essay on how small teams ship weekly",
            "your role leading Northwind Labs, a seed stage fund",
            "you backed 40 startups in 2021",
        ),
    )


@pytest.fixture
def sender(kit):
    return kit.sender()


def named(issues, token):
    return any(token in i.message for i in issues)


# ----- grounded hooks pass --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "hook",
    [
        "I saw that you lead Northwind Labs, and I found the focus on small teams interesting.",
        "I noticed you backed 40 startups in 2021, and I respect that.",
        "I noticed you backed 40 startups, and I respect that focus.",
        "Your essay on how small teams ship weekly stood out, and I agree with it.",
        "I read your essay, and I found it interesting.",
    ],
)
def test_hooks_built_only_from_the_facts_pass(contact, sender, hook):
    assert check_grounding(hook, contact, sender) == []


def test_number_at_the_end_of_a_sentence_still_matches_the_fact(contact, sender):
    assert check_grounding("I respect that you backed 40.", contact, sender) == []


def test_quoted_and_bracketed_names_are_matched_without_their_punctuation(contact, sender):
    hook = 'I read that you lead "Northwind Labs," (a seed fund), and I found it interesting.'
    assert check_grounding(hook, contact, sender) == []


# ----- invented company names ---------------------------------------------------------------------------

def test_invented_company_is_flagged_and_named(contact, sender):
    issues = check_grounding("I read that you left Initech to start your own fund.", contact, sender)
    assert issues
    assert all(isinstance(i, LintIssue) and i.code == "ungrounded_claim" for i in issues)
    assert all(i.severity == "error" for i in issues)
    assert named(issues, "Initech")


def test_each_invented_name_is_named(contact, sender):
    issues = check_grounding("I heard you moved from Initech to Hooli last spring.", contact, sender)
    assert named(issues, "Initech") and named(issues, "Hooli")


def test_a_real_name_next_to_an_invented_one_only_flags_the_invented_one(contact, sender):
    issues = check_grounding("I read that Northwind Labs bought Pied Piper last year.", contact, sender)
    assert named(issues, "Piper")
    assert not named(issues, "Northwind")


def test_invented_unicode_name_is_flagged(contact, sender):
    issues = check_grounding("I enjoyed your talk with \u00d8rsted and found it interesting.", contact, sender)
    assert named(issues, "\u00d8rsted")


# ----- numbers ----------------------------------------------------------------------------------------------------

def test_number_not_in_any_fact_is_flagged_and_named(contact, sender):
    issues = check_grounding("I noticed you grew to 300 customers, and I respect that.", contact, sender)
    assert named(issues, "300")


def test_year_not_in_any_fact_is_flagged(contact, sender):
    assert named(check_grounding("Your essay from 2019 stood out.", contact, sender), "2019")


def test_token_mixing_letters_and_digits_must_be_grounded(contact, sender):
    assert named(check_grounding("I admire how you scaled to $5M quickly.", contact, sender), "5M")


# ----- sentence starts, allowlist, sender, contact fields ----------------------------------------------------------------

def test_capitalised_words_at_sentence_start_are_not_proper_nouns(contact, sender):
    hook = "Congratulations on the essay. Your point about weekly shipping stood out."
    assert check_grounding(hook, contact, sender) == []


def test_lowercase_only_hook_has_nothing_to_ground(contact, sender):
    assert check_grounding("what a thoughtful way to run a small team", contact, sender) == []


def test_empty_hook_returns_no_grounding_issues(contact, sender):
    assert check_grounding("", contact, sender) == []
    assert check_grounding("   ", contact, sender) == []


@pytest.mark.parametrize(
    "hook",
    [
        "I read it on Monday and again on Friday, and I found it interesting.",
        "I read it in March and again in September, and I found it interesting.",
        "I read it last January, and I found it interesting.",
    ],
)
def test_days_and_months_are_always_allowed(contact, sender, hook):
    assert check_grounding(hook, contact, sender) == []


def test_first_person_pronoun_is_allowed_mid_sentence(contact, sender):
    assert check_grounding("It was clear, and I found it interesting.", contact, sender) == []


@pytest.mark.parametrize("hook", [
    "It was clear, and I'm glad I read it.",
    "It was clear, and I've shared it twice.",
    "It was clear, and I'd read more of it.",
])
def test_first_person_contractions_are_not_invented_names(contact, sender, hook):
    assert check_grounding(hook, contact, sender) == []


def test_sender_name_org_and_title_are_allowed(contact, sender):
    hook = "As a Founder myself, I am writing as Sam Rivera of Lumenlist, and I found your essay interesting."
    assert check_grounding(hook, contact, sender) == []


def test_contact_name_org_and_role_are_allowed(contact, sender):
    hook = "As a Partner at Northwind, you must see many pitches, Ada."
    assert check_grounding(hook, contact, sender) == []


def test_matching_is_case_insensitive_substring(contact, sender):
    assert check_grounding("I like what NORTHWIND is doing with small teams.", contact, sender) == []


def test_possessives_of_grounded_names_are_grounded(contact, sender):
    assert check_grounding("I like Northwind's focus on small teams.", contact, sender) == []


def test_name_grounded_in_the_contact_name_with_accents(kit, sender):
    contact = kit.contact(name="Zo\u00eb \u00c5kesson", facts=("your talk on small teams",))
    assert check_grounding("I enjoyed your talk, Zo\u00eb, and found it interesting.", contact, sender) == []


def test_company_only_present_in_the_email_domain_is_not_grounded(kit, sender):
    contact = kit.contact(email="ada@zephyrlabs.example.com", org="Northwind Labs",
                          facts=("your essay on small teams",))
    assert named(check_grounding("I follow what you did at Zephyrlabs closely.", contact, sender), "Zephyrlabs")


# ----- purity ---------------------------------------------------------------------------------------------------------------------

def test_grounding_does_not_mutate_its_inputs(contact, sender):
    contact_before, sender_before = copy.deepcopy(contact), copy.deepcopy(sender)
    hook = "I read that you left Initech to start your own fund."
    first = check_grounding(hook, contact, sender)
    second = check_grounding(hook, contact, sender)
    assert first == second and first is not second
    assert contact == contact_before and sender == sender_before


# ----- SPEC 18.8: proper nouns in any script, not only ASCII A to Z ---------------------------------------------------------------

O_SLASH_UP = chr(0xD8)       # capital O with stroke
E_ACUTE_UP = chr(0xC9)       # capital E with acute
A_RING_UP = chr(0xC5)        # capital A with ring
CYRILLIC_YA = chr(0x42F)     # capital Ya
GREEK_DELTA = chr(0x394)     # capital Delta
E_ACUTE = chr(0xE9)
O_SLASH = chr(0xF8)

NON_ASCII_NAMES = [
    O_SLASH_UP + "rsted",
    E_ACUTE_UP + "lan",
    A_RING_UP + "lesund",
    CYRILLIC_YA + "ndeks",
    GREEK_DELTA + "elta",
]


@pytest.mark.parametrize("name", NON_ASCII_NAMES, ids=["o-stroke", "e-acute", "a-ring", "cyrillic", "greek"])
def test_an_invented_non_ascii_capitalised_name_is_flagged_and_named(contact, sender, name):
    issues = check_grounding(f"I noticed you left {name} to build your own fund, and I respect that.", contact, sender)
    assert named(issues, name)
    assert all(i.code == "ungrounded_claim" and i.severity == "error" for i in issues)


@pytest.mark.parametrize("name", NON_ASCII_NAMES, ids=["o-stroke", "e-acute", "a-ring", "cyrillic", "greek"])
def test_the_same_name_is_fine_when_a_fact_contains_it(kit, sender, name):
    contact = kit.contact(facts=(f"you advised {name} in its first year",))
    assert check_grounding(f"I noticed you advised {name} early, and I respect that.", contact, sender) == []


@pytest.mark.parametrize("name", NON_ASCII_NAMES[:3], ids=["o-stroke", "e-acute", "a-ring"])
def test_non_ascii_grounding_ignores_case(kit, sender, name):
    contact = kit.contact(org=name.upper(), facts=("your essay on small teams",))
    assert check_grounding(f"I follow what you did at {name} closely.", contact, sender) == []


def test_a_non_ascii_capital_at_the_start_of_a_sentence_is_not_a_proper_noun(contact, sender):
    hook = f"I read your essay. {O_SLASH_UP}verall it stayed with me, and I respect that."
    assert check_grounding(hook, contact, sender) == []


def test_lowercase_non_ascii_words_are_not_proper_nouns(contact, sender):
    hook = f"I enjoyed your talk on caf{E_ACUTE} culture and the {O_SLASH}l industry, and I respect that."
    assert check_grounding(hook, contact, sender) == []


def test_a_non_ascii_name_mixed_with_a_grounded_one_only_flags_the_invented_one(contact, sender):
    hook = f"I read that Northwind Labs hired {O_SLASH_UP}rsted last year, and I respect that."
    issues = check_grounding(hook, contact, sender)
    assert named(issues, O_SLASH_UP + "rsted") and not named(issues, "Northwind")
