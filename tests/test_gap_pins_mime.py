"""SPEC section 22.5: build_message takes the Message-ID domain from the From address and refuses groups."""
from __future__ import annotations

import re
import socket

import pytest

from vcoutreach.send.mime import build_message
from vcoutreach.types import OutreachError

NAME = "Sam Rivera"
ADDR = "sam@lumenlist.example.com"


def build(kit, draft=None, name=NAME, addr=ADDR, **kw):
    return build_message(draft or kit.draft(), name, addr, **kw)


def message_id_domain(msg):
    match = re.fullmatch(r"<[^<>@\s]+@([^<>@\s]+)>", str(msg["Message-ID"]).strip())
    assert match, f"not a Message-ID: {msg['Message-ID']!r}"
    return match.group(1)


# ----- the Message-ID names the sender's domain, never this machine ---------------------------------------------------------------

@pytest.mark.parametrize("addr,domain", [
    ("sam@lumenlist.example.com", "lumenlist.example.com"),
    ("alex@other.example.org", "other.example.org"),
    ("Sam@LumenList.Example.COM", "lumenlist.example.com"),
    ("first.last+tag@sub.team.test", "sub.team.test"),
])
def test_the_message_id_domain_is_the_domain_of_the_from_address(kit, addr, domain):
    assert message_id_domain(build(kit, addr=addr)).lower() == domain


def test_the_message_id_does_not_leak_the_local_host_name(kit):
    host = socket.gethostname().strip().lower()
    mid = str(build(kit)["Message-ID"]).lower()
    if host and host not in ADDR:
        assert host not in mid, "the machine name must not appear in the Message-ID"


def test_message_ids_stay_unique_with_the_sender_domain(kit):
    ids = {str(build(kit)["Message-ID"]) for _ in range(5)}
    assert len(ids) == 5
    assert {message_id_domain(build(kit))} == {"lumenlist.example.com"}


def test_the_message_id_domain_follows_the_from_address_even_with_overrides(kit):
    msg = build(kit, to_override="me@mail.test", subject_override="[TEST to alice@example.com] hi")
    assert message_id_domain(msg) == "lumenlist.example.com"


# ----- RFC 5322 group syntax is not "one plain address" ---------------------------------------------------------------------------------

GROUPS = ["team: ann@example.com;", "team: ann@example.com, cal@example.com;", "Undisclosed recipients:;", "g:;", "team:ann@example.com;"]


@pytest.mark.parametrize("group", GROUPS, ids=["one-member", "two-members", "undisclosed", "empty-group", "no-space"])
def test_a_group_as_the_recipient_is_refused(kit, group):
    with pytest.raises(OutreachError):
        build(kit, kit.draft(to=group))


@pytest.mark.parametrize("group", GROUPS, ids=["one-member", "two-members", "undisclosed", "empty-group", "no-space"])
def test_a_group_as_the_override_recipient_is_refused(kit, group):
    with pytest.raises(OutreachError):
        build(kit, to_override=group)


@pytest.mark.parametrize("group", ["team: sam@lumenlist.example.com;", "g:;"], ids=["one-member", "empty-group"])
def test_a_group_as_the_sender_address_is_refused(kit, group):
    with pytest.raises(OutreachError):
        build(kit, addr=group)


def test_a_plain_recipient_and_sender_still_build(kit):
    msg = build(kit, kit.draft(to="alice@example.com"))
    assert kit.recipients(msg) == ["alice@example.com"]
