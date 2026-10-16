"""Mutation-gap tests: the Settings key form and the provider key endpoints (SPEC_V3 section 4, SPEC.md section 3,
CONTRACT.md "Sending", README "Safety" and docs/web-app.md "Settings it reads").

Written from the specs only. Flask's test client, a tmp_path workspace, no network, no real sends.
No dash characters appear in this file.
"""
from __future__ import annotations

import os
import re

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures: web_env_isolation, make_web, web, send_web, ...)
from web_helpers import (
    TEST_INBOX, Web, banner_mode, body, decode_cookie_payload, file_mode, page_text, post_keys, read_env,
)
from web_gap_helpers import new_text

SECRET = "Zq7Kp9Xw3Lm5Vb2Rt8"


def env_bytes(w: Web) -> bytes:
    return (w.ws / ".env").read_bytes()


def key_form(name: str, value: str):
    """The single-field form: one name field and one value field (plus the name as a field, as the bulk form does)."""
    return {name: value, "name": name, "key": name, "value": value}


# The shapes a single-field post can take: name + value, key + value, or just NAME=value.
SINGLE_SHAPES = {
    "name-value": lambda n, v: {"name": n, "value": v},
    "key-value": lambda n, v: {"key": n, "value": v},
    "bulk-field": lambda n, v: {n: v},
    "all-three": key_form,
}


# ---------------------------------------------------------------------------------------------------------------
# T-KEY-SINGLE: nothing outside the allowlist can be written, and live mode can only be armed by GO LIVE
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shape", sorted(SINGLE_SHAPES))
def test_the_single_field_form_cannot_arm_live_mode(make_web, shape):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX, "KEEP_ME": "1"})
    before = env_bytes(w)
    page_before = w.text("/settings")
    r = w.post(w.url("keys_save"), SINGLE_SHAPES[shape]("SEND_MODE", "live"), follow=True)
    assert r.status_code < 500
    assert env_bytes(w) == before, "SEND_MODE must not be writable through the key form"
    assert read_env(w.ws).get("SEND_MODE") == "test"
    assert banner_mode(w) == "test", "the next page load must still say TEST"
    assert banner_mode(w, "/crm") == "test"
    assert new_text(page_before, page_text(r)).strip() or r.status_code >= 400, "a refusal must say something"


@pytest.mark.parametrize("name", ["FLASK_SECRET_KEY", "FOO_BAR", "PATH", "SENDER_PASSWORD", "send_mode"])
@pytest.mark.parametrize("shape", sorted(SINGLE_SHAPES))
def test_the_single_field_form_writes_nothing_outside_the_allowlist(make_web, shape, name):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX})
    before = env_bytes(w)
    secret_before = read_env(w.ws)["FLASK_SECRET_KEY"]
    r = w.post(w.url("keys_save"), SINGLE_SHAPES[shape](name, "overwritten-value"), follow=True)
    assert r.status_code < 500
    assert env_bytes(w) == before
    assert read_env(w.ws)["FLASK_SECRET_KEY"] == secret_before
    assert name == "FLASK_SECRET_KEY" or name not in read_env(w.ws)
    # a refused write must not have changed the process environment either
    assert os.environ.get(name) != "overwritten-value"


def test_the_single_field_form_still_saves_an_allowlisted_name(make_web):
    """Control for the refusals above: the same shape works for a documented key."""
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX})
    w.post(w.url("keys_save"), key_form("OPENAI_MODEL", "model-name-for-tests"), follow=True)
    assert read_env(w.ws)["OPENAI_MODEL"] == "model-name-for-tests"
    assert read_env(w.ws).get("SEND_MODE", "test") == "test"


# ---------------------------------------------------------------------------------------------------------------
# T-CHARS: injection characters in a value
# ---------------------------------------------------------------------------------------------------------------

BAD_CHARS = {
    "semicolon": ";", "backslash": "\\", "nul": "\x00", "tab": "\t", "bell": "\x07", "carriage-return": "\r",
    "escape": "\x1b", "unit-separator": "\x1f", "del": "\x7f", "newline": "\n",
}


@pytest.mark.parametrize("name", ["OPENAI_MODEL", "OPENAI_API_KEY"])
@pytest.mark.parametrize("char", sorted(BAD_CHARS))
def test_a_value_with_a_shell_or_control_character_is_refused_and_the_env_is_unchanged(make_web, name, char):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX})
    before = env_bytes(w)
    page_before = w.text("/settings")
    value = f"MARKERabc{BAD_CHARS[char]}tail123"
    r = w.post(w.url("keys_save"), key_form(name, value), follow=True)
    assert r.status_code < 500
    assert env_bytes(w) == before
    assert name not in read_env(w.ws)
    assert "MARKERabc" not in body(r) and "tail123" not in body(r), "the refused value must not be echoed"
    assert new_text(page_before, page_text(r)).strip(), "a refusal must say something"


