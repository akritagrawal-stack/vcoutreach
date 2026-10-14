"""Gaps found by the second mutation run: sender.load_sender and subject_template (SPEC sections 5 and 7)."""
from __future__ import annotations

import pytest

from vcoutreach.sender import load_sender
from vcoutreach.templates import render_subject
from vcoutreach.types import SenderProfileError

# SPEC 7: subject_template is "an optional string with {first_name} and {org}", nothing else.
BAD_TEMPLATES = ["{nme}", "a note for {nme}", "{org.x}", "{first_name!r}", "{0}", "{}"]


@pytest.mark.parametrize("template", BAD_TEMPLATES, ids=["typo", "typo-in-text", "attribute", "conversion", "positional", "empty-braces"])
def test_a_subject_template_with_anything_but_first_name_and_org_is_refused_when_loading(kit, template):
    path = kit.write_sender(subject_template=template)
    with pytest.raises(SenderProfileError):
        load_sender(path)


@pytest.mark.parametrize("template", ["a note for {first_name}", "{first_name} at {org}", "an idea for {org}", "no placeholders at all",
                                      "{first_name}, {first_name}"],
                         ids=["first-name", "both", "org", "none", "repeated"])
def test_a_subject_template_with_only_first_name_and_org_loads_and_renders(kit, template):
    sender = load_sender(kit.write_sender(subject_template=template))
    subject = render_subject(kit.contact(name="Ada Lovelace", org="Northwind Labs"), sender)
    assert "{" not in subject and "}" not in subject
    assert subject
