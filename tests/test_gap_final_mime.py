"""SPEC 22.19: build_message refuses an empty, blank or at-less sender email with OutreachError."""
from __future__ import annotations

import pytest

from vcoutreach.send.mime import build_message
from vcoutreach.types import OutreachError


@pytest.mark.parametrize("bad", ["", "   ", "\t", "nope", "no-at-sign.example.com", "Sam Rivera"],
                         ids=["empty", "blank", "tab", "nope", "bare-domain", "just-a-name"])
def test_a_sender_email_without_an_at_sign_is_an_outreach_error(kit, bad):
    with pytest.raises(OutreachError):
        build_message(kit.draft(), "Sam Rivera", bad)


@pytest.mark.parametrize("bad", ["", "   ", "nope"], ids=["empty", "blank", "nope"])
def test_it_is_refused_with_the_overrides_set_as_well(kit, bad):
    with pytest.raises(OutreachError):
        build_message(kit.draft(), "Sam Rivera", bad, to_override="me@mail.test", subject_override="[TEST] hi")


@pytest.mark.parametrize("bad", ["", "   ", "nope"], ids=["empty", "blank", "nope"])
def test_it_is_refused_for_a_follow_up_too(kit, bad):
    with pytest.raises(OutreachError):
        build_message(kit.follow_up_draft(), "Sam Rivera", bad)


def test_a_proper_sender_email_still_builds(kit):
    msg = build_message(kit.draft(), "Sam Rivera", "sam@lumenlist.example.com")
    assert "sam@lumenlist.example.com" in str(msg["From"])
