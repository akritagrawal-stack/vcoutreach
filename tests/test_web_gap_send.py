"""Mutation-gap tests: the live send gates, the daily cap, the single-flight lock, ambiguous transport outcomes,
the HTML alternative and the deck attachment (SPEC_V3 section 2, CONTRACT.md "Sending", README "TEST mode and
LIVE mode", docs/web-app.md "How a message gets sent").

Written from the specs only. The Gmail client is the injected fake send_transport; the workspace database is
touched only through the documented schema (vcoutreach/web/schema.sql). No network, no real sends.
No dash characters appear in this file.
"""
from __future__ import annotations

import re
import threading
import time
from email.message import EmailMessage

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures: web_env_isolation, make_web, web, send_web, ...)
from web_helpers import (
    ADA, GRACE, LINUS, SENDER_EMAIL, TEST_INBOX, RecordedCall, Web, banner_mode, body, contact_ref, go_live,
    go_test, journal_lines, page_text, read_draft_file, read_env, send_live, send_test, stage_of, typed,
)
from web_gap_helpers import (
    append_journal_line, draft_row_id, draft_status, drop_env, insert_attempt, live_journal_rows,
    lock_held_elsewhere, new_text, partner_row_id, push_all, push_drafts, ready_web, sent_to, set_env, sql, sql_exec,
)

ADA_MAIL = "ada@example.com"
GRACE_MAIL = "grace@example.com"


def live(w: Web) -> None:
    go_live(w)
    assert banner_mode(w) == "live", "typing GO LIVE should arm live mode"


# ---------------------------------------------------------------------------------------------------------------
# T-LIVE-GATES: each gate of the live push is independent, and a good contact beside it is sent exactly once
# ---------------------------------------------------------------------------------------------------------------

def _break_unverified(w: Web) -> None:
    sql_exec(w, "UPDATE partners SET email_verification_status=NULL, email_verified_at=NULL WHERE slug='grace'")


def _break_invalid(w: Web) -> None:
    sql_exec(w, "UPDATE partners SET email_verification_status='invalid' WHERE slug='grace'")


def _break_unapproved(w: Web) -> None:
    sql_exec(w, "UPDATE drafts SET status='pending', approved_at=NULL WHERE partner_id=?", (partner_row_id(w, "grace"),))


def _break_edited_legacy(w: Web) -> None:
    pid = partner_row_id(w, "grace")
    assert draft_status(w, "grace") == "approved", "precondition: the draft was approved"
    hook = sql(w, "SELECT hook FROM drafts WHERE partner_id=?", (pid,))[0][0]
    w.post(f"/partners/{pid}/update", {"subject": "A different subject for Grace", "hook": hook}, follow=True)
    assert draft_status(w, "grace") == "pending", "editing an approved draft must withdraw the approval"


def _break_edited_editor(w: Web) -> None:
    assert draft_status(w, "grace") == "approved", "precondition: the draft was approved"
    draft = read_draft_file(w.ws, "grace")
    w.post(w.url("draft_save", slug="grace"), {
        "to": draft.to, "subject": draft.subject, "lane": "first_touch", "in_reply_to": "", "references": "",
        "body": draft.body.replace("Hi Grace,", "Hello Grace,")}, follow=True)
    assert draft_status(w, "grace") == "pending", "editing an approved draft must withdraw the approval"


def _break_suppressed(w: Web) -> None:
    sql_exec(w, "UPDATE partners SET dropped_reason='asked to be removed' WHERE slug='grace'")


def _break_bounced(w: Web) -> None:
    sql_exec(w, "INSERT INTO replies(partner_id, gmail_thread_id, gmail_message_id, from_email, subject, body_text, "
                "received_at, label, user_reviewed) VALUES(?,?,?,?,?,?,?,?,0)",
             (partner_row_id(w, "grace"), "thread-gap-1", "msg-gap-1", "mailer-daemon@example.com",
              "Delivery Status Notification (Failure)", "The address grace@example.com was not found.",
              time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "bounce"))


GATES = {
    "unverified-email": _break_unverified,
    "invalid-email": _break_invalid,
    "unapproved-draft": _break_unapproved,
    "edited-after-approval-legacy-form": _break_edited_legacy,
    "edited-after-approval-editor": _break_edited_editor,
    "suppressed-contact": _break_suppressed,
    "bounced-contact": _break_bounced,
}


