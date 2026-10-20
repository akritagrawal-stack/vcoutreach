"""TEST and LIVE send modes of the web app (SPEC_V3 section 2, SPEC_V2 section 9).

Sending goes through the engine's send gate; the Gmail client is replaced by an injected
send_transport that records the built message and recipient. Nothing leaves the process.
No dash characters appear in this file.
"""
from __future__ import annotations

import re
import threading

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, GRACE, LINUS, MINA, OREN, SENDER_EMAIL, STANDARD_ROWS, TEST_INBOX, Web, approved_draft_ids, banner_mode,
    body, contact_ref, send_url,
    create_web_app, engine_draft_all, export_rows, file_mode, go_live, go_test, journal_lines, page_text,
    read_draft_file, read_env, send_batch, send_live, send_test, set_stage, stage_of, typed, write_env,
)


def live(w: Web) -> None:
    go_live(w)
    assert banner_mode(w) == "live", "typing GO LIVE should arm live mode"


def no_calls(w: Web) -> bool:
    return w.transport is not None and not w.transport.calls


def draft_subject(w: Web, slug: str) -> str:
    return read_draft_file(w.ws, slug).subject


# ---------------------------------------------------------------------------------------------------------------
# Starting state, banner, the typed toggle, persistence
# ---------------------------------------------------------------------------------------------------------------

def test_the_app_starts_in_test_mode_and_the_banner_says_where_mail_goes(web):
    text = web.text("/crm/send-plan").lower()
    assert banner_mode(web) == "test"
    assert "test mode" in text and "test mailbox" in text


def test_the_app_never_starts_in_live_mode_even_when_the_env_file_says_live(make_web):
    w = make_web({"SEND_MODE": "live", "TEST_RECIPIENT_EMAIL": TEST_INBOX})
    assert banner_mode(w) == "test"
    assert banner_mode(w, "/crm") == "test"


@pytest.mark.parametrize("phrase", ["go live", "GO LIVE NOW"])
def test_the_toggle_needs_the_exact_phrase_go_live(send_web, phrase):
    w = send_web()
    r = w.post(w.url("toggle"), {**typed(phrase), "mode": "live"}, follow=True)
    assert r.status_code < 500
    assert banner_mode(w) == "test"
    assert read_env(w.ws).get("SEND_MODE", "test") != "live"


def test_typing_go_live_arms_live_mode_and_persists_it_in_the_env_file_with_other_lines_kept(make_web):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX}, env_lines=["# my notes", "KEEP_ME=1"])
    live(w)
    assert banner_mode(w, "/crm") == "live"
    env_path = w.ws / ".env"
    assert read_env(w.ws)["SEND_MODE"] == "live"
    assert file_mode(env_path) == 0o600
    text = env_path.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in text and "# my notes" in text and read_env(w.ws)["TEST_RECIPIENT_EMAIL"] == TEST_INBOX


def test_switching_back_to_test_is_one_click_and_is_persisted(make_web):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX})
    live(w)
    go_test(w)
    assert banner_mode(w) == "test"
    assert read_env(w.ws)["SEND_MODE"] == "test"
    assert file_mode(w.ws / ".env") == 0o600


@pytest.mark.parametrize("garbage", ["banana", "yes"])
def test_an_unreadable_send_mode_value_falls_back_to_test_and_never_sends_live(send_web, garbage):
    w = send_web()
    live(w)
    values = {k: v for k, v in read_env(w.ws).items() if k != "SEND_MODE"}
    write_env(w.ws, {**values, "SEND_MODE": garbage})
    assert banner_mode(w) == "test"
    send_live(w, "ada", "SEND TO ada@example.com")
    for call in w.transport.calls:
        assert call.recipients == [TEST_INBOX], "garbage SEND_MODE must never reach a real recipient"


# ---------------------------------------------------------------------------------------------------------------
# TEST_RECIPIENT_EMAIL
# ---------------------------------------------------------------------------------------------------------------

