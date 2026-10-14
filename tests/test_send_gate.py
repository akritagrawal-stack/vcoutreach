"""send.gate.plan_sends: the send safeguards (SPEC sections 12 and 17).

Everything here uses a FakeMailTransport, so no mail ever leaves the process.
"""
from __future__ import annotations

import dataclasses
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import pytest

from vcoutreach.journal import Journal
from vcoutreach.send.gate import (
    MailTransport,
    PlannedSend,
    SendGateError,
    SendReceipt,
    SendReport,
    SkippedSend,
    plan_sends,
)
from vcoutreach.types import OutreachError

CONFIRM = "CONFIRM_LIVE_SEND"
EM = "\u2014"
EN = "\u2013"
TEST_ADDR = "me@mail.test"


def three(kit):
    return [
        kit.draft(contact_id="alice", to="alice@example.com"),
        kit.draft(contact_id="bob", to="bob@example.com"),
        kit.draft(contact_id="carol", to="carol@example.com"),
    ]


def run(kit, drafts, *, mode="test", live=False, execute=False, confirm="", limit=None,
        transport=None, journal=None, sender=None, **env):
    settings = kit.send_settings(mode, **env)
    return plan_sends(
        drafts, settings, sender or kit.sender(), journal if journal is not None else kit.journal(),
        live=live, execute=execute, confirm_live=confirm, limit=limit, transport=transport,
    )


def run_test_send(kit, drafts, transport, **kw):
    return run(kit, drafts, mode="test", execute=True, transport=transport, **kw)


def run_live(kit, drafts, transport, **kw):
    return run(kit, drafts, mode="live", live=True, execute=True, confirm=CONFIRM, transport=transport, **kw)


def to_of(kit, msg):
    return kit.recipients(msg)


def skipped_reasons(report):
    return [s.reason for s in report.skipped]


def sent_ids(report):
    return [p.draft.contact_id for p in report.sent]


# ----- exported shapes ----------------------------------------------------------------------------------------------

def test_gate_error_is_an_outreach_error():
    assert issubclass(SendGateError, OutreachError)


