"""Mutation-gap tests: Host and Origin checks, the one-time launch token, the mode toggle
(SPEC.md section 3, SPEC_V3 section 4, CONTRACT.md "Security" and "Sending").

Written from the specs only. Flask's test client, a tmp_path workspace, no network.
No dash characters appear in this file.
"""
from __future__ import annotations

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures)
from web_helpers import (
    ADA, BASE, Web, banner_mode, body, contact_form_fields, export_rows, get_token, page_text, read_env, typed,
)

DENIED_MESSAGE = "Open the newest link printed in the Terminal window."


def anon(w: Web) -> Web:
    return Web(w.app, w.ws, w.transport)


# ---------------------------------------------------------------------------------------------------------------
# T-HOST-PORT: the Host header must carry the port the app listens on
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "127.0.0.1:1", "127.0.0.1:8766", "127.0.0.1:80", "localhost:1", "localhost:9999", "127.0.0.1:8765x",
    "127.0.0.1:",
], ids=["loopback-port-1", "loopback-next-port", "loopback-port-80", "localhost-port-1", "localhost-9999",
        "port-with-suffix", "empty-port"])
def test_a_loopback_host_with_a_different_port_is_refused_even_with_a_valid_session(make_web, host):
    w = make_web()
    assert w.raw("get", "/crm", host="127.0.0.1:8765").status_code == 200, "control: the right port is accepted"
    r = w.raw("get", "/crm", host=host)
    assert r.status_code in (400, 403), f"Host {host!r} answered {r.status_code}"


def test_a_loopback_host_without_any_port_is_refused(make_web):
    w = make_web()
    r = w.raw("get", "/crm", host="127.0.0.1")
    assert r.status_code in (400, 403)


def test_a_request_with_no_host_header_at_all_is_refused(make_web):
    from werkzeug.test import EnvironBuilder

    w = make_web()
    builder = EnvironBuilder("/crm", base_url=BASE)
    environ = builder.get_environ()
    environ.pop("HTTP_HOST", None)
    environ["SERVER_NAME"] = "127.0.0.1"
    environ["SERVER_PORT"] = "80"
    environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in w.cookie_pairs())
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = int(status.split()[0])
        return lambda data: None

    chunks = b"".join(w.app.wsgi_app(environ, start_response))
    assert captured["status"] in (400, 403), captured
    assert b"csrf_token" not in chunks, "no form (and no CSRF token) may be rendered for a request without a Host"


def test_a_valid_token_link_on_the_wrong_port_sets_no_cookie(make_web):
    w = anon(make_web(login=False))
    r = w.raw("get", f"/?token={get_token(w.app)}", host="127.0.0.1:1", cookies=[])
    assert r.status_code in (400, 403)
    assert not r.headers.getlist("Set-Cookie")
    # the refused attempt must not have used up the token
    assert w.get(f"/?token={get_token(w.app)}").status_code in (302, 303)


def test_a_post_with_the_wrong_host_port_changes_nothing(make_web):
    w = make_web()
    payload = dict(contact_form_fields(ADA), csrf_token=w.csrf)
    r = w.raw("post", w.url("contact_new"), host="127.0.0.1:1", data=payload)
    assert r.status_code in (400, 403)
    assert export_rows(w) == []


# ---------------------------------------------------------------------------------------------------------------
# T-ORIGIN-SCHEME: Origin and Referer must be same-origin including the scheme
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"Origin": "https://127.0.0.1:8765"},
    {"Origin": "https://localhost:8765"},
    {"Origin": "ftp://127.0.0.1:8765"},
    {"Referer": "https://127.0.0.1:8765/crm/new"},
], ids=["https-origin", "https-localhost-origin", "ftp-origin", "https-referer"])
def test_a_post_whose_origin_has_the_right_host_and_port_but_the_wrong_scheme_is_refused(web, headers):
    r = web.post(web.url("contact_new"), contact_form_fields(ADA), headers=headers)
    assert r.status_code in (400, 403)
    assert export_rows(web) == []


@pytest.mark.parametrize("headers", [
    {"Origin": "http://127.0.0.1:8765"},
    {"Referer": "http://127.0.0.1:8765/crm/new"},
], ids=["same-origin", "same-origin-referer"])
def test_a_post_with_a_same_origin_origin_or_referer_is_accepted(web, headers):
    """Control for the refusals above."""
    r = web.post(web.url("contact_new"), contact_form_fields(ADA), headers=headers)
    assert r.status_code in (302, 303)
    assert [row["email"] for row in export_rows(web)] == ["ada@example.com"]


def test_a_wrong_scheme_origin_cannot_flip_the_send_mode(make_web):
    w = make_web({"TEST_RECIPIENT_EMAIL": "inbox@testmail.example.org"})
    r = w.post(w.url("toggle"), {**typed("GO LIVE"), "mode": "live"}, headers={"Origin": "https://127.0.0.1:8765"})
    assert r.status_code in (400, 403)
    assert banner_mode(w) == "test"
    assert read_env(w.ws).get("SEND_MODE", "test") == "test"


# ---------------------------------------------------------------------------------------------------------------
# T-TOKEN-ONCE: the launch token works once
# ---------------------------------------------------------------------------------------------------------------

def test_the_launch_token_link_is_accepted_once_and_refused_the_second_time(make_web):
    w = anon(make_web(login=False))
    token = get_token(w.app)
    first = w.get(f"/?token={token}")
    assert first.status_code in (302, 303)
    assert first.headers.getlist("Set-Cookie")
    other_browser = Web(w.app, w.ws, w.transport)
    second = other_browser.get(f"/?token={token}")
    assert second.status_code == 403
    assert DENIED_MESSAGE in page_text(second)
    assert not second.headers.getlist("Set-Cookie")
    assert other_browser.get("/crm").status_code == 403, "the replayed link must not open any page"
    assert w.get("/crm").status_code == 200, "the first browser keeps its session"


def test_replaying_the_token_from_a_cookieless_client_never_logs_in_even_many_times(make_web):
    w = anon(make_web(login=False))
    token = get_token(w.app)
    assert w.get(f"/?token={token}").status_code in (302, 303)
    for _ in range(3):
        replay = Web(w.app, w.ws, w.transport)
        r = replay.get(f"/?token={token}")
        assert r.status_code == 403
        assert replay.get("/settings").status_code == 403


# ---------------------------------------------------------------------------------------------------------------
# T-TOGGLE: an unknown mode value changes nothing
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("junk", ["banana", "yes", "2"], ids=["banana", "yes", "two"])
def test_the_mode_toggle_refuses_an_unknown_mode_and_leaves_the_mode_alone(make_web, junk):
    w = make_web({"TEST_RECIPIENT_EMAIL": "inbox@testmail.example.org"})
    before = (w.ws / ".env").read_bytes()
    payload = {**typed("GO LIVE"), "mode": junk, "send_mode": junk, "target_mode": junk}
    r = w.post(w.url("toggle"), payload)
    assert r.status_code in (400, 302, 303)
    assert (w.ws / ".env").read_bytes() == before
    assert banner_mode(w) == "test"


def test_the_mode_toggle_answers_400_for_a_made_up_mode(make_web):
    w = make_web({"TEST_RECIPIENT_EMAIL": "inbox@testmail.example.org"})
    r = w.post(w.url("toggle"), {**typed("GO LIVE"), "mode": "banana", "send_mode": "banana", "target_mode": "banana"})
    assert r.status_code == 400
    assert read_env(w.ws).get("SEND_MODE", "test") == "test"