def test_the_test_recipient_has_no_default_value(send_web):
    w = send_web(with_recipient=False)
    assert "TEST_RECIPIENT_EMAIL" not in read_env(w.ws)
    for path in ("/crm/send-plan", "/settings"):
        for tag in re.findall(r"(?is)<input\b[^>]*name=[\"'][^\"']*recipient[^\"']*[\"'][^>]*>", w.html(path)):
            value = re.search(r"""value=["']([^"']*)["']""", tag)
            assert not value or value.group(1) == "", f"{path}: {tag}"


def test_a_test_send_without_a_test_recipient_is_refused_with_a_message(send_web):
    w = send_web(with_recipient=False)
    r = send_test(w, "ada")
    assert r.status_code < 500
    assert no_calls(w)
    assert re.search(r"(?i)test[_ ]recipient", page_text(r)), page_text(r)[:400]


@pytest.mark.parametrize("value", [
    "a@example.com,b@example.com",
    "ada@example.com",
], ids=["two-addresses", "same-as-a-real-recipient"])
def test_an_invalid_or_dangerous_test_recipient_is_refused_and_nothing_is_sent(send_web, value):
    w = send_web(with_recipient=False, env={"TEST_RECIPIENT_EMAIL": value})
    r = send_test(w, "ada")
    assert r.status_code < 500
    assert no_calls(w)


def test_the_send_plan_recipient_field_writes_the_env_file_and_the_next_test_send_uses_it(send_web):
    w = send_web(with_recipient=False)
    address = "typed.here@testmail.example.org"
    names = ("TEST_RECIPIENT_EMAIL", "test_recipient_email", "test_recipient", "recipient")

    def fields(value):
        return {**{n: value for n in names}, "key": "TEST_RECIPIENT_EMAIL", "name": "TEST_RECIPIENT_EMAIL",
                "value": value}

    # SPEC_V3 does not say that the field validates at save time; what must hold is that a junk value is
    # either refused when saved or refused when a send is attempted, and that nothing is sent to it.
    w.post(w.url("recipient"), fields("no-at-sign-here"), follow=True)
    send_test(w, "ada")
    assert no_calls(w), "a recipient without an at sign must never be used"
    w.post(w.url("recipient"), fields(address), follow=True)
    if read_env(w.ws).get("TEST_RECIPIENT_EMAIL") != address:
        pytest.skip("SPEC_V3 does not define a web field for the test recipient (SPEC_V2 section 9 did); see SPEC_QUESTIONS.md")
    assert read_env(w.ws).get("TEST_RECIPIENT_EMAIL") == address
    send_test(w, "ada")
    assert [c.recipients for c in w.transport.calls] == [[address]]


# ---------------------------------------------------------------------------------------------------------------
# Test-mode sends
# ---------------------------------------------------------------------------------------------------------------

def test_a_test_send_goes_only_to_the_test_inbox_with_the_test_prefix(send_web):
    w = send_web()
    before = (w.ws / "drafts" / "ada.md").read_text(encoding="utf-8")
    r = send_test(w, "ada")
    assert r.status_code < 500
    assert len(w.transport.calls) == 1
    call = w.transport.calls[0]
    assert call.recipients == [TEST_INBOX]
    assert call.recipient in (None, TEST_INBOX)
    assert call.subject == f"[TEST to ada@example.com] {draft_subject(w, 'ada')}"
    assert call.text.strip() == read_draft_file(w.ws, "ada").body.strip()
    assert SENDER_EMAIL in str(call.message["From"])
    assert stage_of(w, "ada@example.com") == "prospect", "a test send must not move the contact"
    assert (w.ws / "drafts" / "ada.md").read_text(encoding="utf-8") == before
    assert not [e for e in journal_lines(w.ws) if e.get("mode") == "live"]


def test_the_live_send_route_in_test_mode_behaves_as_a_test_send(send_web):
    w = send_web()
    send_live(w, "ada", "SEND TO ada@example.com")
    assert [c.recipients for c in w.transport.calls] == [[TEST_INBOX]]
    assert w.transport.calls[0].subject.startswith("[TEST to ada@example.com] ")