def test_exported_dataclasses_have_the_documented_fields_and_are_frozen():
    assert [f.name for f in dataclasses.fields(SendReceipt)] == ["message_id", "thread_id"]
    assert SendReceipt(message_id="<m@x>").thread_id == ""
    assert [f.name for f in dataclasses.fields(PlannedSend)] == ["draft", "to", "subject", "mode"]
    assert [f.name for f in dataclasses.fields(SkippedSend)] == ["draft", "reason", "detail"]
    assert SkippedSend(draft=None, reason="blocked").detail == ""  # type: ignore[arg-type]
    assert [f.name for f in dataclasses.fields(SendReport)] == ["planned", "sent", "skipped"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        SendReceipt(message_id="x").message_id = "y"  # type: ignore[misc]


def test_a_fake_mail_transport_satisfies_the_protocol(kit):
    from email.message import EmailMessage

    transport: MailTransport = kit.mail_transport()
    receipt = transport.send(EmailMessage())
    assert isinstance(receipt, SendReceipt) and receipt.message_id


# ----- plan only (the default) -----------------------------------------------------------------------------------------

def test_plan_only_sends_nothing_and_leaves_the_journal_untouched(kit):
    transport = kit.mail_transport()
    report = run(kit, three(kit), transport=transport)
    assert isinstance(report, SendReport)
    assert transport.attempts == 0 and transport.sent == []
    assert report.sent == ()
    assert len(report.planned) == 3
    assert not kit.journal_path.exists()


def test_plan_only_does_not_need_a_transport(kit):
    report = run(kit, three(kit), transport=None)
    assert len(report.planned) == 3 and report.sent == ()


def test_plan_shows_where_each_message_would_really_go_in_test_mode(kit):
    report = run(kit, three(kit))
    first = report.planned[0]
    assert isinstance(first, PlannedSend)
    assert first.mode == "test"
    assert first.to == TEST_ADDR
    assert first.subject == "[TEST to alice@example.com] a note for Alice"
    assert first.draft.contact_id == "alice"
    assert [p.to for p in report.planned] == [TEST_ADDR] * 3
    assert [p.draft.contact_id for p in report.planned] == ["alice", "bob", "carol"]


def test_plan_report_collections_are_tuples(kit):
    report = run(kit, three(kit))
    assert isinstance(report.planned, tuple) and isinstance(report.sent, tuple) and isinstance(report.skipped, tuple)


def test_an_empty_drafts_directory_plans_nothing_without_error(kit):
    report = run(kit, [], execute=True, transport=kit.mail_transport())
    assert report.planned == () and report.sent == () and report.skipped == ()


# ----- test mode --------------------------------------------------------------------------------------------------------------

def test_test_mode_reroutes_every_message_to_the_test_recipient_with_a_subject_prefix(kit):
    drafts = three(kit)
    transport = kit.mail_transport()
    report = run_test_send(kit, drafts, transport)
    assert len(transport.sent) == 3 and len(report.sent) == 3
    for draft, msg in zip(drafts, transport.sent):
        assert to_of(kit, msg) == [TEST_ADDR]
        assert kit.header(msg, "Subject") == f"[TEST to {draft.to}] {draft.subject}"
        assert draft.to not in to_of(kit, msg)
        assert kit.message_text(msg).rstrip("\n") == draft.body.rstrip("\n")
        assert parseaddr(kit.header(msg, "From")) == ("Sam Rivera", "sam@lumenlist.example.com")
    assert [p.mode for p in report.sent] == ["test"] * 3


def test_the_real_recipient_appears_nowhere_in_the_routing_headers_in_test_mode(kit):
    transport = kit.mail_transport()
    run_test_send(kit, [kit.draft(contact_id="alice", to="alice@example.com")], transport)
    (msg,) = transport.sent
    for header in ("To", "Cc", "Bcc", "Reply-To", "Sender"):
        assert "alice@example.com" not in str(msg[header] or "").lower()


def test_test_mode_journals_under_the_test_namespace_keyed_by_the_original_recipient(kit):
    run_test_send(kit, three(kit), kit.mail_transport())
    entries = kit.journal_lines()
    assert len(entries) == 3
    assert {e["mode"] for e in entries} == {"test"}
    assert {e["key"] for e in entries} == {
        kit.expected_key(f"{n}@example.com", "first_touch") for n in ("alice", "bob", "carol")
    }, "the journal key must come from the ORIGINAL recipient, not the rerouted test address"


def test_missing_test_recipient_is_a_gate_error_and_nothing_is_sent(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError):
        run_test_send(kit, three(kit), transport, TEST_RECIPIENT_EMAIL=None)
    assert transport.attempts == 0 and not kit.journal_path.exists()


def test_blank_test_recipient_is_also_a_gate_error(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError):
        run_test_send(kit, three(kit), transport, TEST_RECIPIENT_EMAIL="   ")
    assert transport.attempts == 0


UNSET_NOTICE = "<TEST_RECIPIENT_EMAIL not set>"


def test_a_plan_does_not_need_a_test_recipient(kit):
    report = run(kit, three(kit), TEST_RECIPIENT_EMAIL=None)
    assert len(report.planned) == 3 and report.sent == ()
    assert [p.to for p in report.planned] == [UNSET_NOTICE] * 3
    assert all(p.mode == "test" for p in report.planned)
    assert not kit.journal_path.exists()


def test_the_plan_notice_is_the_literal_text_and_is_not_an_address(kit):
    report = run(kit, three(kit), TEST_RECIPIENT_EMAIL=None)
    assert report.planned[0].to == "<TEST_RECIPIENT_EMAIL not set>"
    assert "@" not in report.planned[0].to


def test_a_blank_test_recipient_is_treated_as_unset_in_a_plan(kit):
    report = run(kit, three(kit), TEST_RECIPIENT_EMAIL="   ")
    assert [p.to for p in report.planned] == [UNSET_NOTICE] * 3


def test_a_plan_without_a_test_recipient_still_names_the_original_recipient_in_the_subject(kit):
    report = run(kit, three(kit), TEST_RECIPIENT_EMAIL=None)
    assert "alice@example.com" in report.planned[0].subject
    assert [p.draft.contact_id for p in report.planned] == ["alice", "bob", "carol"]


def test_a_plan_without_a_test_recipient_still_skips_what_it_would_skip(kit):
    drafts = three(kit) + [kit.draft(contact_id="dave", to="dave@example.com", status="blocked")]
    report = run(kit, drafts, TEST_RECIPIENT_EMAIL=None)
    assert [p.draft.contact_id for p in report.planned] == ["alice", "bob", "carol"]
    assert ("dave", "blocked") in [(s.draft.contact_id, s.reason) for s in report.skipped]


def test_executing_without_a_test_recipient_is_still_a_gate_error(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError):
        run(kit, three(kit), execute=True, transport=transport, TEST_RECIPIENT_EMAIL=None)
    assert transport.attempts == 0 and not kit.journal_path.exists()


def test_a_malformed_test_recipient_is_still_refused_in_a_plan_only_unset_is_allowed(kit):
    with pytest.raises(SendGateError):
        run(kit, three(kit), TEST_RECIPIENT_EMAIL="not-an-address")


def test_executing_without_a_transport_is_a_gate_error(kit):
    with pytest.raises(SendGateError):
        run(kit, three(kit), execute=True, transport=None)
    assert not kit.journal_path.exists()


def test_live_flag_while_settings_say_test_is_a_gate_error_never_a_silent_downgrade(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError):
        run(kit, three(kit), mode="test", live=True, execute=True, confirm=CONFIRM, transport=transport)
    assert transport.attempts == 0


def test_test_mode_ignores_the_daily_cap(kit):
    transport = kit.mail_transport()
    report = run_test_send(kit, three(kit), transport, DAILY_SEND_CAP="1")
    assert len(report.sent) == 3 and len(transport.sent) == 3


def test_test_mode_messages_do_not_count_toward_the_live_cap(kit):
    run_test_send(kit, three(kit), kit.mail_transport())
    live = kit.mail_transport()
    more = [kit.draft(contact_id=n, to=f"{n}@example.org") for n in ("dave", "erin", "frank")]
    report = run_live(kit, more, live, DAILY_SEND_CAP="2")
    assert len(report.sent) == 2


def test_repeating_a_test_run_does_not_resend_to_the_same_recipients(kit):
    first, second = kit.mail_transport(), kit.mail_transport()
    run_test_send(kit, three(kit), first)
    report = run_test_send(kit, three(kit), second)
    assert len(first.sent) == 3 and second.sent == []
    assert skipped_reasons(report) == ["already_sent"] * 3


# ----- live mode: all four conditions --------------------------------------------------------------------------------------------

def test_live_with_all_four_conditions_sends_to_the_real_recipients(kit):
    drafts = three(kit)
    transport = kit.mail_transport()
    report = run_live(kit, drafts, transport)
    assert len(transport.sent) == 3 and len(report.sent) == 3
    for draft, msg in zip(drafts, transport.sent):
        assert to_of(kit, msg) == [draft.to]
        assert kit.header(msg, "Subject") == draft.subject, "live subjects must not carry the TEST prefix"
        assert TEST_ADDR not in str(msg["To"]) + str(msg["Cc"] or "") + str(msg["Bcc"] or "")
    assert [p.mode for p in report.sent] == ["live"] * 3
    assert {e["mode"] for e in kit.journal_lines()} == {"live"}
    assert len(kit.journal_lines()) == 3


LIVE_GATE_CASES = {
    "settings-say-test": dict(mode="test", live=True, execute=True, confirm=CONFIRM),
    "no-live-flag": dict(mode="live", live=False, execute=True, confirm=CONFIRM),
    "no-execute": dict(mode="live", live=True, execute=False, confirm=CONFIRM),
    "no-confirm": dict(mode="live", live=True, execute=True, confirm=""),
    "confirm-wrong-case": dict(mode="live", live=True, execute=True, confirm="confirm_live_send"),
    "confirm-trailing-space": dict(mode="live", live=True, execute=True, confirm=CONFIRM + " "),
    "confirm-leading-space": dict(mode="live", live=True, execute=True, confirm=" " + CONFIRM),
    "confirm-yes": dict(mode="live", live=True, execute=True, confirm="yes"),
    "confirm-partial": dict(mode="live", live=True, execute=True, confirm="CONFIRM_LIVE"),
}


@pytest.mark.parametrize("case", list(LIVE_GATE_CASES), ids=list(LIVE_GATE_CASES))
def test_live_needs_every_condition_and_a_missing_one_sends_nothing(kit, case):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError):
        run(kit, three(kit), transport=transport, **LIVE_GATE_CASES[case])
    assert transport.attempts == 0 and transport.sent == []
    assert kit.journal_lines() == []


