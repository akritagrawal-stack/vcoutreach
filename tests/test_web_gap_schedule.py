"""Mutation-gap tests: scheduling a send and the scheduler that runs it (SPEC_V3 section 2, CONTRACT.md "Sending"
and "Final schema and operation notes", README "TEST mode and LIVE mode").

Written from the specs only. Scheduled rows are made through POST /schedule-send; the clock is moved by editing
scheduled_for in the documented scheduled_sends table. The scheduler thread is the one create_app starts when
OUTREACH_SCHEDULER=1; its loop body is run on a helper thread with the waits shortened (see SchedulerHarness).
The Gmail client is the injected fake send_transport. No network, no real sends.
No dash characters appear in this file.
"""
from __future__ import annotations

import datetime as dt

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures: web_env_isolation, make_web, web, send_web, ...)
from web_helpers import ADA, GRACE, TEST_INBOX, Web, banner_mode, go_live, go_test, page_text, stage_of
from web_gap_helpers import (
    SchedulerHarness, draft_row_id, live_journal_rows, ready_web, schedule_form, scheduled_rows, sent_to, sql,
    sql_exec, utc_iso,
)

LIVE_ONE = "SCHEDULE LIVE FOR 1"


def schedule(w: Web, slugs, mode: str, phrase: str, in_seconds: float = 300):
    when = dt.datetime.now() + dt.timedelta(seconds=in_seconds)
    ids = [draft_row_id(w, s) for s in slugs]
    return w.post("/schedule-send", schedule_form(ids, when, mode, phrase), follow=True)


def make_due(w: Web, row_id: int, seconds_ago: int = 120) -> None:
    sql_exec(w, "UPDATE scheduled_sends SET scheduled_for=? WHERE id=?", (utc_iso(-seconds_ago), row_id))


def make_future(w: Web, row_id: int, seconds_ahead: int = 900) -> None:
    sql_exec(w, "UPDATE scheduled_sends SET scheduled_for=? WHERE id=?", (utc_iso(seconds_ahead), row_id))


def row_by_id(w: Web, row_id: int):
    return next(r for r in scheduled_rows(w) if r["id"] == row_id)


def live(w: Web) -> None:
    go_live(w)
    assert banner_mode(w) == "live"


# ---------------------------------------------------------------------------------------------------------------
# T-SCHED-FORM: the live phrase, the LIVE precondition and the 60 second minimum
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", [
    "", "schedule live for 1", "SCHEDULE LIVE FOR 2", "SCHEDULE LIVE FOR 0", "SCHEDULE LIVE FOR", "SCHEDULE LIVE 1",
    "SCHEDULE LIVE FOR 1 please", " SCHEDULE LIVE FOR 1x", "GO LIVE", "SEND BATCH OF 1",
], ids=["empty", "lower-case", "wrong-count-2", "zero", "no-count", "missing-for", "trailing-words",
        "trailing-char", "go-live", "batch-phrase"])
def test_a_live_schedule_without_the_exact_phrase_creates_no_row(make_web, phrase):
    w = ready_web(make_web, [ADA, GRACE])
    live(w)
    r = schedule(w, ["ada"], "live", phrase)
    assert r.status_code < 500
    assert scheduled_rows(w) == [], f"phrase {phrase!r} must not schedule a live send"
    assert page_text(r).strip()


def test_the_exact_live_phrase_with_the_right_count_creates_exactly_one_live_row(make_web):
    w = ready_web(make_web, [ADA, GRACE])
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    rows = scheduled_rows(w)
    assert len(rows) == 1
    assert rows[0]["mode"] == "live" and rows[0]["status"] == "pending"
    assert rows[0]["confirmation"] == LIVE_ONE, "the typed phrase is kept with the row"
    assert str(draft_row_id(w, "ada")) in rows[0]["draft_ids"]


def test_the_phrase_must_name_the_number_of_selected_drafts(make_web):
    w = ready_web(make_web, [ADA, GRACE])
    live(w)
    schedule(w, ["ada", "grace"], "live", LIVE_ONE)
    assert scheduled_rows(w) == [], "one selected is not two"
    schedule(w, ["ada", "grace"], "live", "SCHEDULE LIVE FOR 2")
    assert len(scheduled_rows(w)) == 1


def test_the_live_phrase_is_refused_while_the_workspace_is_still_in_test_mode(make_web):
    w = ready_web(make_web, [ADA, GRACE])
    assert banner_mode(w) == "test"
    r = schedule(w, ["ada"], "live", LIVE_ONE)
    assert r.status_code < 500
    assert scheduled_rows(w) == [], "the workspace must be armed LIVE first"
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    assert len(scheduled_rows(w)) == 1, "control: the same post works once the workspace is LIVE"