@pytest.mark.parametrize("route", ["send_batch", "push"])
@pytest.mark.parametrize("gate", sorted(GATES))
def test_a_live_push_skips_a_contact_that_fails_one_gate_and_sends_the_good_one_exactly_once(make_web, gate, route):
    w = ready_web(make_web, [ADA, GRACE])
    GATES[gate](w)
    live(w)
    push_drafts(w, ["ada", "grace"], route)
    assert sent_to(w) == [ADA_MAIL], f"only the contact that passes every gate may be sent ({gate})"
    assert [row[0] for row in live_journal_rows(w) if row[3] in ("sent", "sending", "uncertain")] == [ADA_MAIL]
    assert stage_of(w, GRACE_MAIL) != "contacted", "a refused contact is not marked contacted"
    assert draft_status(w, "grace") != "live_sent"
    assert not [e for e in journal_lines(w.ws) if e.get("recipient") == GRACE_MAIL]


@pytest.mark.parametrize("gate", sorted(GATES))
def test_a_single_live_send_is_refused_for_a_contact_that_fails_one_gate(make_web, gate):
    w = ready_web(make_web, [GRACE])
    GATES[gate](w)
    live(w)
    r = send_live(w, "grace", "SEND TO grace@example.com")
    assert r.status_code < 500
    assert w.transport.calls == [], gate
    assert not live_journal_rows(w)
    assert new_text("", page_text(r)).strip()


def test_the_same_pair_pushed_twice_sends_the_good_contact_once(make_web):
    w = ready_web(make_web, [ADA, GRACE])
    live(w)
    push_drafts(w, ["ada", "grace"])
    push_drafts(w, ["ada", "grace"])
    assert sorted(sent_to(w)) == [ADA_MAIL, GRACE_MAIL], "each eligible contact is sent exactly once"


def test_a_legacy_draft_that_fails_lint_is_not_sent_hard_wrapped(make_web):
    """The legacy review path keeps the body in the drafts table. A hard wrapped paragraph there is a lint error
    and must not go out, even if someone marked the draft approved."""
    w = ready_web(make_web, [ADA, GRACE])
    wrapped = ("Hi Grace,\n\nWRAPMARKER I came across your talk on making software\nreadable and I found it "
               "interesting.\n\nWe build a tool that turns a spreadsheet of contacts into plain drafts a founder "
               "can actually send.\n\nSam Rivera\nFounder, Lumenlist\nhttps://lumenlist.example.com")
    sql_exec(w, "UPDATE drafts SET hook=?, status='approved' WHERE partner_id=?", (wrapped, partner_row_id(w, "grace")))
    live(w)
    push_drafts(w, ["ada", "grace"])
    assert ADA_MAIL in sent_to(w)
    for call in w.transport.calls:
        assert "WRAPMARKER" not in call.text, "a hard wrapped body must never be sent"
    go_test(w)
    send_test(w, "grace")
    for call in w.transport.calls:
        assert "WRAPMARKER" not in call.text, "a hard wrapped body must never be sent, not even as a test"


# ---------------------------------------------------------------------------------------------------------------
# T-CAP-COUNT: the daily cap counts every kind of attempt
# ---------------------------------------------------------------------------------------------------------------

def _cap_web(make_web, **kw):
    return ready_web(make_web, [ADA, GRACE], env={"DAILY_SEND_CAP": "1"}, **kw)


def test_without_earlier_attempts_a_cap_of_one_lets_the_first_live_send_through(make_web):
    """Control for the cap tests below."""
    w = _cap_web(make_web)
    live(w)
    send_live(w, "ada", "SEND TO ada@example.com")
    assert sent_to(w) == [ADA_MAIL]


@pytest.mark.parametrize("state", ["uncertain", "sending", "sent"])
def test_an_earlier_live_attempt_in_the_database_counts_toward_the_cap(make_web, state):
    w = _cap_web(make_web)
    insert_attempt(w, "earlier@example.com", state, mode="live")
    live(w)
    r = send_live(w, "ada", "SEND TO ada@example.com")
    assert w.transport.calls == [], f"an attempt left {state!r} today uses up a cap of 1"
    assert "cap" in page_text(r).lower()


def test_an_earlier_test_attempt_in_the_database_counts_toward_the_cap(make_web):
    w = _cap_web(make_web)
    insert_attempt(w, "earlier@example.com", "sent", mode="test")
    live(w)
    send_live(w, "ada", "SEND TO ada@example.com")
    assert w.transport.calls == []


@pytest.mark.parametrize("mode", ["live", "test"])
def test_a_send_recorded_only_in_the_shared_journal_file_counts_toward_the_cap(make_web, mode):
    w = _cap_web(make_web)
    append_journal_line(w, "earlier@example.com", mode)
    live(w)
    r = send_live(w, "ada", "SEND TO ada@example.com")
    assert w.transport.calls == []
    assert "cap" in page_text(r).lower()