def test_live_without_a_transport_is_a_gate_error(kit):
    with pytest.raises(SendGateError):
        run(kit, three(kit), mode="live", live=True, execute=True, confirm=CONFIRM, transport=None)


def test_live_settings_without_any_flags_cannot_be_used_to_send(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError):
        run(kit, three(kit), mode="live", live=False, execute=True, confirm="", transport=transport)
    assert transport.attempts == 0


# ----- dedupe ----------------------------------------------------------------------------------------------------------------------

def test_a_second_live_run_skips_everything_as_already_sent(kit):
    first, second = kit.mail_transport(), kit.mail_transport()
    run_live(kit, three(kit), first)
    report = run_live(kit, three(kit), second)
    assert len(first.sent) == 3
    assert second.sent == [] and report.sent == ()
    assert skipped_reasons(report) == ["already_sent"] * 3
    assert all(isinstance(s, SkippedSend) for s in report.skipped)
    assert len(kit.journal_lines()) == 3, "skips must not add journal lines"


def test_the_journal_survives_a_fresh_process_style_reopen(kit):
    run_live(kit, three(kit), kit.mail_transport())
    reopened = Journal(kit.journal_path)
    second = kit.mail_transport()
    report = run_live(kit, three(kit), second, journal=reopened)
    assert second.sent == [] and skipped_reasons(report) == ["already_sent"] * 3


def test_a_test_send_never_blocks_a_later_live_send(kit):
    run_test_send(kit, three(kit), kit.mail_transport())
    live = kit.mail_transport()
    report = run_live(kit, three(kit), live)
    assert len(live.sent) == 3 and report.skipped == ()
    modes = Counter(e["mode"] for e in kit.journal_lines())
    assert modes == {"test": 3, "live": 3}


