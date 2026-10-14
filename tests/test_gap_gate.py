"""Gaps found by the second mutation run: the send gate (SPEC sections 12, 17, 18, 19 and 20).

Every test here uses a FakeMailTransport, so no mail ever leaves the process. Where a refusal is
asserted it is asserted for the case that used to slip through: NO eligible draft at all, so the
only thing standing between the caller and a send is the up-front check of the gate itself.
"""
from __future__ import annotations

import dataclasses
import re

import pytest

from vcoutreach.send.gate import SendGateError, plan_sends

CONFIRM = "CONFIRM_LIVE_SEND"


def run(kit, drafts, *, mode="test", settings=None, live=False, execute=False, confirm="", limit=None,
        transport=None, **env):
    settings = settings if settings is not None else kit.send_settings(mode, **env)
    return plan_sends(
        drafts, settings, kit.sender(), kit.journal(),
        live=live, execute=execute, confirm_live=confirm, limit=limit, transport=transport,
    )


def run_live(kit, drafts, transport, **kw):
    return run(kit, drafts, mode="live", live=True, execute=True, confirm=CONFIRM, transport=transport, **kw)


def run_test_send(kit, drafts, transport, **kw):
    return run(kit, drafts, mode="test", execute=True, transport=transport, **kw)


def three(kit, middle=None):
    """alice and carol are fine; ``middle`` replaces bob."""
    bob = middle if middle is not None else kit.draft(contact_id="bob", to="bob@example.com")
    return [kit.draft(contact_id="alice", to="alice@example.com"), bob,
            kit.draft(contact_id="carol", to="carol@example.com")]


def planned_ids(report):
    return [p.draft.contact_id for p in report.planned]


def sent_ids(report):
    return [p.draft.contact_id for p in report.sent]


def skipped_ids(report):
    return [s.draft.contact_id for s in report.skipped]


NOTHING_TO_SEND = {
    "no-drafts": lambda kit: [],
    "only-a-blocked-draft": lambda kit: [kit.draft(contact_id="bob", to="bob@example.com", status="blocked")],
}


# ----- the gate refuses BEFORE it looks at the drafts (SPEC 17: live gate, transport, test recipient) ----------------------------

@pytest.mark.parametrize("kind", list(NOTHING_TO_SEND), ids=list(NOTHING_TO_SEND))
def test_live_flag_while_send_mode_is_test_is_refused_even_with_nothing_to_send(kit, kind):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run(kit, NOTHING_TO_SEND[kind](kit), mode="test", live=True, execute=True, confirm=CONFIRM, transport=transport)
    assert "SEND_MODE" in str(info.value)
    assert transport.attempts == 0 and not kit.journal_path.exists()


@pytest.mark.parametrize("kind", list(NOTHING_TO_SEND), ids=list(NOTHING_TO_SEND))
@pytest.mark.parametrize("confirm", ["", "WRONG", "confirm_live_send", CONFIRM + " ", "yes"],
                         ids=["empty", "wrong", "lower-case", "trailing-space", "yes"])
def test_live_without_the_exact_confirm_token_is_refused_even_with_nothing_to_send(kit, kind, confirm):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run(kit, NOTHING_TO_SEND[kind](kit), mode="live", live=True, execute=True, confirm=confirm, transport=transport)
    assert "confirm" in str(info.value).lower()
    assert transport.attempts == 0 and not kit.journal_path.exists()


@pytest.mark.parametrize("kind", list(NOTHING_TO_SEND), ids=list(NOTHING_TO_SEND))
@pytest.mark.parametrize("mode,live,confirm", [("test", False, ""), ("live", True, CONFIRM)], ids=["test-mode", "live-mode"])
def test_execute_without_a_transport_is_refused_even_with_nothing_to_send(kit, kind, mode, live, confirm):
    with pytest.raises(SendGateError):
        run(kit, NOTHING_TO_SEND[kind](kit), mode=mode, live=live, execute=True, confirm=confirm, transport=None)
    assert not kit.journal_path.exists()


