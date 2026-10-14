"""SPEC section 22.11 and 22.12: negative limit and the test-mode From fallback (send gate)."""
from __future__ import annotations

import pytest

from email.utils import parseaddr

from vcoutreach.send.gate import SendGateError, plan_sends


def run(kit, drafts, *, mode="test", execute=False, limit=None, transport=None, **env):
    return plan_sends(drafts, kit.send_settings(mode, **env), kit.sender(), kit.journal(),
                      execute=execute, limit=limit, transport=transport)


def three(kit):
    return [kit.draft(contact_id=n, to=f"{n}@example.com") for n in ("alice", "bob", "carol")]


# ----- 22.11 a negative limit is an error; zero is a real limit ----------------------------------------------------------------------

@pytest.mark.parametrize("limit", [-1, -5])
@pytest.mark.parametrize("execute", [False, True], ids=["plan", "execute"])
def test_a_negative_limit_is_a_gate_error_and_nothing_is_sent(kit, limit, execute):
    transport = kit.mail_transport() if execute else None
    with pytest.raises(SendGateError):
        run(kit, three(kit), execute=execute, limit=limit, transport=transport)
    assert not kit.journal_path.exists()
    if transport is not None:
        assert transport.attempts == 0


def test_a_limit_of_zero_is_not_an_error(kit):
    report = run(kit, three(kit), limit=0)
    assert len(report.planned) == 0 and len(report.skipped) == 3


# ----- 22.12 test mode with SENDER_EMAIL unset: the test recipient is the From address ------------------------------------------------

@pytest.mark.parametrize("unset", [None, ""], ids=["missing", "empty"])
def test_in_test_mode_an_unset_sender_email_falls_back_to_the_test_recipient(kit, unset):
    transport = kit.mail_transport()
    report = run(kit, three(kit), execute=True, transport=transport, SENDER_EMAIL=unset)
    assert len(report.sent) == 3 and len(transport.sent) == 3
    for message in transport.sent:
        assert parseaddr(kit.header(message, "From"))[1] == "me@mail.test"
        assert kit.recipients(message) == ["me@mail.test"]


def test_a_configured_sender_email_still_wins_over_the_fallback(kit):
    transport = kit.mail_transport()
    run(kit, three(kit), execute=True, transport=transport, SENDER_EMAIL="sam@lumenlist.example.com")
    assert {parseaddr(kit.header(m, "From"))[1] for m in transport.sent} == {"sam@lumenlist.example.com"}


def test_the_fallback_is_only_for_executing_a_plan_needs_no_sender(kit):
    report = run(kit, three(kit), SENDER_EMAIL=None)
    assert len(report.planned) == 3