def test_a_legal_value_with_punctuation_is_accepted(make_web):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX})
    legal = "vendor/model-name_v2.1:beta"
    w.post(w.url("keys_save"), key_form("OPENAI_MODEL", legal), follow=True)
    assert read_env(w.ws).get("OPENAI_MODEL") == legal
    assert file_mode(w.ws / ".env") == 0o600


# ---------------------------------------------------------------------------------------------------------------
# T-CAP-BOUNDS: DAILY_SEND_CAP is an integer from 1 to 10000
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "-1", "10001", "abc", "1.5", "9" * 30, "1e3", "0x10", "+0", "", "  "],
                         ids=["zero", "minus-one", "10001", "letters", "decimal", "30-digits", "exponent", "hex",
                              "plus-zero", "empty", "blank"])
def test_a_daily_cap_outside_1_to_10000_is_refused_and_the_stored_cap_is_unchanged(make_web, bad):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX, "DAILY_SEND_CAP": "7"})
    r = w.post(w.url("keys_save"), key_form("DAILY_SEND_CAP", bad), follow=True)
    assert r.status_code < 500
    assert read_env(w.ws).get("DAILY_SEND_CAP") == "7"


@pytest.mark.parametrize("good", ["1", "25", "10000"])
def test_a_daily_cap_from_1_to_10000_is_accepted(make_web, good):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX, "DAILY_SEND_CAP": "7"})
    w.post(w.url("keys_save"), key_form("DAILY_SEND_CAP", good), follow=True)
    assert read_env(w.ws).get("DAILY_SEND_CAP") == good


# ---------------------------------------------------------------------------------------------------------------
# T-URL: the tracking Worker address is an https origin only
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "http://worker.example.com",
    "https://user:pass@worker.example.com",
    "https://user@worker.example.com",
    "https://worker.example.com/some/path",
    "https://worker.example.com?x=1",
    "https://worker.example.com#frag",
    "ftp://worker.example.com",
    "worker.example.com",
], ids=["http", "credentials", "user-only", "path", "query", "fragment", "ftp", "no-scheme"])
def test_a_tracking_worker_url_that_is_not_a_plain_https_origin_is_refused(make_web, bad):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX})
    before = env_bytes(w)
    w.post(w.url("keys_save"), key_form("TRACKING_WORKER_URL", bad), follow=True)
    assert env_bytes(w) == before
    assert "TRACKING_WORKER_URL" not in read_env(w.ws)


def test_a_plain_https_origin_is_accepted_as_the_tracking_worker_url(make_web):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX})
    w.post(w.url("keys_save"), key_form("TRACKING_WORKER_URL", "https://worker.example.com"), follow=True)
    assert read_env(w.ws).get("TRACKING_WORKER_URL") == "https://worker.example.com"


# ---------------------------------------------------------------------------------------------------------------
# T-ENV-PRIVATE: the key file is never world readable, not even for an instant
# ---------------------------------------------------------------------------------------------------------------

def test_the_env_file_is_replaced_by_rename_from_a_temp_file_that_is_already_private(make_web, monkeypatch):
    w = make_web({"TEST_RECIPIENT_EMAIL": TEST_INBOX, "KEEP_ME": "1"})
    env_path = w.ws / ".env"
    os.chmod(env_path, 0o644)
    seen = []
    real_replace, real_rename = os.replace, os.rename

    def spy(real):
        def wrapper(src, dst, *a, **k):
            try:
                seen.append((os.fspath(src), os.fspath(dst), os.stat(src).st_mode & 0o777))
            except OSError:
                seen.append((os.fspath(src), os.fspath(dst), None))
            return real(src, dst, *a, **k)
        return wrapper

    with monkeypatch.context() as mp:
        mp.setattr(os, "replace", spy(real_replace))
        mp.setattr(os, "rename", spy(real_rename))
        w.post(w.url("keys_save"), key_form("OPENAI_MODEL", "model-for-tests"), follow=True)
    assert read_env(w.ws)["OPENAI_MODEL"] == "model-for-tests"
    assert file_mode(env_path) == 0o600, "the final file must be private even if it was 0644 before"
    into_env = [s for s in seen if os.path.realpath(s[1]) == os.path.realpath(env_path)]
    assert into_env, "the .env must be replaced by renaming a finished temp file over it, not rewritten in place"
    for src, _dst, mode in into_env:
        assert mode == 0o600, f"temp file {src} had mode {mode and oct(mode)} when it was renamed into place"
    assert "KEEP_ME=1" in env_path.read_text(encoding="utf-8")