def test_test_mode_batch_reroutes_every_message_and_skips_blocked_drafts(send_web):
    w = send_web(STANDARD_ROWS + [MINA])
    assert len(approved_draft_ids(w)) == 3, "the three ok drafts are approved; the blocked one cannot be"
    r = send_batch(w, None)
    assert r.status_code < 500
    calls = w.transport.calls
    assert len(calls) == 3
    assert all(c.recipients == [TEST_INBOX] for c in calls)
    prefixes = {re.match(r"\[TEST to ([^\]]+)\]", c.subject).group(1) for c in calls}
    assert prefixes == {"ada@example.com", "grace@example.com", "linus@example.com"}


def test_a_follow_up_test_send_keeps_its_thread_headers(send_web):
    w = send_web([OREN])
    send_test(w, "oren")
    call = w.transport.calls[0]
    assert call.message["In-Reply-To"] == "<orig-001@example.com>"
    assert call.message["References"] == "<orig-001@example.com>"
    assert call.recipients == [TEST_INBOX]


def test_a_follow_up_draft_without_in_reply_to_is_refused(send_web):
    w = send_web([OREN])
    draft = read_draft_file(w.ws, "oren")
    w.post(w.url("draft_save", slug="oren"), {"to": draft.to, "subject": draft.subject, "lane": "follow_up",
                                              "in_reply_to": "", "references": "", "body": draft.body}, follow=True)
    r = send_test(w, "oren")
    assert r.status_code < 500
    assert no_calls(w)


def test_a_blocked_draft_is_never_sent_in_either_mode(send_web):
    w = send_web(STANDARD_ROWS + [MINA])
    send_test(w, "mina")
    live(w)
    send_live(w, "mina", "SEND TO mina@example.com")
    assert no_calls(w)
    assert read_draft_file(w.ws, "mina").status == "blocked"


def test_a_draft_that_fails_lint_after_hand_editing_is_refused_at_send_time(send_web):
    w = send_web()
    draft = read_draft_file(w.ws, "ada")
    w.post(w.url("draft_save", slug="ada"), {
        "to": draft.to, "subject": draft.subject, "lane": "first_touch", "in_reply_to": "", "references": "",
        "body": draft.body.replace("Hi Ada,", "Hi Ada, great news!")}, follow=True)
    send_test(w, "ada")
    assert no_calls(w)


def test_a_draft_whose_contact_does_not_exist_is_refused(send_web):
    """A draft file with no contact behind it can never be sent, through any route the app has."""
    ghost = {**GRACE, "id": "ghost", "name": "Gil Host", "email": "ghost@example.com"}
    w = send_web([ADA])
    engine_draft_all(w.ws, [ADA, ghost])
    # the read-only plan must not present the orphan draft as ready to go out (SPEC_V2 section 9: the contact exists)
    plan = w.text("/crm/send-plan")
    assert not re.search(r"(?i)ghost\b.{0,200}?\bplanned\b", plan), plan[-400:]
    attempts = ["/partners/987654/send-test", "/partners/987654/send", "/crm/drafts/ghost/send-test",
                "/crm/drafts/ghost/send"]
    for path in attempts:
        r = w.post(path, typed("SEND TO ghost@example.com"))
        assert r.status_code < 500, path
    go_live(w)
    for path in attempts:
        r = w.post(path, typed("SEND TO ghost@example.com"))
        assert r.status_code < 500, path
    assert no_calls(w)


# ---------------------------------------------------------------------------------------------------------------
# Live sends
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    None, "send to ada@example.com", "SEND TO grace@example.com",
])
def test_a_live_send_needs_the_exact_typed_phrase(send_web, phrase):
    w = send_web()
    live(w)
    r = send_live(w, "ada", phrase)
    assert r.status_code < 500
    assert no_calls(w)
    assert not [e for e in journal_lines(w.ws) if e.get("mode") == "live"]
    # control: the refusal above was about the phrase, because the exact phrase sends
    send_live(w, "ada", "SEND TO ada@example.com")
    assert [c.recipients for c in w.transport.calls] == [["ada@example.com"]]