def test_a_live_send_is_not_repeated_even_if_the_address_changes_case(kit):
    first, second = kit.mail_transport(), kit.mail_transport()
    run_live(kit, [kit.draft(contact_id="alice", to="alice@example.com")], first)
    report = run_live(kit, [kit.draft(contact_id="alice2", to="ALICE@Example.COM")], second)
    assert second.sent == [] and skipped_reasons(report) == ["already_sent"]


def test_first_touch_and_follow_up_to_the_same_person_are_separate_sends(kit):
    transport = kit.mail_transport()
    drafts = [
        kit.draft(contact_id="alice", to="alice@example.com"),
        kit.follow_up_draft(contact_id="alice-fu", to="alice@example.com"),
    ]
    report = run_live(kit, drafts, transport)
    assert len(report.sent) == 2
    keys = {e["key"] for e in kit.journal_lines()}
    assert keys == {kit.expected_key("alice@example.com", "first_touch"), kit.expected_key("alice@example.com", "follow_up")}


def test_two_drafts_for_one_recipient_in_the_same_run_send_only_once(kit):
    transport = kit.mail_transport()
    drafts = [
        kit.draft(contact_id="alice", to="alice@example.com"),
        kit.draft(contact_id="alice-copy", to="alice@example.com"),
    ]
    report = run_live(kit, drafts, transport)
    assert len(transport.sent) == 1
    assert skipped_reasons(report) == ["already_sent"]


# ----- daily cap and limit ----------------------------------------------------------------------------------------------------------

def four(kit):
    return [kit.draft(contact_id=n, to=f"{n}@example.com") for n in ("alice", "bob", "carol", "dave")]


def test_live_sends_stop_at_the_daily_cap_and_the_rest_are_reported(kit):
    transport = kit.mail_transport()
    report = run_live(kit, four(kit), transport, DAILY_SEND_CAP="2")
    assert sent_ids(report) == ["alice", "bob"]
    assert len(transport.sent) == 2
    assert [(s.draft.contact_id, s.reason) for s in report.skipped] == [("carol", "daily_cap"), ("dave", "daily_cap")]


def test_the_cap_still_holds_on_a_second_run_the_same_day(kit):
    run_live(kit, four(kit), kit.mail_transport(), DAILY_SEND_CAP="2")
    again = kit.mail_transport()
    report = run_live(kit, four(kit), again, DAILY_SEND_CAP="2")
    assert again.sent == []
    by_id = {s.draft.contact_id: s.reason for s in report.skipped}
    assert by_id["carol"] == "daily_cap" and by_id["dave"] == "daily_cap"
    assert by_id["alice"] in ("already_sent", "daily_cap") and by_id["bob"] in ("already_sent", "daily_cap")


def test_a_cap_of_zero_sends_nothing(kit):
    transport = kit.mail_transport()
    report = run_live(kit, three(kit), transport, DAILY_SEND_CAP="0")
    assert transport.sent == [] and set(skipped_reasons(report)) == {"daily_cap"}


def test_live_sends_from_yesterday_do_not_use_up_todays_cap(kit):
    j = kit.journal()
    for i in range(5):
        j.record(Journal.key(f"old{i}@example.com", "first_touch"), "live")
    today = datetime.now(timezone.utc).date()
    text = kit.journal_path.read_text(encoding="utf-8")
    kit.journal_path.write_text(text.replace(today.isoformat(), (today - timedelta(days=1)).isoformat()), encoding="utf-8")
    transport = kit.mail_transport()
    report = run_live(kit, four(kit), transport, DAILY_SEND_CAP="2")
    assert len(report.sent) == 2


def test_limit_caps_the_number_of_messages_and_reports_the_rest(kit):
    transport = kit.mail_transport()
    report = run_test_send(kit, three(kit), transport, limit=1)
    assert len(transport.sent) == 1 and sent_ids(report) == ["alice"]
    assert skipped_reasons(report) == ["limit", "limit"]


def test_limit_also_shapes_a_plan(kit):
    report = run(kit, three(kit), limit=2)
    assert len(report.planned) == 2
    assert skipped_reasons(report) == ["limit"]


def test_limit_applies_to_live_sends_too(kit):
    transport = kit.mail_transport()
    report = run_live(kit, three(kit), transport, limit=2)
    assert len(transport.sent) == 2 and skipped_reasons(report) == ["limit"]


def test_a_limit_of_zero_plans_nothing_and_is_not_read_as_no_limit(kit):
    report = run(kit, three(kit), limit=0)
    assert len(report.planned) == 0
    assert len(report.skipped) == 3 and set(skipped_reasons(report)) == {"limit"}