def test_a_brand_new_env_file_is_private_from_the_moment_it_exists(tmp_path, monkeypatch):
    """The first start creates the .env (with the generated secret): its temp file is private as well."""
    ws = tmp_path / "fresh"
    ws.mkdir()
    seen = []
    real_replace, real_rename = os.replace, os.rename

    def spy(real):
        def wrapper(src, dst, *a, **k):
            try:
                seen.append((os.fspath(dst), os.stat(src).st_mode & 0o777))
            except OSError:
                pass
            return real(src, dst, *a, **k)
        return wrapper

    from web_helpers import create_web_app

    with monkeypatch.context() as mp:
        mp.setattr(os, "replace", spy(real_replace))
        mp.setattr(os, "rename", spy(real_rename))
        create_web_app(ws, None)
    assert file_mode(ws / ".env") == 0o600
    for dst, mode in seen:
        if os.path.realpath(dst) == os.path.realpath(ws / ".env"):
            assert mode == 0o600, f"temp file renamed onto .env had mode {oct(mode)}"


# ---------------------------------------------------------------------------------------------------------------
# T-KEY-API: the provider key endpoints never echo the key
# ---------------------------------------------------------------------------------------------------------------

KEY_ENDPOINTS = [
    ("/api/gemini/key", {}),
    ("/api/model-provider/key", {"provider": "openai"}),
    ("/api/model-provider/key", {"provider": "anthropic"}),
    ("/api/data-provider/key", {"provider": "enrow"}),
    ("/api/data-provider/key", {"provider": "apollo"}),
]


def every_window(secret: str, size: int = 5):
    return {secret[i:i + size] for i in range(len(secret) - size + 1)}


def everything_returned(w: Web, response) -> str:
    """Status line, headers, body and the cookie payloads that came back, as one string."""
    parts = [str(response.status_code), str(response.headers), response.get_data(as_text=True)]
    parts += [decode_cookie_payload(v) + v for _, v in w.cookie_pairs()]
    return "\n".join(parts)


@pytest.mark.parametrize("path, extra", KEY_ENDPOINTS, ids=[f"{p}:{e.get('provider', '')}" for p, e in KEY_ENDPOINTS])
def test_a_provider_key_endpoint_never_echoes_the_key_on_success(web, path, extra):
    r = web.post(path, {**extra, "api_key": SECRET})
    assert r.status_code < 500
    dump = everything_returned(web, r)
    assert SECRET not in dump
    leaked = [w5 for w5 in every_window(SECRET) if w5 in dump]
    assert not leaked, f"pieces of the key came back: {leaked}"
    # and the page that follows (flash messages) does not show it either
    follow = web.get("/settings", follow_redirects=True)
    assert SECRET not in body(follow) and not [w5 for w5 in every_window(SECRET) if w5 in body(follow)]
    assert SECRET in "".join(read_env(web.ws).values()), "control: the key was stored in the workspace .env"


@pytest.mark.parametrize("path, extra", KEY_ENDPOINTS, ids=[f"{p}:{e.get('provider', '')}" for p, e in KEY_ENDPOINTS])
@pytest.mark.parametrize("flaw", ["space", "newline", "quote", "dollar"])
def test_a_provider_key_endpoint_never_echoes_a_refused_key_either(web, path, extra, flaw):
    bad = {"space": f"{SECRET} extra", "newline": f"{SECRET}\nEVIL=1", "quote": f'{SECRET}"', "dollar": f"{SECRET}$X"}[flaw]
    before = env_bytes(web)
    r = web.post(path, {**extra, "api_key": bad})
    assert r.status_code < 500
    dump = everything_returned(web, r)
    assert SECRET not in dump
    leaked = [w5 for w5 in every_window(SECRET) if w5 in dump]
    assert not leaked, f"pieces of the refused key came back: {leaked}"
    assert env_bytes(web) == before, "a key with an injection character must not be written"


def test_a_provider_key_endpoint_with_an_unknown_provider_writes_nothing_and_echoes_nothing(web):
    before = env_bytes(web)
    r = web.post("/api/model-provider/key", {"provider": "not-a-provider", "api_key": SECRET})
    assert r.status_code < 500
    assert env_bytes(web) == before
    assert SECRET not in everything_returned(web, r)