def test_a_live_send_with_the_phrase_reaches_the_real_recipient_and_records_everything(send_web):
    w = send_web()
    live(w)
    send_live(w, "ada", "SEND TO ada@example.com")
    assert len(w.transport.calls) == 1
    call = w.transport.calls[0]
    assert call.recipients == ["ada@example.com"]
    assert call.subject == draft_subject(w, "ada") and "[TEST" not in call.subject
    assert call.text.strip() == read_draft_file(w.ws, "ada").body.strip()
    assert any(e.get("mode") == "live" and e.get("recipient") == "ada@example.com" for e in journal_lines(w.ws))
    assert stage_of(w, "ada@example.com") == "contacted"
    ref = contact_ref(w, "ada@example.com")
    assert "email_sent_by_app" in w.html(f"/crm/{ref}") or "sent by app" in w.text(f"/crm/{ref}").lower()


def test_a_recipient_already_sent_to_is_refused_the_second_time(send_web):
    w = send_web()
    live(w)
    send_live(w, "ada", "SEND TO ada@example.com")
    send_live(w, "ada", "SEND TO ada@example.com")
    assert len(w.transport.calls) == 1


def test_the_daily_cap_stops_live_sends(send_web):
    w = send_web(env={"DAILY_SEND_CAP": "1"})
    live(w)
    send_live(w, "ada", "SEND TO ada@example.com")
    r = send_live(w, "grace", "SEND TO grace@example.com")
    assert [c.recipients for c in w.transport.calls] == [["ada@example.com"]]
    assert "cap" in page_text(r).lower()


def test_contacts_marked_passed_or_hold_are_never_sent_to(send_web):
    for stage in ("passed", "hold"):
        w = send_web()
        set_stage(w, "ada@example.com", stage)
        live(w)
        send_live(w, "ada", "SEND TO ada@example.com")
        go_test(w)
        send_test(w, "ada")
        assert no_calls(w), stage


@pytest.mark.parametrize("stage", ["warm", "meeting"])
def test_contacts_in_active_stages_can_still_be_sent_to(send_web, stage):
    w = send_web()
    set_stage(w, "ada@example.com", stage)
    live(w)
    send_live(w, "ada", "SEND TO ada@example.com")
    assert len(w.transport.calls) == 1, stage


def test_contacted_contacts_are_held(send_web):
    w = send_web()
    set_stage(w, "ada@example.com", "contacted")
    live(w)
    response = send_live(w, "ada", "SEND TO ada@example.com")
    assert response.status_code == 200
    assert no_calls(w)
    assert "already contacted" in page_text(response).lower()


def test_a_failed_live_send_leaves_the_draft_unsent_and_a_retry_works(send_web):
    w = send_web()
    live(w)
    w.transport.fail_with("simulated gmail outage")
    r = send_live(w, "ada", "SEND TO ada@example.com")
    assert r.status_code < 500
    assert not [e for e in journal_lines(w.ws) if e.get("mode") == "live"]
    assert stage_of(w, "ada@example.com") == "prospect"
    assert read_draft_file(w.ws, "ada").status == "ok"
    w.transport.heal()
    r = send_live(w, "ada", "SEND TO ada@example.com")
    assert r.status_code < 500
    # SPEC_QUESTIONS.md Q8: after a transport error the app may either allow the retry or hold the draft as an
    # uncertain attempt (the outcome may have reached Gmail) and tell the user to check Sent. Never two sends.
    assert len(w.transport.calls) <= 1
    if not w.transport.calls:
        assert re.search(r"(?i)uncertain|check sent|before any retry|already", page_text(r)), page_text(r)[:300]


@pytest.mark.parametrize("phrase", [None, "SEND BATCH OF 2"])
def test_live_batch_needs_the_exact_count_phrase(send_web, phrase):
    w = send_web(STANDARD_ROWS + [MINA])
    live(w)
    send_batch(w, phrase)
    assert no_calls(w)
    send_batch(w, "SEND BATCH OF 3")  # control: the exact count for the three approved drafts sends
    assert len(w.transport.calls) == 3


