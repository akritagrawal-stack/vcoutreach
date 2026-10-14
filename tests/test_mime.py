"""send.mime.build_message (SPEC section 12 and 17)."""
from __future__ import annotations

import email
import email.policy
import re
from email.message import EmailMessage
from email.utils import parseaddr

import pytest

from vcoutreach.send.mime import build_message
from vcoutreach.types import OutreachError

NAME = "Sam Rivera"
ADDR = "sam@lumenlist.example.com"


def build(kit, draft=None, **kw):
    return build_message(draft or kit.draft(), NAME, ADDR, **kw)


def roundtrip(msg):
    return email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)


# ----- shape ---------------------------------------------------------------------------------------------------------

def test_result_is_a_plain_text_single_part_email_message(kit):
    msg = build(kit)
    assert isinstance(msg, EmailMessage)
    assert msg.get_content_type() == "text/plain"
    assert not msg.is_multipart()
    assert list(msg.iter_attachments()) == []


def test_from_to_and_subject_come_from_the_arguments_and_draft(kit):
    draft = kit.draft(to="alice@example.com", subject="a note for Alice")
    msg = build(kit, draft)
    assert parseaddr(kit.header(msg, "From")) == (NAME, ADDR)
    assert parseaddr(kit.header(msg, "To")) == ("", "alice@example.com")
    assert kit.header(msg, "Subject") == "a note for Alice"


def test_body_is_the_draft_body_exactly(kit):
    draft = kit.draft()
    assert kit.message_text(build(kit, draft)).rstrip("\n") == draft.body.rstrip("\n")


def test_a_message_id_is_generated_and_looks_like_one(kit):
    mid = build(kit)["Message-ID"]
    assert mid and re.fullmatch(r"<[^<>@\s]+@[^<>@\s]+>", mid.strip()), mid


def test_message_ids_are_unique_per_message(kit):
    ids = {build(kit)["Message-ID"] for _ in range(50)}
    assert len(ids) == 50


def test_no_stray_recipient_or_reply_headers_are_added(kit):
    msg = build(kit)
    for header in ("Cc", "Bcc", "Reply-To", "In-Reply-To", "References"):
        assert msg[header] is None, header


def test_the_message_serialises_and_parses_back_to_the_same_content(kit):
    draft = kit.draft(subject="a note for Alice")
    again = roundtrip(build(kit, draft))
    assert again["Subject"] == "a note for Alice"
    assert again["To"] == "alice@example.com"
    assert again.get_content().rstrip("\n") == draft.body.rstrip("\n")


# ----- overrides (test-mode rerouting) --------------------------------------------------------------------------------------

def test_to_override_replaces_the_recipient_entirely(kit):
    msg = build(kit, kit.draft(to="alice@example.com"), to_override="me@mail.test")
    assert kit.recipients(msg) == ["me@mail.test"]
    assert "alice@example.com" not in kit.header(msg, "To")


def test_subject_override_replaces_the_subject_verbatim(kit):
    msg = build(kit, kit.draft(subject="a note for Alice"), subject_override="[TEST to alice@example.com] a note for Alice")
    assert kit.header(msg, "Subject") == "[TEST to alice@example.com] a note for Alice"


def test_overrides_are_independent_of_each_other(kit):
    draft = kit.draft(to="alice@example.com", subject="a note for Alice")
    only_to = build(kit, draft, to_override="me@mail.test")
    only_subject = build(kit, draft, subject_override="changed")
    assert kit.header(only_to, "Subject") == "a note for Alice"
    assert kit.recipients(only_subject) == ["alice@example.com"]


# ----- threading ----------------------------------------------------------------------------------------------------------------

def test_follow_up_sets_in_reply_to_and_references_from_the_draft(kit):
    draft = kit.follow_up_draft(
        in_reply_to="<orig-001@lumenlist.example.com>",
        references="<orig-000@lumenlist.example.com> <orig-001@lumenlist.example.com>",
    )
    msg = build(kit, draft)
    assert msg["In-Reply-To"].strip() == "<orig-001@lumenlist.example.com>"
    assert msg["References"].split() == ["<orig-000@lumenlist.example.com>", "<orig-001@lumenlist.example.com>"]


def test_follow_up_without_separate_references_still_threads_on_the_parent(kit):
    msg = build(kit, kit.follow_up_draft(in_reply_to="<orig-001@lumenlist.example.com>", references=""))
    assert msg["In-Reply-To"].strip() == "<orig-001@lumenlist.example.com>"
    refs = msg["References"]
    assert refs is None or "<orig-001@lumenlist.example.com>" in refs