def test_a_test_mode_schedule_needs_no_phrase(make_web):
    w = ready_web(make_web, [ADA])
    schedule(w, ["ada"], "test", "")
    rows = scheduled_rows(w)
    assert len(rows) == 1 and rows[0]["mode"] == "test"


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("seconds", [-86400, -600, -1, 0, 20, 50], ids=lambda s: f"{s}s")
def test_a_run_time_in_the_past_or_less_than_60_seconds_ahead_is_refused(make_web, mode, seconds):
    w = ready_web(make_web, [ADA])
    if mode == "live":
        live(w)
    r = schedule(w, ["ada"], mode, LIVE_ONE if mode == "live" else "", in_seconds=seconds)
    assert r.status_code < 500
    assert scheduled_rows(w) == [], f"a run time {seconds} seconds from now must not be accepted"


@pytest.mark.parametrize("mode", ["test", "live"])
@pytest.mark.parametrize("seconds", [75, 120, 3600, 86400 * 3], ids=lambda s: f"{s}s")
def test_a_run_time_61_seconds_or_more_ahead_is_accepted(make_web, mode, seconds):
    w = ready_web(make_web, [ADA])
    if mode == "live":
        live(w)
    schedule(w, ["ada"], mode, LIVE_ONE if mode == "live" else "", in_seconds=seconds)
    rows = scheduled_rows(w)
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    when = dt.datetime.fromisoformat(rows[0]["scheduled_for"])
    ahead = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
    assert seconds - 5 <= ahead <= seconds + 5, f"stored time is {ahead:.0f}s ahead, asked for {seconds}s"


def test_a_schedule_with_an_unreadable_date_or_unknown_mode_creates_no_row(make_web):
    w = ready_web(make_web, [ADA])
    ids = [str(draft_row_id(w, "ada"))]
    when = dt.datetime.now() + dt.timedelta(minutes=10)
    good = {"draft_ids": ids, "sched_date": when.strftime("%Y-%m-%d"), "sched_time": when.strftime("%H:%M"),
            "sched_mode": "test", "confirm_text": ""}
    for patch in ({"sched_date": "not-a-date"}, {"sched_time": "99:99"}, {"sched_mode": "banana"}, {"draft_ids": []}):
        r = w.post("/schedule-send", {**good, **patch}, follow=True)
        assert r.status_code < 500
    assert scheduled_rows(w) == []
    w.post("/schedule-send", good, follow=True)
    assert len(scheduled_rows(w)) == 1, "control: the unchanged form works"


# ---------------------------------------------------------------------------------------------------------------
# T-SCHED-RUN: what the scheduler may and may not do
# ---------------------------------------------------------------------------------------------------------------

def build(make_web, **kw):
    return lambda: ready_web(make_web, [ADA, GRACE], **kw)


def test_the_scheduler_sends_a_due_test_row_once_and_to_the_test_inbox_only(make_web, monkeypatch):
    """Control for the cases below: it proves that the harness really drives the scheduler."""
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    assert h.captured, "OUTREACH_SCHEDULER=1 should have started the scheduler thread"
    schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    make_due(w, row)
    assert h.run_ticks()
    assert [c.recipients for c in w.transport.calls] == [[TEST_INBOX]]
    assert row_by_id(w, row)["status"] == "completed"
    assert stage_of(w, "ada@example.com") == "prospect", "a test run does not move the contact"


def test_a_row_that_is_not_due_yet_is_not_run_even_with_the_switch_on(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    make_future(w, row, 900)
    assert h.run_ticks()
    assert w.transport.calls == []
    assert row_by_id(w, row)["status"] == "pending"
    # and a live row that is not due is not run either
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    live_row = [r for r in scheduled_rows(w) if r["mode"] == "live"][0]["id"]
    make_future(w, live_row, 900)
    assert h.run_ticks()
    assert w.transport.calls == []
    assert row_by_id(w, live_row)["status"] == "pending"


@pytest.mark.parametrize("kind", ["test", "live"])
def test_a_due_row_is_not_run_when_the_scheduler_switch_is_off(make_web, monkeypatch, kind):
    """The scheduler runs only with OUTREACH_SCHEDULER=1: without it a due row just waits."""
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    if kind == "live":
        live(w)
        schedule(w, ["ada"], "live", LIVE_ONE)
    else:
        schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    make_due(w, row)
    monkeypatch.delenv("OUTREACH_SCHEDULER", raising=False)
    h.run_ticks()
    assert w.transport.calls == []
    assert row_by_id(w, row)["status"] == "pending"


def test_with_the_switch_never_set_no_scheduler_thread_exists_and_a_due_row_waits(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web), switch=None)
    w = h.web
    schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    make_due(w, row)
    assert h.captured == []
    assert h.run_ticks()
    assert w.transport.calls == []
    assert row_by_id(w, row)["status"] == "pending"


