"""Gaps found by the second mutation run: send.mime.build_message (SPEC sections 12, 17 and 18.5)."""
from __future__ import annotations

import pytest

from vcoutreach.send.mime import build_message
from vcoutreach.types import Lane, OutreachError

NAME = "Sam Rivera"
ADDR = "sam@lumenlist.example.com"
NUL = chr(0)


def build(kit, draft=None, name=NAME, addr=ADDR, **kw):
    return build_message(draft or kit.draft(), name, addr, **kw)


def headers_text(msg):
    return "".join(f"{k}: {v}\n" for k, v in msg.items())


# ----- a NUL byte is as unsafe in a header as a line break ------------------------------------------------------------------------

NUL_CASES = {
    "subject": lambda kit: dict(draft=kit.draft(subject="hello" + NUL + "world")),
    "subject-override": lambda kit: dict(subject_override="hello" + NUL + "world"),
    "recipient": lambda kit: dict(draft=kit.draft(to="alice" + NUL + "@example.com")),
    "recipient-override": lambda kit: dict(to_override="me" + NUL + "@mail.test"),
    "sender-name": lambda kit: dict(name="Sam" + NUL + " Rivera"),
    "sender-address": lambda kit: dict(addr="sam" + NUL + "@lumenlist.example.com"),
}


@pytest.mark.parametrize("case", list(NUL_CASES), ids=list(NUL_CASES))
def test_a_nul_byte_never_reaches_a_header(kit, case):
    try:
        msg = build(kit, **NUL_CASES[case](kit))
    except (ValueError, OutreachError):
        return  # refusing is what the docstring promises
    assert NUL not in headers_text(msg), "a NUL byte was written into a header"


# ----- a follow up must be threaded, in the message builder as well as in the gate (SPEC 5 and 12) ------------------------------------

def test_a_follow_up_without_in_reply_to_is_refused_by_the_builder(kit):
    draft = kit.draft(lane=Lane.FOLLOW_UP, in_reply_to="", references="")
    with pytest.raises(OutreachError):
        build(kit, draft)


def test_a_follow_up_with_a_thread_still_builds(kit):
    msg = build(kit, kit.follow_up_draft())
    assert msg["In-Reply-To"]


def test_a_first_touch_without_in_reply_to_still_builds(kit):
    msg = build(kit, kit.draft(lane=Lane.FIRST_TOUCH, in_reply_to=""))
    assert msg["In-Reply-To"] is None


# ----- the sender address must look like an address -------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "nope", "no-at-sign.example.com"], ids=["empty", "blank", "word", "domain-only"])
def test_a_missing_or_malformed_sender_address_is_an_outreach_error_not_a_crash(kit, bad):
    with pytest.raises(OutreachError):
        build(kit, addr=bad)


def test_a_plain_sender_address_still_builds(kit):
    assert ADDR in str(build(kit)["From"])