def test_a_limit_of_zero_sends_nothing_even_when_executing(kit):
    transport = kit.mail_transport()
    report = run_test_send(kit, three(kit), transport, limit=0)
    assert len(transport.sent) == 0 and len(report.sent) == 0
    assert set(skipped_reasons(report)) == {"limit"}
    assert not kit.journal_path.exists() or kit.journal_lines() == []


# ----- eligibility ------------------------------------------------------------------------------------------------------------------

def test_blocked_drafts_are_never_sent_in_any_mode(kit):
    drafts = three(kit) + [kit.draft(contact_id="mallory", to="mallory@example.com", status="blocked")]
    for runner in (run_test_send, run_live):
        transport = kit.mail_transport()
        report = runner(kit, drafts, transport)
        assert "mallory@example.com" not in [r for m in transport.sent for r in to_of(kit, m)]
        assert ("mallory", "blocked") in [(s.draft.contact_id, s.reason) for s in report.skipped]
        assert "mallory" not in sent_ids(report)
        assert "mallory" not in [p.draft.contact_id for p in report.planned]


def test_a_blocked_draft_is_not_part_of_a_plan_either(kit):
    report = run(kit, [kit.draft(contact_id="mallory", to="m@example.com", status="blocked")])
    assert report.planned == () and skipped_reasons(report) == ["blocked"]


BROKEN_BODIES = {
    "em-dash": lambda kit: kit.clean_body(hook=f"I came across your essay {EM} on small teams, and I found it interesting."),
    "en-dash": lambda kit: kit.clean_body(hook=f"I came across your essay {EN} on small teams, and I found it interesting."),
    "exclamation": lambda kit: kit.clean_body(hook="I came across your essay, and I found it interesting!"),
    "filler": lambda kit: kit.clean_body(hook="I came across your essay. Just circling back on it."),
    "forbidden-phrase": lambda kit: kit.clean_body(hook="I came across your essay and its real synergy."),
    "hard-wrap": lambda kit: kit.clean_body(hook="I came across your essay on how\nsmall teams ship weekly."),
}


@pytest.mark.parametrize("name", list(BROKEN_BODIES), ids=list(BROKEN_BODIES))
def test_a_hand_edited_draft_that_now_fails_lint_is_not_sent_but_its_neighbours_are(kit, name):
    drafts = three(kit)
    drafts[1] = kit.draft(contact_id="bob", to="bob@example.com", body=BROKEN_BODIES[name](kit))
    transport = kit.mail_transport()
    report = run_live(kit, drafts, transport)
    assert sent_ids(report) == ["alice", "carol"]
    assert [(s.draft.contact_id, s.reason) for s in report.skipped] == [("bob", "lint")]
    assert "bob@example.com" not in [r for m in transport.sent for r in to_of(kit, m)]
    assert kit.expected_key("bob@example.com", "first_touch") not in {e["key"] for e in kit.journal_lines()}


LINT_DETAIL_CODE = {
    "em-dash": "em_dash", "en-dash": "em_dash", "exclamation": "exclamation", "filler": "filler",
    "forbidden-phrase": "forbidden_phrase", "hard-wrap": "hard_wrap",
}


@pytest.mark.parametrize("name", list(BROKEN_BODIES), ids=list(BROKEN_BODIES))
def test_a_lint_skip_carries_the_issue_codes_in_its_detail(kit, name):
    drafts = three(kit)
    drafts[1] = kit.draft(contact_id="bob", to="bob@example.com", body=BROKEN_BODIES[name](kit))
    report = run_live(kit, drafts, kit.mail_transport())
    (skipped,) = report.skipped
    assert skipped.reason == "lint"
    detail_codes = skipped.detail.split(", ")
    assert LINT_DETAIL_CODE[name] in detail_codes
    assert all(c and c == c.lower() and c.replace("_", "").isalpha() for c in detail_codes), skipped.detail
    assert ", ".join(detail_codes) == skipped.detail


def test_a_draft_with_two_lint_errors_lists_both_codes_in_its_detail(kit):
    body = kit.clean_body(hook=f"I came across your essay {EM} on small teams, and I found it interesting!")
    drafts = [kit.draft(contact_id="bob", to="bob@example.com", body=body)]
    report = run_live(kit, drafts, kit.mail_transport())
    (skipped,) = report.skipped
    assert skipped.reason == "lint"
    assert {"em_dash", "exclamation"} <= set(skipped.detail.split(", "))


def test_skips_for_other_reasons_still_have_a_text_detail(kit):
    first = kit.mail_transport()
    run_test_send(kit, three(kit)[:1], first)
    drafts = three(kit) + [kit.draft(contact_id="dave", to="dave@example.com", status="blocked")]
    report = run(kit, drafts)
    assert {s.reason for s in report.skipped} >= {"already_sent", "blocked"}
    assert all(isinstance(s.detail, str) for s in report.skipped)