def test_a_due_live_row_with_its_stored_confirmation_is_sent_live_once(make_web, monkeypatch):
    """Control: all the conditions the cases below remove."""
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    row = scheduled_rows(w)[0]["id"]
    make_due(w, row)
    assert h.run_ticks()
    assert sent_to(w) == ["ada@example.com"]
    assert row_by_id(w, row)["status"] == "completed"
    assert [r[0] for r in live_journal_rows(w)] == ["ada@example.com"]


@pytest.mark.parametrize("stored", ["", None, "SCHEDULE LIVE FOR 2", "GO LIVE"], ids=["empty", "null", "wrong", "other"])
def test_a_due_live_row_without_its_stored_confirmation_is_marked_failed_and_sends_nothing(make_web, monkeypatch, stored):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    row = scheduled_rows(w)[0]["id"]
    sql_exec(w, "UPDATE scheduled_sends SET confirmation=? WHERE id=?", (stored, row))
    make_due(w, row)
    assert h.run_ticks()
    assert w.transport.calls == [], "no typed confirmation, no live send"
    assert row_by_id(w, row)["status"] == "failed"
    assert not live_journal_rows(w)


def test_a_due_live_row_is_not_sent_live_after_the_workspace_went_back_to_test(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    row = scheduled_rows(w)[0]["id"]
    go_test(w)
    assert banner_mode(w) == "test"
    make_due(w, row)
    assert h.run_ticks()
    assert "ada@example.com" not in sent_to(w), "a live row must never reach the real recipient from a TEST workspace"
    assert not live_journal_rows(w)
    assert row_by_id(w, row)["status"] != "completed" or sent_to(w) in ([], [TEST_INBOX])


def test_a_row_is_claimed_once_however_often_the_scheduler_looks(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    make_due(w, row)
    for _ in range(3):
        assert h.run_ticks(sleeps_allowed=4)
    assert len(w.transport.calls) == 1
    assert row_by_id(w, row)["status"] == "completed"


def test_a_live_row_is_sent_once_however_often_the_scheduler_looks(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    row = scheduled_rows(w)[0]["id"]
    make_due(w, row)
    for _ in range(3):
        assert h.run_ticks(sleeps_allowed=4)
    assert sent_to(w) == ["ada@example.com"]


@pytest.mark.parametrize("status", ["running", "completed", "failed", "cancelled"])
def test_a_row_that_is_already_running_or_finished_is_not_claimed_again(make_web, monkeypatch, status):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    sql_exec(w, "UPDATE scheduled_sends SET status=? WHERE id=?", (status, row))
    make_due(w, row)
    assert h.run_ticks(sleeps_allowed=4)
    assert w.transport.calls == []
    assert row_by_id(w, row)["status"] == status


def test_a_cancelled_row_is_never_run(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    r = w.post(f"/scheduled/{row}/cancel", {}, follow=True)
    assert r.status_code < 500
    make_due(w, row)
    assert h.run_ticks()
    assert w.transport.calls == []
    assert row_by_id(w, row)["status"] != "completed"


def test_a_row_with_an_unknown_mode_is_never_executed(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    live(w)
    schedule(w, ["ada"], "live", LIVE_ONE)
    row = scheduled_rows(w)[0]["id"]
    sql_exec(w, "UPDATE scheduled_sends SET mode='banana' WHERE id=?", (row,))
    make_due(w, row)
    assert h.run_ticks()
    assert w.transport.calls == []
    assert row_by_id(w, row)["status"] != "completed"


def test_a_due_test_row_never_reaches_a_real_recipient_even_if_the_workspace_is_live(make_web, monkeypatch):
    h = SchedulerHarness(monkeypatch, build(make_web))
    w = h.web
    schedule(w, ["ada"], "test", "")
    row = scheduled_rows(w)[0]["id"]
    live(w)
    make_due(w, row)
    assert h.run_ticks()
    assert "ada@example.com" not in sent_to(w), "a row scheduled as TEST is a test send, whatever the mode is later"