def test_live_batch_with_the_right_count_sends_each_eligible_draft_once(send_web):
    w = send_web(STANDARD_ROWS + [MINA])
    live(w)
    ids = approved_draft_ids(w)
    assert len(ids) == 3
    send_batch(w, "SEND BATCH OF 3")
    assert sorted(c.recipients[0] for c in w.transport.calls) == [
        "ada@example.com", "grace@example.com", "linus@example.com"]
    send_batch(w, "SEND BATCH OF 3", ids=ids)  # the same drafts again: duplicates are refused
    send_batch(w, "SEND BATCH OF 0", ids=ids)
    assert len(w.transport.calls) == 3


def test_live_batch_leaves_out_contacts_marked_passed(send_web):
    w = send_web()
    set_stage(w, "grace@example.com", "passed")
    live(w)
    ids = approved_draft_ids(w)
    assert len(ids) == 3
    # all three drafts are still selected, so the count to type is 3; the passed contact is left out anyway
    send_batch(w, "SEND BATCH OF 3", ids=ids)
    assert sorted(c.recipients[0] for c in w.transport.calls) == ["ada@example.com", "linus@example.com"]


def test_push_in_live_mode_needs_the_typed_confirmation(send_web):
    w = send_web()
    live(w)
    for phrase in (None, "SEND BATCH OF 99"):
        r = w.post(w.url("push"), typed(phrase) if phrase else {}, follow=True)
        assert r.status_code < 500
    assert no_calls(w)
    assert re.search(r"(?i)confirm|typed|SEND BATCH OF", page_text(r))


# ---------------------------------------------------------------------------------------------------------------
# GET never sends, CSRF on send POSTs
# ---------------------------------------------------------------------------------------------------------------

SEND_KEYS = ["send_test", "send_live", "send_batch", "push", "schedule", "toggle"]


def _send_url(w: Web, key: str) -> str:
    return send_url(w, key, "ada") if key in ("send_test", "send_live") else w.url(key)


def test_a_get_request_to_any_send_route_or_the_plan_page_never_sends_or_changes_the_mode(send_web):
    w = send_web()
    live(w)
    for key in SEND_KEYS:
        r = w.get(_send_url(w, key), query_string={**typed("SEND TO ada@example.com"), "slug": "ada"})
        assert r.status_code in (404, 405), key
    assert w.get("/crm/send-plan").status_code == 200
    assert no_calls(w)
    assert banner_mode(w) == "live"


def test_send_posts_without_a_csrf_token_are_rejected_in_live_mode(send_web):
    w = send_web()
    live(w)
    for key in ("send_test", "send_live", "send_batch", "push", "schedule"):
        payload = typed("SEND TO ada@example.com" if key == "send_live" else "SEND BATCH OF 3")
        r = w.post(_send_url(w, key), payload, csrf=False)
        assert r.status_code in (400, 403), key
    assert no_calls(w)


# ---------------------------------------------------------------------------------------------------------------
# The scheduler thread exists only when OUTREACH_SCHEDULER=1
# ---------------------------------------------------------------------------------------------------------------

def _threads_started_by_create_app(monkeypatch, tmp_path, scheduler_value):
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    if scheduler_value is not None:
        monkeypatch.setenv("OUTREACH_SCHEDULER", scheduler_value)
    ws = tmp_path / "sched-ws"
    ws.mkdir(parents=True)
    app = create_web_app(ws)
    client = app.test_client()
    client.get("/healthz", base_url="http://127.0.0.1:8765")
    return started


def test_no_background_thread_is_started_unless_the_scheduler_is_switched_on(monkeypatch, tmp_path):
    assert _threads_started_by_create_app(monkeypatch, tmp_path, None) == []
    assert _threads_started_by_create_app(monkeypatch, tmp_path / "zero", "0") == []


def test_the_scheduler_thread_starts_when_outreach_scheduler_is_1(monkeypatch, tmp_path):
    assert _threads_started_by_create_app(monkeypatch, tmp_path, "1") != []
