"""Gaps found by the second mutation run: lint.unapproved_number with grouped digits (SPEC section 8)."""
from __future__ import annotations

import pytest

from vcoutreach.lint import lint_draft

SUBJECT = "a note for Ada"


def issues_for(kit, hook, facts=None):
    contact = kit.contact(facts=tuple(facts)) if facts is not None else kit.contact()
    body = kit.clean_body(hook=hook)
    return lint_draft(SUBJECT, body, kit.sender(), contact, hook)


def codes(issues):
    return [i.code for i in issues]


GROUPED = ["1,000,000", "12,500", "2,500,000", "10,000"]


@pytest.mark.parametrize("number", GROUPED, ids=GROUPED)
def test_a_comma_grouped_number_that_is_in_no_fact_is_flagged(kit, number):
    hook = f"Your essay reached {number} readers, and I respect that."
    found = [i for i in issues_for(kit, hook) if i.code == "unapproved_number"]
    assert found and all(i.severity == "error" for i in found)


@pytest.mark.parametrize("number", GROUPED, ids=GROUPED)
def test_a_comma_grouped_number_that_the_facts_state_is_not_flagged(kit, number):
    hook = f"Your essay reached {number} readers, and I respect that."
    facts = [f"your essay reached {number} readers"]
    assert "unapproved_number" not in codes(issues_for(kit, hook, facts))


def test_a_plain_number_that_is_in_no_fact_is_still_flagged(kit):
    assert "unapproved_number" in codes(issues_for(kit, "Your essay reached 40000 readers, and I respect that."))