@pytest.mark.parametrize("kind", list(NOTHING_TO_SEND), ids=list(NOTHING_TO_SEND))
@pytest.mark.parametrize("recipient", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_execute_in_test_mode_needs_the_test_recipient_even_with_nothing_to_send(kit, kind, recipient):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run(kit, NOTHING_TO_SEND[kind](kit), mode="test", execute=True, transport=transport, TEST_RECIPIENT_EMAIL=recipient)
    assert "TEST_RECIPIENT_EMAIL" in str(info.value)
    assert transport.attempts == 0 and not kit.journal_path.exists()


@pytest.mark.parametrize("kind", list(NOTHING_TO_SEND), ids=list(NOTHING_TO_SEND))
def test_a_plan_with_nothing_to_send_is_still_fine_without_a_transport_or_a_recipient(kit, kind):
    report = run(kit, NOTHING_TO_SEND[kind](kit), TEST_RECIPIENT_EMAIL=None)
    assert report.planned == () and report.sent == ()


# ----- an invalid SEND_MODE (SPEC 11: only test or live exist) -----------------------------------------------------------------------

def bogus_mode_settings(kit, value="bogus"):
    return dataclasses.replace(kit.send_settings("test"), send_mode=value)


@pytest.mark.parametrize("value", ["bogus", "LIVE", "Test", "production", ""], ids=["bogus", "upper-live", "title-test", "production", "empty"])
def test_an_invalid_send_mode_is_refused_even_with_every_live_flag_and_nothing_to_send(kit, value):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run(kit, [], settings=bogus_mode_settings(kit, value), live=True, execute=True, confirm=CONFIRM, transport=transport)
    assert "SEND_MODE" in str(info.value)
    assert transport.attempts == 0 and not kit.journal_path.exists()


def test_an_invalid_send_mode_never_sends_in_a_plain_execute(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run(kit, three(kit), settings=bogus_mode_settings(kit), execute=True, transport=transport)
    assert "SEND_MODE" in str(info.value)
    assert transport.attempts == 0 and transport.sent == [] and not kit.journal_path.exists()


# ----- header content that could inject a header (SPEC 18.5 and the build_message contract) ---------------------------------------

BAD_HEADER_DRAFTS = {
    "lf-in-subject": lambda kit: kit.draft(contact_id="bob", to="bob@example.com", subject="hello\nBcc: x@example.org"),
    "crlf-in-subject": lambda kit: kit.draft(contact_id="bob", to="bob@example.com", subject="hello\r\nBcc: x@example.org"),
    "cr-in-subject": lambda kit: kit.draft(contact_id="bob", to="bob@example.com", subject="hello\rworld"),
    "nul-in-subject": lambda kit: kit.draft(contact_id="bob", to="bob@example.com", subject="hello" + chr(0) + "world"),
    "lf-in-in-reply-to": lambda kit: kit.follow_up_draft(contact_id="bob", to="bob@example.com",
                                                        in_reply_to="<a@lumenlist.example.com>\nBcc: x@example.org"),
    "crlf-in-references": lambda kit: kit.follow_up_draft(contact_id="bob", to="bob@example.com",
                                                         references="<a@lumenlist.example.com>\r\nBcc: x@example.org"),
    "nul-in-in-reply-to": lambda kit: kit.follow_up_draft(contact_id="bob", to="bob@example.com",
                                                         in_reply_to="<a@lumenlist.example.com>" + chr(0)),
}


@pytest.mark.parametrize("name", list(BAD_HEADER_DRAFTS), ids=list(BAD_HEADER_DRAFTS))
def test_a_plan_leaves_out_a_draft_with_line_breaks_or_nul_in_a_header_field(kit, name):
    report = run(kit, three(kit, BAD_HEADER_DRAFTS[name](kit)))
    assert planned_ids(report) == ["alice", "carol"]
    assert skipped_ids(report) == ["bob"] and report.skipped[0].reason == "invalid_header"


@pytest.mark.parametrize("name", list(BAD_HEADER_DRAFTS), ids=list(BAD_HEADER_DRAFTS))
@pytest.mark.parametrize("mode", ["test", "live"])
def test_an_execute_skips_that_draft_and_still_sends_its_neighbours(kit, name, mode):
    transport = kit.mail_transport()
    drafts = three(kit, BAD_HEADER_DRAFTS[name](kit))
    report = (run_live if mode == "live" else run_test_send)(kit, drafts, transport)
    assert sent_ids(report) == ["alice", "carol"] and skipped_ids(report) == ["bob"]
    assert report.skipped[0].reason == "invalid_header"
    assert len(transport.sent) == 2
    assert all(m["Bcc"] is None for m in transport.sent)
    assert kit.expected_key("bob@example.com", "first_touch") not in {e["key"] for e in kit.journal_lines()}


# ----- a recipient that is a list (SPEC 18.5) -------------------------------------------------------------------------------------------

BAD_RECIPIENTS = [
    "bob@example.com, eve@example.org",
    "bob@example.com,eve@example.org",
    "bob@example.com\nBcc: eve@example.org",
    "bob@example.com\r\nBcc: eve@example.org",
]


@pytest.mark.parametrize("bad", BAD_RECIPIENTS, ids=["comma-space", "comma", "lf-bcc", "crlf-bcc"])
def test_a_plan_leaves_out_a_draft_whose_recipient_is_a_list(kit, bad):
    report = run(kit, three(kit, kit.draft(contact_id="bob", to=bad)))
    assert planned_ids(report) == ["alice", "carol"] and skipped_ids(report) == ["bob"]
    assert report.skipped[0].reason == "invalid_header"


@pytest.mark.parametrize("bad", BAD_RECIPIENTS, ids=["comma-space", "comma", "lf-bcc", "crlf-bcc"])
def test_a_live_run_skips_that_draft_and_the_good_ones_after_it_are_still_sent(kit, bad):
    transport = kit.mail_transport()
    report = run_live(kit, three(kit, kit.draft(contact_id="bob", to=bad)), transport)
    assert sent_ids(report) == ["alice", "carol"] and skipped_ids(report) == ["bob"]
    assert report.skipped[0].reason == "invalid_header"
    assert sorted(a for m in transport.sent for a in kit.recipients(m)) == ["alice@example.com", "carol@example.com"]


@pytest.mark.parametrize("bad", BAD_RECIPIENTS, ids=["comma-space", "comma", "lf-bcc", "crlf-bcc"])
def test_a_test_mode_run_does_not_send_that_draft_either(kit, bad):
    transport = kit.mail_transport()
    report = run_test_send(kit, three(kit, kit.draft(contact_id="bob", to=bad)), transport)
    assert sent_ids(report) == ["alice", "carol"] and skipped_ids(report) == ["bob"]
    assert report.skipped[0].reason == "invalid_header"
    assert len(transport.sent) == 2


# ----- the same recipient twice in one folder is caught by a plan too (SPEC 12 already_sent, README in 21.8) ----------------------------

def test_a_plan_lists_a_recipient_once_when_two_drafts_share_the_address(kit):
    drafts = [kit.draft(contact_id="alice", to="alice@example.com"), kit.draft(contact_id="alice-copy", to="alice@example.com")]
    report = run(kit, drafts)
    assert planned_ids(report) == ["alice"]
    assert [(s.draft.contact_id, s.reason) for s in report.skipped] == [("alice-copy", "already_sent")]
    assert not kit.journal_path.exists(), "a plan never touches the journal"


def test_the_address_comparison_in_a_plan_ignores_case(kit):
    drafts = [kit.draft(contact_id="alice", to="alice@example.com"), kit.draft(contact_id="alice-copy", to="Alice@Example.COM")]
    report = run(kit, drafts)
    assert planned_ids(report) == ["alice"] and skipped_ids(report) == ["alice-copy"]


def test_a_first_touch_and_a_follow_up_for_one_person_are_two_planned_sends(kit):
    drafts = [kit.draft(contact_id="alice", to="alice@example.com"),
              kit.follow_up_draft(contact_id="alice-up", to="alice@example.com")]
    report = run(kit, drafts)
    assert planned_ids(report) == ["alice", "alice-up"] and report.skipped == ()


def test_a_plan_and_an_execute_agree_on_a_repeated_address(kit):
    drafts = [kit.draft(contact_id="alice", to="alice@example.com"), kit.draft(contact_id="alice-copy", to="alice@example.com")]
    plan = run(kit, drafts)
    sent = run_test_send(kit, drafts, kit.mail_transport())
    assert planned_ids(plan) == sent_ids(sent) == ["alice"]
    assert skipped_ids(plan) == skipped_ids(sent) == ["alice-copy"]


# ----- an ok draft without a body or a subject is never sent (SPEC 12: lint gate again at send time) -------------------------------------

EMPTY_PARTS = {
    "empty-body": dict(body=""),
    "blank-body": dict(body="   "),
    "newlines-only-body": dict(body="\n\n\n"),
    "empty-subject": dict(subject=""),
    "blank-subject": dict(subject="   "),
    "tab-subject": dict(subject="\t"),
}


@pytest.mark.parametrize("name", list(EMPTY_PARTS), ids=list(EMPTY_PARTS))
def test_a_plan_leaves_out_an_ok_draft_with_an_empty_body_or_subject(kit, name):
    report = run(kit, three(kit, kit.draft(contact_id="bob", to="bob@example.com", **EMPTY_PARTS[name])))
    assert planned_ids(report) == ["alice", "carol"]
    assert skipped_ids(report) == ["bob"] and report.skipped[0].reason == "lint"


@pytest.mark.parametrize("name", list(EMPTY_PARTS), ids=list(EMPTY_PARTS))
@pytest.mark.parametrize("mode", ["test", "live"])
def test_an_execute_never_sends_a_blank_email(kit, name, mode):
    transport = kit.mail_transport()
    drafts = three(kit, kit.draft(contact_id="bob", to="bob@example.com", **EMPTY_PARTS[name]))
    report = (run_live if mode == "live" else run_test_send)(kit, drafts, transport)
    assert sent_ids(report) == ["alice", "carol"] and skipped_ids(report) == ["bob"]
    assert report.skipped[0].reason == "lint"
    assert len(transport.sent) == 2
    for message in transport.sent:
        assert kit.message_text(message).strip()
        subject = re.sub(r"^\[TEST to [^\]]*\]\s*", "", kit.header(message, "Subject") or "")
        assert subject.strip()
    assert kit.expected_key("bob@example.com", "first_touch") not in {e["key"] for e in kit.journal_lines()}


# ----- SENDER_EMAIL must be one plain address when executing (SPEC 17: Settings.sender_email) --------------------------------------------

BAD_SENDER_EMAILS = ["bad", "ann@example.com, cal@example.com", "Alex <ann@example.com>", "ann@example.com\r\nBcc: eve@example.org"]


@pytest.mark.parametrize("value", BAD_SENDER_EMAILS, ids=["no-at-sign", "two-addresses", "display-name", "crlf"])
@pytest.mark.parametrize("mode", ["test", "live"])
def test_executing_with_a_malformed_sender_email_is_a_gate_error_and_nothing_is_sent(kit, value, mode):
    transport = kit.mail_transport()
    settings = dataclasses.replace(kit.send_settings(mode), sender_email=value)
    live = mode == "live"
    with pytest.raises(SendGateError):
        run(kit, three(kit), settings=settings, mode=mode, live=live, execute=True,
            confirm=CONFIRM if live else "", transport=transport)
    assert transport.attempts == 0 and not kit.journal_path.exists()


def test_a_plain_sender_email_still_sends(kit):
    transport = kit.mail_transport()
    settings = dataclasses.replace(kit.send_settings("test"), sender_email="sam@lumenlist.example.com")
    report = run(kit, three(kit), settings=settings, execute=True, transport=transport)
    assert sent_ids(report) == ["alice", "bob", "carol"]
    assert all("sam@lumenlist.example.com" in str(m["From"]) for m in transport.sent)
