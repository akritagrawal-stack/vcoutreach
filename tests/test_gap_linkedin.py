"""LinkedIn session guards with no browser or network access."""
from __future__ import annotations

from unittest.mock import Mock
import json
import time

import pytest

from web_helpers import make_web, web_env_isolation  # noqa: F401


@pytest.mark.parametrize("days,source", [(None, None), (-2, "storage_state"), (0, "sqlite")])
@pytest.mark.parametrize("with_url", [True, False])
def test_known_dead_session_never_opens_a_browser(monkeypatch, days, source, with_url):
    """Contract lines 268 to 274: optional URL and dead-session browser guard."""
    from vcoutreach.web.enrichment import linkedin

    monkeypatch.setattr(linkedin, "check_session_status", lambda: {
        "logged_in": False, "auth_cookies": [], "days_until_expiry": days,
        "warning": "LinkedIn session has expired", "error": "Sign in again", "source": source,
    })
    session = Mock(side_effect=AssertionError("A known expired session must not launch Playwright"))
    monkeypatch.setattr(linkedin, "HumanSession", session)
    partner = {"partner_name": "Zoë Márquez", "firm": "Café Ventures"}
    if with_url:
        partner["linkedin_url"] = "https://www.linkedin.com/in/zoe-marquez/"
    found = linkedin.scrape_partner(partner)
    session.assert_not_called()
    assert {"url", "headline", "current_role", "about_excerpt", "education", "recent_posts", "html_excerpt", "error"} <= found.keys()
    assert found["error"]


def test_fresh_workspace_reports_no_linkedin_session(make_web):
    """Contract lines 250 to 257: status shape and permitted source values."""
    from vcoutreach.web.enrichment.linkedin import check_session_status

    workspace = make_web()
    with workspace.app.app_context():
        status = check_session_status()
    assert {"logged_in", "auth_cookies", "days_until_expiry", "warning", "error", "source"} <= status.keys()
    assert status["logged_in"] is False
    assert status["source"] is None


@pytest.mark.parametrize("offset,logged_in", [(-3600, False), (86400 * 10, True)])
def test_storage_state_cookie_expiry_controls_session(make_web, monkeypatch, offset, logged_in):
    from vcoutreach.web.enrichment.linkedin import check_session_status

    w = make_web()
    now = 1792512000.0
    monkeypatch.setattr(time, "time", lambda: now)
    state = {"cookies": [{"name": "li_at", "value": "fake-session-cookie", "domain": ".linkedin.com",
                           "path": "/", "expires": time.time() + offset,
                           "httpOnly": True, "secure": True, "sameSite": "None"}], "origins": []}
    (w.ws / "data" / ".linkedin_storage.json").write_text(json.dumps(state))
    with w.app.app_context():
        status = check_session_status()
    assert status["logged_in"] is logged_in, status
    assert status["source"] == "storage_state", status
    assert status["days_until_expiry"] == pytest.approx(offset / 86400), status
    if not logged_in:
        assert status["error"] or status["warning"]


@pytest.mark.parametrize("function,reason", [
    ("ensure_profile_dir", "Workspace-backed outreach integration does not define behavior (247 to 248)."),
    ("check_session_status storage precedence", "Storage file location, SQLite profile location, accepted cookies and expiry policy are absent (249 to 257)."),
    ("HumanConfig", "No docstring or constructor contract is supplied (246)."),
    ("HumanSession", "No browser injection, scrape output schema, rate caps, challenge detection rules or timeout policy is supplied (258 to 263)."),
])
def test_unspecified_linkedin_contract(function, reason):
    pytest.skip(f"UNSPECIFIED {function}: {reason}")
