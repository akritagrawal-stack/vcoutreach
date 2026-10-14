"""SPEC section 22.13: grounding compares numbers with their thousands separators removed on both sides."""
from __future__ import annotations

import pytest

from vcoutreach.grounding import check_grounding


def issues(kit, hook, facts):
    return check_grounding(hook, kit.contact(facts=tuple(facts)), kit.sender())


def codes(found):
    return [i.code for i in found]


@pytest.mark.parametrize("hook_number,fact_number", [
    ("1,200,000", "1200000"),
    ("12,500", "12500"),
    ("2,500,000", "2500000"),
    ("1200000", "1,200,000"),
    ("12500", "12,500"),
    ("1,200,000", "1,200,000"),
    ("1200000", "1200000"),
])
def test_a_number_is_grounded_whichever_side_has_the_separators(kit, hook_number, fact_number):
    hook = f"Your essay reached {hook_number} readers, and I respect that."
    assert "ungrounded_claim" not in codes(issues(kit, hook, [f"your essay reached {fact_number} readers"]))


@pytest.mark.parametrize("hook_number,fact_number", [
    ("1,200,001", "1200000"),
    ("1,200,000", "1200001"),
    ("12,500", "1,250"),
    ("1200001", "1,200,000"),
])
def test_a_different_number_is_still_not_grounded(kit, hook_number, fact_number):
    hook = f"Your essay reached {hook_number} readers, and I respect that."
    found = issues(kit, hook, [f"your essay reached {fact_number} readers"])
    assert "ungrounded_claim" in codes(found)


def test_a_number_in_no_fact_at_all_is_not_grounded(kit):
    found = issues(kit, "Your essay reached 3,400,000 readers, and I respect that.", ["your essay on small teams"])
    assert "ungrounded_claim" in codes(found)


def test_removing_separators_does_not_ground_a_word_by_accident(kit):
    found = issues(kit, "Your essay reached Acme Corp, and I respect that.", ["your essay reached 1200000 readers"])
    assert "ungrounded_claim" in codes(found)