@pytest.mark.parametrize("route", ["send_batch", "push"])
def test_the_cap_also_stops_a_batch_when_an_attempt_is_already_uncertain(make_web, route):
    w = _cap_web(make_web)
    insert_attempt(w, "earlier@example.com", "uncertain")
    live(w)
    push_drafts(w, ["ada", "grace"], route)
    assert w.transport.calls == []


def test_a_cap_of_two_with_one_earlier_attempt_allows_exactly_one_more(make_web):
    w = ready_web(make_web, [ADA, GRACE, LINUS], env={"DAILY_SEND_CAP": "2"})
    insert_attempt(w, "earlier@example.com", "uncertain")
    live(w)
    push_drafts(w, ["ada", "grace", "linus"])
    assert len(w.transport.calls) == 1


# ---------------------------------------------------------------------------------------------------------------
# T-SENDER: a live send needs a sender address
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["unset", "empty"])
@pytest.mark.parametrize("route", ["single", "send_batch", "push"])
def test_a_live_send_without_a_sender_address_is_refused_before_any_transport_call(make_web, how, route):
    w = ready_web(make_web, [ADA], sender_email=None if how == "unset" else "")
    live(w)
    if route == "single":
        r = send_live(w, "ada", "SEND TO ada@example.com")
    else:
        r = push_drafts(w, ["ada"], route)
    assert r.status_code < 500
    assert w.transport.calls == []
    assert not live_journal_rows(w), "no attempt may be reserved for a send that cannot be made"
    # control: with the address set, the same send goes out
    set_env(w, SENDER_EMAIL=SENDER_EMAIL)
    send_live(w, "ada", "SEND TO ada@example.com")
    assert sent_to(w) == [ADA_MAIL]
    assert SENDER_EMAIL in str(w.transport.calls[0].message["From"])


# ---------------------------------------------------------------------------------------------------------------
# T-LOCK: one push at a time
# ---------------------------------------------------------------------------------------------------------------

def _in_background(fn):
    """Run fn on a helper thread so a request that blocks on the lock cannot hang the test run."""
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


@pytest.mark.parametrize("route", ["send_batch", "push"])
def test_no_message_leaves_while_another_process_holds_the_send_lock(make_web, route):
    w = ready_web(make_web, [ADA, GRACE])
    live(w)
    with lock_held_elsewhere(w.ws / ".vcoutreach" / "send.lock"):
        thread, _box = _in_background(lambda: push_drafts(w, ["ada", "grace"], route))
        thread.join(4.0)
        assert w.transport.calls == [], "a second run must send nothing while the first holds the lock"
        assert not [r for r in live_journal_rows(w) if r[3] == "sent"]
    thread.join(30.0)
    assert not thread.is_alive(), "the request must finish once the lock is released"


@pytest.mark.parametrize("route", ["send_batch", "push"])
def test_a_push_while_the_send_lock_is_held_answers_at_once_and_says_so(make_web, route):
    """SPEC T-LOCK: the second run does nothing and says so. It must not sit and wait for the first."""
    w = ready_web(make_web, [ADA, GRACE])
    live(w)
    with lock_held_elsewhere(w.ws / ".vcoutreach" / "send.lock"):
        thread, box = _in_background(lambda: push_drafts(w, ["ada", "grace"], route))
        thread.join(8.0)
        answered = not thread.is_alive()
        calls_during_hold = list(w.transport.calls)
    thread.join(30.0)
    assert answered, "the push waited for the lock instead of answering that another run is in progress"
    assert calls_during_hold == []
    assert "error" not in box and box["result"].status_code < 500
    assert new_text("", page_text(box["result"])).strip()
    assert not live_journal_rows(w), "a run that did nothing must not reserve attempts"


@pytest.mark.parametrize("route", ["send_batch", "push"])
def test_after_the_send_lock_is_released_a_push_runs_normally_and_sends_each_contact_once(make_web, route):
    w = ready_web(make_web, [ADA, GRACE])
    live(w)
    with lock_held_elsewhere(w.ws / ".vcoutreach" / "send.lock"):
        thread, _box = _in_background(lambda: push_drafts(w, ["ada", "grace"], route))
        thread.join(4.0)
    thread.join(30.0)
    push_drafts(w, ["ada", "grace"], route)
    assert sorted(sent_to(w)) == [ADA_MAIL, GRACE_MAIL]


