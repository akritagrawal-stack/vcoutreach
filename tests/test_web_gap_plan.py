"""Mutation-gap tests: the read-only send plan at GET /crm/send-plan (MUTATION_REPORT T-PLAN, SPEC_V3, CONTRACT.md).

Written from the specs only. The plan is read through the page a person sees; each contact has a Result column
(Planned or Held) and a Delivery target column. "Hides" means the contact is not presented as Planned and its
address is not offered as a delivery target. Nothing here sends mail. No dash characters appear in this file.
"""
from __future__ import annotations

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures)
from web_helpers import ADA, GRACE, LINUS, TEST_INBOX, banner_mode, go_live, set_stage
from web_gap_helpers import BEA, CYD, column, insert_attempt, plan_rows, ready_web, sql_exec

CONTACTS = [ADA, GRACE, LINUS, BEA, CYD]


def by_contact(w) -> dict:
    return {column(r, "contact"): r for r in plan_rows(w)}


def planned(w) -> list:
    return sorted(c for c, r in by_contact(w).items() if column(r, "result").strip().lower() == "planned")


def targets(w) -> list:
    return [column(r, "target") for r in plan_rows(w)]


# ---------------------------------------------------------------------------------------------------------------
# D07, D08: passed, hold and suppressed contacts
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["passed", "hold"])
@pytest.mark.parametrize("live", [False, True], ids=["test-mode", "live-mode"])
def test_the_plan_leaves_out_passed_and_hold_contacts(make_web, stage, live):
    w = ready_web(make_web, [ADA, GRACE, LINUS])
    assert planned(w) == ["ada", "grace", "linus"], "control: all three are eligible before the stage change"
    set_stage(w, "grace@example.com", stage)
    if live:
        go_live(w)
        assert banner_mode(w) == "live"
    assert planned(w) == ["ada", "linus"], f"a contact marked {stage} must not be planned"
    row = by_contact(w)["grace"]
    assert column(row, "result").strip().lower() != "planned"
    assert "grace@example.com" not in " ".join(targets(w)), "its address must not be offered as a delivery target"


@pytest.mark.parametrize("live", [False, True], ids=["test-mode", "live-mode"])
def test_the_plan_leaves_out_a_suppressed_contact(make_web, live):
    w = ready_web(make_web, [ADA, GRACE])
    if live:
        go_live(w)
    assert planned(w) == ["ada", "grace"], "control: both are eligible first"
    sql_exec(w, "UPDATE partners SET dropped_reason='asked to be removed' WHERE slug='grace'")
    assert planned(w) == ["ada"]
    assert "grace@example.com" not in " ".join(targets(w))


# ---------------------------------------------------------------------------------------------------------------
# D09, D10: unapproved and unverified contacts in LIVE mode
# ---------------------------------------------------------------------------------------------------------------

def test_the_live_plan_leaves_out_unapproved_drafts(make_web):
    w = ready_web(make_web, [ADA, GRACE, LINUS], approve=["ada", "linus"])
    go_live(w)
    assert banner_mode(w) == "live"
    got = planned(w)
    assert "grace" not in got, "a draft nobody approved must not be planned for a real send"
    assert got == ["ada", "linus"], "control: the approved and verified ones are planned"
    assert "grace@example.com" not in " ".join(targets(w))


def test_the_live_plan_leaves_out_unverified_addresses(make_web):
    w = ready_web(make_web, [ADA, GRACE, LINUS], verify=["ada@example.com", "linus@example.com"])
    go_live(w)
    assert banner_mode(w) == "live"
    got = planned(w)
    assert "grace" not in got, "an address that was never verified must not be planned for a real send"
    assert got == ["ada", "linus"]
    assert "grace@example.com" not in " ".join(targets(w))


def test_the_live_plan_with_a_mix_lists_exactly_the_approved_verified_unblocked_contacts(make_web):
    w = ready_web(make_web, CONTACTS, approve=["ada", "grace", "linus", "bea"],
                  verify=["ada@example.com", "grace@example.com", "bea@example.com", "cyd@example.com"])
    set_stage(w, "grace@example.com", "passed")
    go_live(w)
    # ada: ok. grace: passed. linus: not verified. bea: ok. cyd: not approved.
    assert planned(w) == ["ada", "bea"]


# ---------------------------------------------------------------------------------------------------------------
# D11: the daily cap
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("live", [False, True], ids=["test-mode", "live-mode"])
@pytest.mark.parametrize("cap", [1, 2, 3])
def test_the_plan_applies_the_daily_cap(make_web, cap, live):
    w = ready_web(make_web, CONTACTS, env={"DAILY_SEND_CAP": str(cap)})
    if live:
        go_live(w)
    got = planned(w)
    assert len(got) == cap, f"cap {cap}: {got} planned"
    held = [r for r in plan_rows(w) if column(r, "result").strip().lower() != "planned"]
    assert len(held) == len(CONTACTS) - cap
    assert any("cap" in column(r, "reason").lower() for r in held), "the page says why they wait"


def test_the_plan_is_not_capped_below_the_limit(make_web):
    w = ready_web(make_web, CONTACTS, env={"DAILY_SEND_CAP": "5"})
    assert len(planned(w)) == 5


@pytest.mark.parametrize("state", ["sent", "uncertain"])
def test_the_plan_counts_earlier_attempts_today_against_the_cap(make_web, state):
    w = ready_web(make_web, [ADA, GRACE, LINUS, BEA], env={"DAILY_SEND_CAP": "3"})
    go_live(w)
    assert len(planned(w)) == 3, "control: with no earlier attempt, three are planned"
    insert_attempt(w, "earlier@example.com", state)
    insert_attempt(w, "earlier2@example.com", state)
    assert len(planned(w)) == 1, "two attempts already used two of the three"


# ---------------------------------------------------------------------------------------------------------------
# D13: TEST mode shows the test inbox, never the contact
# ---------------------------------------------------------------------------------------------------------------

def test_the_test_mode_plan_shows_the_test_inbox_not_the_contact(make_web):
    w = ready_web(make_web, [ADA, GRACE])
    assert banner_mode(w) == "test"
    rows = plan_rows(w)
    assert len(rows) == 2
    for r in rows:
        assert column(r, "target").strip() == TEST_INBOX
        assert "@example.com" not in column(r, "target")
        assert column(r, "mode").strip().upper() == "TEST"


def test_the_live_plan_shows_the_contact_address_as_the_delivery_target(make_web):
    """Control for the TEST mode check above."""
    w = ready_web(make_web, [ADA, GRACE])
    go_live(w)
    got = {column(r, "contact"): column(r, "target").strip() for r in plan_rows(w)}
    assert got == {"ada": "ada@example.com", "grace": "grace@example.com"}
    assert all(column(r, "mode").strip().upper() == "LIVE" for r in plan_rows(w))


def test_the_plan_page_sends_nothing(make_web):
    w = ready_web(make_web, CONTACTS)
    go_live(w)
    for _ in range(2):
        assert w.get("/crm/send-plan").status_code == 200
    assert w.transport.calls == []
    assert "nothing is sent" in w.text("/crm/send-plan").lower()