def test_a_dash_added_to_the_subject_by_hand_blocks_the_send(kit):
    drafts = [kit.draft(contact_id="bob", to="bob@example.com", subject=f"a note {EM} for Bob")]
    transport = kit.mail_transport()
    report = run_test_send(kit, drafts, transport)
    assert transport.sent == [] and skipped_reasons(report) == ["lint"]


def test_word_count_warnings_do_not_block_a_send(kit):
    short = kit.draft(contact_id="bob", to="bob@example.com", body="Hi Bob,\n\nI liked your essay.\n\nSam Rivera\nFounder, Lumenlist")
    transport = kit.mail_transport()
    report = run_test_send(kit, [short], transport)
    assert len(transport.sent) == 1 and report.skipped == ()


# ----- threading -------------------------------------------------------------------------------------------------------------------

def test_follow_up_without_in_reply_to_is_ineligible_with_reason_missing_thread(kit):
    drafts = three(kit)
    drafts.append(kit.follow_up_draft(contact_id="dan", to="dan@example.com", in_reply_to="", references=""))
    transport = kit.mail_transport()
    report = run_live(kit, drafts, transport)
    assert sent_ids(report) == ["alice", "bob", "carol"]
    assert [(s.draft.contact_id, s.reason) for s in report.skipped] == [("dan", "missing_thread")]
    assert kit.expected_key("dan@example.com", "follow_up") not in {e["key"] for e in kit.journal_lines()}


def test_a_threaded_follow_up_goes_out_with_in_reply_to_and_references(kit):
    draft = kit.follow_up_draft(
        contact_id="grace", to="grace@example.org",
        in_reply_to="<orig-001@lumenlist.example.com>",
        references="<orig-000@lumenlist.example.com> <orig-001@lumenlist.example.com>",
    )
    transport = kit.mail_transport()
    report = run_live(kit, [draft], transport)
    assert len(report.sent) == 1
    (msg,) = transport.sent
    assert msg["In-Reply-To"].strip() == "<orig-001@lumenlist.example.com>"
    assert msg["References"].split() == ["<orig-000@lumenlist.example.com>", "<orig-001@lumenlist.example.com>"]
    assert kit.header(msg, "Subject") == draft.subject


def test_follow_up_in_test_mode_keeps_the_threading_headers(kit):
    transport = kit.mail_transport()
    run_test_send(kit, [kit.follow_up_draft()], transport)
    (msg,) = transport.sent
    assert msg["In-Reply-To"] and msg["References"]
    assert kit.header(msg, "Subject").startswith("[TEST to grace@example.org] ")


def test_first_touch_messages_carry_no_threading_headers(kit):
    transport = kit.mail_transport()
    run_test_send(kit, [kit.draft()], transport)
    (msg,) = transport.sent
    assert msg["In-Reply-To"] is None and msg["References"] is None


# ----- failure handling -------------------------------------------------------------------------------------------------------------

def test_every_successful_send_is_journaled_immediately_even_if_a_later_one_fails(kit):
    transport = kit.mail_transport(fail_on={2})
    try:
        run_live(kit, three(kit), transport)
    except Exception:
        pass  # aborting on the failure and carrying on are both acceptable
    delivered = [r for m in transport.sent for r in to_of(kit, m)]
    assert transport.attempts >= 2 and 1 <= len(delivered) <= 2
    assert {e["key"] for e in kit.journal_lines()} == {kit.expected_key(r, "first_touch") for r in delivered}
    assert len(kit.journal_lines()) == len(delivered), "a failed send must not be journaled"


def test_a_failed_send_is_not_journaled_and_can_be_retried_without_double_sending(kit):
    broken = kit.mail_transport(fail_on={2})
    try:
        run_live(kit, three(kit), broken)
    except Exception:
        pass
    healthy = kit.mail_transport()
    run_live(kit, three(kit), healthy)
    delivered = [r for t in (broken, healthy) for m in t.sent for r in to_of(kit, m)]
    assert sorted(delivered) == ["alice@example.com", "bob@example.com", "carol@example.com"]


def test_nothing_is_journaled_when_every_send_fails(kit):
    try:
        run_live(kit, three(kit), kit.mail_transport(fail_all=True))
    except Exception:
        pass
    assert kit.journal_lines() == []
    healthy = kit.mail_transport()
    run_live(kit, three(kit), healthy)
    assert len(healthy.sent) == 3


# ----- the message for SEND_MODE=live without --live (SPEC 20.6) -----------------------------------------------------

LIVE_WITHOUT_FLAG = (
    "SEND_MODE=live is set but --live was not passed. Nothing was sent. To preview without live risk use "
    "SEND_MODE=test (the default); to send for real you also need --live, --execute and --confirm-live CONFIRM_LIVE_SEND."
)