def test_a_single_live_send_sends_nothing_while_the_lock_is_held(make_web):
    w = ready_web(make_web, [ADA])
    live(w)
    with lock_held_elsewhere(w.ws / ".vcoutreach" / "send.lock"):
        thread, _box = _in_background(lambda: send_live(w, "ada", "SEND TO ada@example.com"))
        thread.join(4.0)
        assert w.transport.calls == []
    thread.join(30.0)
    send_live(w, "ada", "SEND TO ada@example.com")
    assert sent_to(w) == [ADA_MAIL]


# ---------------------------------------------------------------------------------------------------------------
# T-RECEIPT: an empty receipt is not a sent mail, and a delivered-then-raised attempt is never repeated
# ---------------------------------------------------------------------------------------------------------------

def _record(w: Web, args, kwargs):
    values = tuple(args) + tuple(kwargs.values())
    message = next(v for v in values if isinstance(v, EmailMessage))
    recipient = next((v for v in values if isinstance(v, str)), None)
    w.transport.calls.append(RecordedCall(message, recipient))


def _receipt(message_id):
    from vcoutreach.send.gate import SendReceipt

    return SendReceipt(message_id=message_id)


@pytest.mark.parametrize("kind", ["empty-id", "none"])
def test_a_transport_that_returns_no_message_id_is_not_recorded_as_sent(make_web, kind):
    w = ready_web(make_web, [ADA, GRACE])

    def flaky(args, kwargs):
        _record(w, args, kwargs)
        if kind == "none":
            return None
        try:
            return _receipt("")
        except Exception:
            return None

    w.transport._send = flaky
    live(w)
    push_drafts(w, ["ada"])
    assert len(w.transport.calls) == 1, "the transport was reached once"
    states = [row[3] for row in live_journal_rows(w)]
    assert "sent" not in states, f"a send without a receipt must not be recorded as sent: {states}"
    assert draft_status(w, "ada") != "live_sent"
    assert stage_of(w, ADA_MAIL) == "prospect"
    assert not [e for e in journal_lines(w.ws) if e.get("recipient") == ADA_MAIL and e.get("mode") == "live"
                and e.get("message_id")]


def test_a_transport_that_delivers_and_then_raises_leaves_the_attempt_uncertain_and_is_never_retried(make_web):
    from vcoutreach.types import OutreachError

    w = ready_web(make_web, [ADA])

    def deliver_then_raise(args, kwargs):
        _record(w, args, kwargs)
        raise OutreachError("connection reset after the server accepted the message")

    w.transport._send = deliver_then_raise
    live(w)
    push_drafts(w, ["ada"])
    assert sent_to(w) == [ADA_MAIL]
    assert [row[3] for row in live_journal_rows(w)] == ["uncertain"], "the outcome is unknown, so it is uncertain"
    assert stage_of(w, ADA_MAIL) == "prospect"
    # nothing may send it again: not another batch, not /push, not a single send, not after the transport heals
    w.transport._send = lambda a, k: (_record(w, a, k), _receipt("<fake-late@mail.test>"))[1]
    push_drafts(w, ["ada"])
    push_all(w)
    send_live(w, "ada", "SEND TO ada@example.com")
    assert sent_to(w) == [ADA_MAIL], "an uncertain attempt is never retried automatically"
    assert [row[3] for row in live_journal_rows(w)] == ["uncertain"]


def test_a_plain_transport_failure_before_delivery_is_not_marked_sent(make_web):
    w = ready_web(make_web, [ADA])
    w.transport.fail_with("simulated outage before anything left")
    live(w)
    push_drafts(w, ["ada"])
    assert w.transport.calls == []
    assert "sent" not in [row[3] for row in live_journal_rows(w)]
    assert stage_of(w, ADA_MAIL) == "prospect"


# ---------------------------------------------------------------------------------------------------------------
# T-HTML: the HTML alternative escapes the body
# ---------------------------------------------------------------------------------------------------------------

def _html_of(call: RecordedCall) -> str:
    part = call.message.get_body(preferencelist=("html",))
    assert part is not None, "the message must carry an HTML alternative"
    return part.get_content()


