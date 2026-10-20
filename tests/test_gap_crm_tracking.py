"""Tracking tests limited to the supplied public contract."""
from __future__ import annotations

import pytest
import datetime as dt
import json


def activation(root, action, email, age_days=0, status="tracking_activation_success"):
    from vcoutreach.web.crm_tracking import email_identity_hash
    timestamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)).isoformat(timespec="seconds")
    record = {"status": status, "tracking_action_id": action,
              "recipient_email_hash": email_identity_hash(email),
              "executed_at": timestamp}
    if status is None:
        record.pop("status")
    (root / (action + ".activation.json")).write_text(json.dumps(record))


def test_populated_journals_match_only_the_intended_recipient(tmp_path):
    from vcoutreach.web.crm_tracking import activated_actions_for_email
    action = "action_" + "a" * 32
    activation(tmp_path, action, "ada@example.com")
    assert activated_actions_for_email("ada@example.com", root=tmp_path) == (action,)
    assert activated_actions_for_email("grace@example.com", root=tmp_path) == ()


@pytest.mark.parametrize("external", [False, True], ids=["inside-root", "outside-root"])
def test_symlinked_activation_journal_cannot_attest_tracking(tmp_path, external):
    from vcoutreach.web.crm_tracking import activated_actions_for_email

    root = tmp_path / "journals"
    root.mkdir()
    target_dir = tmp_path / "elsewhere" if external else root
    target_dir.mkdir(exist_ok=True)
    action = "action_" + "d" * 32
    activation(target_dir, action, "ada@example.com")
    target = target_dir / (action + ".activation.json")
    # A regular journal is accepted, proving the record and recipient are valid.
    assert activated_actions_for_email("ada@example.com", root=target_dir) == (action,)
    target = target.rename(target_dir / "saved-record.json")
    (root / (action + ".activation.json")).symlink_to(target)
    assert activated_actions_for_email("ada@example.com", root=root) == ()


@pytest.mark.parametrize("status", ["failed", "pending", None])
def test_unsuccessful_attestation_is_excluded(tmp_path, status):
    from vcoutreach.web.crm_tracking import activated_actions_for_email
    action = "action_" + "b" * 32
    activation(tmp_path, action, "ada@example.com", status=status)
    assert activated_actions_for_email("ada@example.com", root=tmp_path) == ()


@pytest.mark.parametrize("days,included", [(29.9, True), (30.1, False), (100, False)])
def test_activation_freshness_is_thirty_days(tmp_path, days, included):
    from vcoutreach.web.crm_tracking import activated_actions_for_email
    action = "action_" + "c" * 32
    activation(tmp_path, action, "ada@example.com", age_days=days)
    assert activated_actions_for_email("ada@example.com", root=tmp_path) == ((action,) if included else ())


@pytest.mark.parametrize("email", ["priya@cedargrove.example", "zoë@cafeventures.example",
                                  pytest.param("", marks=pytest.mark.skip(reason="UNSPECIFIED activated_actions_for_email: empty-email validation is not documented (46 to 47)."))])
def test_no_activation_journals_means_no_attested_actions(tmp_path, email):
    """Contract lines 46 to 47: an empty local join has no activated actions."""
    from vcoutreach.web.crm_tracking import activated_actions_for_email

    assert activated_actions_for_email(email, root=tmp_path) == ()


@pytest.mark.parametrize("function,reason", [
    ("fetch_tracking_snapshot", "Worker URL, authorization, transport, and response schema are unspecified (44 to 45)."),
    ("fetch_action_snapshot", "Worker configuration and classified-count response schema are unspecified (48 to 49)."),
    ("fetch_contact_tracking", "No tracked/untracked response representation or count schema is defined (50 to 51)."),
    ("fetch_action_events", "404 behavior is defined, but no endpoint/configuration or injectable transport is supplied (52 to 57)."),
    ("action_partner_map", "Journal layout, attestation format, and contact schema are unspecified (58 to 59)."),
])
def test_unspecified_tracking_contract(function, reason):
    pytest.skip(f"UNSPECIFIED {function}: {reason}")