def test_live_mode_without_the_live_flag_explains_exactly_what_to_do(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run(kit, three(kit), mode="live", live=False, execute=True, confirm=CONFIRM, transport=transport)
    assert LIVE_WITHOUT_FLAG in str(info.value)
    assert transport.attempts == 0 and kit.journal_lines() == []


def test_the_live_without_flag_message_is_the_same_whatever_else_is_missing(kit):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run(kit, three(kit), mode="live", live=False, execute=True, confirm="", transport=transport)
    assert LIVE_WITHOUT_FLAG in str(info.value)


def test_the_live_flag_in_test_mode_keeps_its_own_message(kit):
    with pytest.raises(SendGateError) as info:
        run(kit, three(kit), mode="test", live=True, execute=True, confirm=CONFIRM, transport=kit.mail_transport())
    assert "was not passed" not in str(info.value)
    assert LIVE_WITHOUT_FLAG not in str(info.value)


# ----- the Gmail transport stays optional ----------------------------------------------------------------------------------------------


def test_importing_the_gmail_module_never_imports_google_libraries(kit):
    code = (
        "import sys\n"
        "import vcoutreach.send.gmail, vcoutreach.send.gate, vcoutreach.send.mime, vcoutreach.cli\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'google', 'googleapiclient', 'google_auth_oauthlib', 'oauthlib', 'httplib2'})\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = kit.run_python(code)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_gmail_transport_without_the_extra_fails_with_the_install_hint(kit):
    from email.message import EmailMessage

    kit.hide_google()

    from vcoutreach.send.gmail import GmailTransport

    settings = kit.send_settings("live")
    with pytest.raises(OutreachError) as info:
        GmailTransport(settings).send(EmailMessage())
    assert "pip install -r requirements-gmail.txt" in str(info.value)
    assert not settings.gmail_token_path.exists(), "no token file may appear when the extra is missing"


def test_authorize_without_the_extra_fails_with_the_install_hint(kit):
    from vcoutreach.send.gmail import authorize

    kit.hide_google()

    settings = kit.send_settings("live")
    with pytest.raises(OutreachError) as info:
        authorize(settings)
    assert "pip install -r requirements-gmail.txt" in str(info.value)
    assert not settings.gmail_token_path.exists()


# ----- SPEC 18.1: the test recipient must be one valid address and must not be a real recipient --------------------------

def test_a_draft_addressed_to_the_test_recipient_is_a_gate_error_naming_the_draft(kit):
    drafts = three(kit)
    drafts[1] = kit.draft(contact_id="bob", to=TEST_ADDR)
    transport = kit.mail_transport()
    with pytest.raises(SendGateError) as info:
        run_test_send(kit, drafts, transport)
    assert "bob" in str(info.value)
    assert transport.attempts == 0 and kit.journal_lines() == []


@pytest.mark.parametrize("clash", ["ME@Mail.TEST", "Me@mail.test", TEST_ADDR])
def test_the_collision_check_ignores_case_and_also_guards_a_plan(kit, clash):
    drafts = three(kit) + [kit.draft(contact_id="dup", to=clash)]
    with pytest.raises(SendGateError) as info:
        run(kit, drafts)
    assert "dup" in str(info.value)


def test_a_blocked_draft_to_the_test_recipient_is_not_eligible_so_it_does_not_trip_the_guard(kit):
    drafts = three(kit) + [kit.draft(contact_id="dup", to=TEST_ADDR, status="blocked")]
    transport = kit.mail_transport()
    report = run_test_send(kit, drafts, transport)
    assert len(transport.sent) == 3
    assert ("dup", "blocked") in [(s.draft.contact_id, s.reason) for s in report.skipped]


def test_live_mode_does_not_apply_the_test_mode_collision_guard(kit):
    transport = kit.mail_transport()
    report = run_live(kit, [kit.draft(contact_id="dup", to=TEST_ADDR)], transport)
    assert len(report.sent) == 1 and kit.recipients(transport.sent[0]) == [TEST_ADDR]


BAD_TEST_RECIPIENTS = [
    "a@mail.test,b@mail.test",
    "a@mail.test, b@mail.test",
    "a@mail.test b@mail.test",
    "a@mail.test\nb@mail.test",
    "a@mail.test\r\nBcc: z@mail.test",
    "a@mail.test\n",
    "not-an-address",
    "@mail.test",
    "a@mail",
    "a@@mail.test",
]


@pytest.mark.parametrize("bad", BAD_TEST_RECIPIENTS, ids=[repr(b) for b in BAD_TEST_RECIPIENTS])
def test_a_malformed_or_multi_address_test_recipient_is_a_gate_error(kit, bad):
    transport = kit.mail_transport()
    with pytest.raises(SendGateError):
        run_test_send(kit, three(kit), transport, TEST_RECIPIENT_EMAIL=bad)
    assert transport.attempts == 0 and kit.journal_lines() == []


def test_a_comma_in_the_test_recipient_is_also_refused_for_a_plan(kit):
    with pytest.raises(SendGateError):
        run(kit, three(kit), TEST_RECIPIENT_EMAIL="a@mail.test,b@mail.test")


@pytest.mark.parametrize("good", ["me@mail.test", "first.last+qa@sub.mail.test"])
def test_ordinary_single_test_recipients_are_accepted(kit, good):
    transport = kit.mail_transport()
    run_test_send(kit, three(kit), transport, TEST_RECIPIENT_EMAIL=good)
    assert [kit.recipients(m) for m in transport.sent] == [[good]] * 3


def test_a_hand_edited_draft_with_two_recipients_never_reaches_a_second_address_when_sent_live(kit):
    drafts = [
        kit.draft(contact_id="alice", to="alice@example.com"),
        kit.draft(contact_id="evil", to="evil@example.com, attacker@example.com"),
    ]
    transport = kit.mail_transport()
    try:
        run_live(kit, drafts, transport)
    except OutreachError:
        pass
    delivered = [r for m in transport.sent for r in kit.recipients(m)]
    assert "attacker@example.com" not in delivered


# ----- SPEC 18.2: one executing run at a time -----------------------------------------------------------------------------------

def lock_path(kit):
    return kit.journal_path.parent / "send.lock"


def hold_lock(kit):
    fcntl = pytest.importorskip("fcntl")
    path = lock_path(kit)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def lock_is_free(kit):
    fcntl = pytest.importorskip("fcntl")
    path = lock_path(kit)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


class BlockingMailTransport:
    """Holds the first send open until the test says go, so a second run can collide with it."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.sent = []
        self.attempts = 0

    def send(self, message):
        self.attempts += 1
        self.entered.set()
        assert self.release.wait(30), "the test never released the first run"
        self.sent.append(message)
        return SendReceipt(message_id="<blocked@mail.test>")


class LockProbeTransport:
    """Records whether the execute lock is held while a message is being sent."""

    def __init__(self, kit):
        self.kit = kit
        self.free_during_send = []
        self.sent = []

    def send(self, message):
        self.free_during_send.append(lock_is_free(self.kit))
        self.sent.append(message)
        return SendReceipt(message_id=f"<probe-{len(self.sent)}@mail.test>")


def test_execute_while_the_lock_is_held_is_a_gate_error_and_sends_nothing(kit):
    handle = hold_lock(kit)
    try:
        transport = kit.mail_transport()
        with pytest.raises(SendGateError) as info:
            run_test_send(kit, three(kit), transport)
        assert "another send run is in progress" in str(info.value)
        assert transport.attempts == 0 and kit.journal_lines() == []
        live = kit.mail_transport()
        with pytest.raises(SendGateError):
            run_live(kit, three(kit), live)
        assert live.attempts == 0
    finally:
        handle.close()
    after = kit.mail_transport()
    run_test_send(kit, three(kit), after)
    assert len(after.sent) == 3, "once the other run is gone, sending works again"


def test_a_plan_only_run_does_not_need_the_lock(kit):
    handle = hold_lock(kit)
    try:
        report = run(kit, three(kit))
        assert len(report.planned) == 3
    finally:
        handle.close()


def test_the_lock_is_held_for_the_whole_run_and_released_after_it(kit):
    probe = LockProbeTransport(kit)
    run_test_send(kit, three(kit), probe)
    assert probe.free_during_send == [False, False, False]
    assert lock_is_free(kit)


def test_the_lock_is_released_even_when_the_transport_raises(kit):
    for transport in (kit.mail_transport(fail_all=True), kit.mail_transport(fail_on={2})):
        try:
            run_live(kit, three(kit), transport)
        except Exception:
            pass
        assert lock_is_free(kit), "a crashed run must not leave the lock behind"
    healthy = kit.mail_transport()
    run_test_send(kit, three(kit), healthy)
    assert len(healthy.sent) == 3


def test_the_lock_is_released_when_the_gate_refuses_the_run(kit):
    with pytest.raises(SendGateError):
        run(kit, three(kit), mode="test", live=True, execute=True, confirm=CONFIRM, transport=kit.mail_transport())
    assert lock_is_free(kit)
    with pytest.raises(SendGateError):
        run_test_send(kit, three(kit), kit.mail_transport(), TEST_RECIPIENT_EMAIL=None)
    assert lock_is_free(kit)


def test_two_overlapping_execute_runs_cannot_both_send(kit):
    first = BlockingMailTransport()
    errors = []

    def worker():
        try:
            run_test_send(kit, [kit.draft()], first)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert first.entered.wait(15), "the first run never started sending"
        second = kit.mail_transport()
        with pytest.raises(SendGateError) as info:
            run_test_send(kit, three(kit), second)
        assert "another send run is in progress" in str(info.value)
        assert second.attempts == 0
    finally:
        first.release.set()
        thread.join(30)
    assert errors == []
    assert len(first.sent) == 1
    assert lock_is_free(kit)