def test_follow_up_threading_survives_a_to_override(kit):
    msg = build(kit, kit.follow_up_draft(), to_override="me@mail.test")
    assert msg["In-Reply-To"] and msg["References"]


def test_first_touch_never_gets_threading_headers(kit):
    msg = build(kit, kit.draft(in_reply_to="", references=""))
    assert msg["In-Reply-To"] is None and msg["References"] is None


# ----- awkward text ---------------------------------------------------------------------------------------------------------------

def test_non_ascii_subject_name_and_body_round_trip(kit):
    body = kit.clean_body(first_name="Zo\u00eb", hook="I read your caf\u00e9 essay in \u65e5\u672c\u8a9e, and I found it interesting.")
    draft = kit.draft(contact_id="zoe", to="zoe@example.com", subject="caf\u00e9 note for Zo\u00eb", body=body)
    msg = build_message(draft, "Zo\u00eb Rivera", ADDR)
    assert kit.header(msg, "Subject") == "caf\u00e9 note for Zo\u00eb"
    assert parseaddr(kit.header(msg, "From"))[0] == "Zo\u00eb Rivera"
    again = roundtrip(msg)
    assert again.get_content().rstrip("\n") == body
    assert again["Subject"] == "caf\u00e9 note for Zo\u00eb"


def test_one_line_paragraphs_longer_than_the_smtp_limit_survive_unwrapped(kit):
    long_paragraph = ("steady careful small teams ship weekly because focus keeps scope honest " * 60).strip()
    assert len(long_paragraph) > 2000
    body = kit.clean_body(hook=long_paragraph)
    again = roundtrip(build(kit, kit.draft(body=body)))
    assert again.get_content().rstrip("\n") == body


def test_lines_that_look_like_mbox_or_smtp_markers_are_preserved(kit):
    body = "Hi Alice,\n\nFrom the start I liked it.\n\n.\n\nSam Rivera\n>quoted"
    again = roundtrip(build(kit, kit.draft(body=body)))
    assert again.get_content().rstrip("\n") == body


def test_sender_display_name_with_commas_and_quotes_stays_one_address(kit):
    msg = build_message(kit.draft(), 'Rivera, Sam "The Founder" Jr.', ADDR)
    name, addr = parseaddr(kit.header(msg, "From"))
    assert addr == ADDR
    assert "Rivera" in name
    assert len(email.utils.getaddresses([str(msg["From"])])) == 1


# ----- header injection and multiple recipients ---------------------------------------------------------------------------------------

def test_a_newline_in_the_subject_cannot_inject_a_header(kit):
    draft = kit.draft(subject="hello\r\nBcc: attacker@example.com")
    try:
        msg = build(kit, draft)
    except (ValueError, OutreachError):
        return  # refusing is safe
    assert msg["Bcc"] is None
    assert "attacker@example.com" not in kit.recipients(msg)
    assert "\n" not in str(msg["Subject"]).replace("\r\n ", " ").replace("\n ", " ")


def test_a_newline_in_the_override_subject_cannot_inject_a_header(kit):
    try:
        msg = build(kit, subject_override="hi\nBcc: attacker@example.com")
    except (ValueError, OutreachError):
        return
    assert msg["Bcc"] is None


# ----- SPEC 18.5: exactly one recipient, no line breaks ---------------------------------------------------------------------------

BAD_TO = [
    "alice@example.com, bob@example.com",
    "alice@example.com,bob@example.com",
    "alice@example.com\nbob@example.com",
    "alice@example.com\r\nBcc: attacker@example.com",
    "alice@example.com\rBcc: attacker@example.com",
    "alice@example.com\n",
    "\nalice@example.com",
    "alice@example.com\r\n",
]


@pytest.mark.parametrize("bad", BAD_TO, ids=[repr(b) for b in BAD_TO])
def test_a_draft_recipient_with_several_addresses_or_a_line_break_raises_outreach_error(kit, bad):
    with pytest.raises(OutreachError):
        build(kit, kit.draft(to=bad))


@pytest.mark.parametrize("bad", BAD_TO, ids=[repr(b) for b in BAD_TO])
def test_the_same_rule_applies_to_the_override_recipient(kit, bad):
    with pytest.raises(OutreachError):
        build(kit, kit.draft(to="alice@example.com"), to_override=bad)


@pytest.mark.parametrize("good", ["alice@example.com", "first.last+tag@sub.team.test", "UPPER@EXAMPLE.COM"])
def test_a_single_plain_address_is_accepted(kit, good):
    msg = build(kit, kit.draft(to=good))
    assert kit.recipients(msg) == [good.lower()]