@pytest.mark.parametrize("mode", ["test", "live"])
def test_the_html_alternative_escapes_ampersands_tags_and_quotes_from_the_body(make_web, mode):
    from test_enrichment_facts_reach_drafts import fact, put_enrichment

    w = ready_web(make_web, [ADA])
    put_enrichment(w, "ada", [fact(
        'your essay on how small teams ship weekly, and the R&D part, the <b>bold</b> idea and "quoted words" stood out.',
        url="https://example.com/essay")])
    draft = read_draft_file(w.ws, "ada")
    body_text = draft.body.replace(
        "and I found it interesting.",
        'and the R&D part, the <b>bold</b> idea and "quoted words" stood out.')
    assert body_text != draft.body
    w.post(w.url("draft_save", slug="ada"), {
        "to": draft.to, "subject": draft.subject, "lane": "first_touch", "in_reply_to": "", "references": "",
        "body": body_text}, follow=True)
    saved = read_draft_file(w.ws, "ada")
    assert "R&D" in saved.body and "<b>bold</b>" in saved.body, "precondition: the edit was saved as typed"
    assert saved.status == "ok", "precondition: the grounded edit is ready to send"
    if mode == "test":
        response = send_test(w, "ada")
    else:
        w.post(w.url("approve", id=partner_row_id(w, "ada")), {}, follow=True)
        live(w)
        response = send_live(w, "ada", "SEND TO ada@example.com")
    assert len(w.transport.calls) == 1, page_text(response)
    call = w.transport.calls[0]
    html = _html_of(call)
    assert "R&amp;D" in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "<b>" not in html and "</b>" not in html, "no raw tag from the body may survive"
    assert re.search(r"(&quot;|&#34;|&#x22;)quoted words(&quot;|&#34;|&#x22;)", html), html[:600]
    assert '"quoted words"' not in html
    assert "R&D" in call.text and "<b>bold</b>" in call.text, "the plain text part carries the words as written"


# ---------------------------------------------------------------------------------------------------------------
# T-DECK: only a deck inside the workspace is ever attached
# ---------------------------------------------------------------------------------------------------------------

DECK_BYTES = b"%PDF-1.4 OUTSIDE-DECK-SECRET-0123456789 %%EOF"
INSIDE_BYTES = b"%PDF-1.4 INSIDE-DECK-0123456789 %%EOF"


def attachments_of(call: RecordedCall):
    return [(part.get_filename(), part.get_payload(decode=True)) for part in call.message.iter_attachments()]


def whole_message_bytes(call: RecordedCall) -> bytes:
    return call.message.as_bytes()


def _send_with_deck(make_web, deck_setting: str, mode: str):
    w = ready_web(make_web, [ADA])
    set_env(w, PITCH_DECK_PATH=deck_setting)
    if mode == "test":
        send_test(w, "ada")
    else:
        live(w)
        send_live(w, "ada", "SEND TO ada@example.com")
    return w


@pytest.mark.parametrize("mode", ["test", "live"])
def test_a_deck_inside_the_workspace_is_attached(make_web, tmp_path, mode):
    inside = tmp_path / "ws-with-deck"
    inside.mkdir()
    w = ready_web(make_web, [ADA])
    deck = w.ws / "outputs" / "deck.pdf"
    deck.parent.mkdir(parents=True, exist_ok=True)
    deck.write_bytes(INSIDE_BYTES)
    set_env(w, PITCH_DECK_PATH=str(deck))
    if mode == "test":
        send_test(w, "ada")
    else:
        live(w)
        send_live(w, "ada", "SEND TO ada@example.com")
    assert len(w.transport.calls) == 1
    payloads = [p for _name, p in attachments_of(w.transport.calls[0])]
    assert INSIDE_BYTES in payloads, "the deck inside the workspace should be attached to the message"


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("how", ["absolute", "relative-escape", "symlink"])
def test_a_deck_outside_the_workspace_is_never_attached(make_web, tmp_path, mode, how):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    secret_file = outside / "private-deck.pdf"
    secret_file.write_bytes(DECK_BYTES)
    w = ready_web(make_web, [ADA])
    if how == "absolute":
        setting = str(secret_file)
    elif how == "relative-escape":
        setting = "../elsewhere/private-deck.pdf"
    else:
        link = w.ws / "outputs" / "innocent-deck.pdf"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(secret_file)
        setting = str(link)
    set_env(w, PITCH_DECK_PATH=setting)
    if mode == "test":
        send_test(w, "ada")
    else:
        live(w)
        send_live(w, "ada", "SEND TO ada@example.com")
    for call in w.transport.calls:
        assert not [p for _n, p in attachments_of(call) if p and b"OUTSIDE-DECK-SECRET" in p]
        raw = whole_message_bytes(call)
        import base64

        assert b"OUTSIDE-DECK-SECRET" not in raw
        assert base64.b64encode(DECK_BYTES) not in raw.replace(b"\n", b"").replace(b"\r", b"")
