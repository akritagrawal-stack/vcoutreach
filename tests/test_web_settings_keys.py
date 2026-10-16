"""Settings: the sender profile, the provider key box, the generated Flask secret.

SPEC_V3 sections 3 and 4, SPEC.md sections 3 and 5. The sender profile is validated by the
engine's own loader; keys are written only to the workspace .env (mode 0600) and never echoed.
No dash characters appear in this file.
"""
from __future__ import annotations

import logging
import re

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, EM_DASH, GRACE, Web, body, create_web_app, decode_cookie_payload, file_mode, page_text, post_keys,
    read_draft_file, read_env, seed_contacts, sender_form, write_env,
)

SENDER_FIELDS = ["name", "title", "org", "website", "signature", "product_lines", "traction_lines", "ask_line",
                 "first_touch_subject", "approved_claims", "forbidden_phrases"]
ALLOWED_KEYS = {
    "PUBLIK_API_KEY": "fakekey_Zq8w2e9r7t6y5u4i3o",
    "OPENAI_API_KEY": "fakekey_Lk3j4h5g6f7d8s9a0p",
    "ANTHROPIC_API_KEY": "fakekey_Mn1b2v3c4x5z6l7k8j",
    "OPENAI_BASE_URL": "https://models.example.com/v1",
    "OPENAI_MODEL": "model-name-for-tests",
    "ANTHROPIC_MODEL": "other-model-for-tests",
}
LITERAL_FALLBACK = "vc-outreach-dev-" + "secret-change-in-production"


def save_sender(w: Web, **over):
    return w.post(w.url("sender_save"), sender_form(**over), follow=True)


def new_text(before: str, after: str) -> str:
    """The words that the second page shows and the first does not: flash messages and inline errors, not the help text."""
    import difflib

    old, new = before.split(), after.split()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return " ".join(" ".join(new[j1:j2]) for tag, _i1, _i2, j1, j2 in matcher.get_opcodes() if tag in ("insert", "replace"))


def key_form(name: str, value: str):
    return {name: value, "name": name, "key": name, "value": value}


# ---------------------------------------------------------------------------------------------------------------
# Sender profile
# ---------------------------------------------------------------------------------------------------------------

def test_settings_has_a_field_for_every_part_of_the_sender_profile(web):
    markup = web.html("/settings")
    missing = [f for f in SENDER_FIELDS if not re.search(rf'name="{f}"', markup)]
    assert not missing, f"missing sender form fields: {missing}"


def test_a_valid_sender_form_is_saved_in_the_engine_format_and_shown_again(web):
    from vcoutreach.sender import load_sender

    r = save_sender(web, name="Pat Quinn", org="Quinn Labs", first_touch_subject="Question for {first_name}",
                    traction_lines=["Five teams use it every week."],
                    approved_claims={"teams": "Five teams use it every week"}, forbidden_phrases=["synergy"])
    assert r.status_code == 200
    sender = load_sender(web.ws / "sender.json")
    assert sender.name == "Pat Quinn" and sender.org == "Quinn Labs"
    assert sender.first_touch_subject == "Question for {first_name}"
    assert sender.traction_lines == ("Five teams use it every week.",)
    assert dict(sender.approved_claims) == {"teams": "Five teams use it every week"}
    assert sender.forbidden_phrases == ("synergy",)
    assert "Pat Quinn" in body(web.get("/settings"))


@pytest.mark.parametrize("over, fragment", [
    ({"first_touch_subject": "Big news!"}, "exclamation"),
    ({"first_touch_subject": "About {org}"}, "first_name"),
    ({"first_touch_subject": "A" * 121}, "120"),
    ({"signature": "Pat Quinn " + EM_DASH + " Founder"}, "dash"),
    ({"first_touch_subject": "Hello\nWorld"}, "single line"),
], ids=["exclamation", "placeholder", "too-long", "dash", "line-break"])
def test_the_engine_sender_rules_are_enforced_and_the_old_profile_is_kept(make_web, over, fragment):
    w = make_web(sender=True)
    before = (w.ws / "sender.json").read_bytes()
    clean_page = w.text("/settings")
    r = save_sender(w, **over)
    # the words must be NEW on the page after the save: the form's own help text may already mention them
    assert fragment in new_text(clean_page, page_text(r)).lower(), new_text(clean_page, page_text(r))[:300]
    assert (w.ws / "sender.json").read_bytes() == before


def test_the_first_touch_subject_is_used_for_the_next_drafts(make_web):
    w = make_web()
    save_sender(w, first_touch_subject="Question for {first_name}")
    seed_contacts(w, [ADA, GRACE])
    w.post(w.url("drafts_write"), {"provider": "template"}, follow=True)
    assert read_draft_file(w.ws, "ada").subject == "Question for Ada"
    assert read_draft_file(w.ws, "grace").subject == "Question for Grace"


def test_the_settings_page_has_a_gmail_card_that_says_how_to_connect(web):
    """SPEC_V2 section 9 names the command `vcoutreach auth gmail`; SPEC_V3 section 5 moves the instructions to the
    README. The Settings card must show the connection state and say where the steps are (SPEC_QUESTIONS.md Q10)."""
    text = web.text("/settings")
    low = text.lower()
    assert "gmail" in low and re.search(r"(?i)not connected|connected", text)
    assert "vcoutreach auth gmail" in text or "readme" in low


# ---------------------------------------------------------------------------------------------------------------
# Provider keys
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["PUBLIK_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"])
def test_an_allowed_key_is_written_to_env_with_mode_0600_and_never_shown_anywhere(web, caplog, name):
    value = ALLOWED_KEYS[name]
    caplog.set_level(logging.DEBUG)
    not_set_before = web.text("/settings").lower().count("not set")
    r = web.post(web.url("keys_save"), key_form(name, value))
    assert r.status_code in (302, 303)
    cookies = " ".join(r.headers.getlist("Set-Cookie")) + " ".join(
        decode_cookie_payload(v) + v for _, v in web.cookie_pairs())
    assert value not in cookies
    after = web.get("/settings", follow_redirects=True)
    assert value not in body(after) and value not in page_text(after)
    assert read_env(web.ws)[name] == value
    assert file_mode(web.ws / ".env") == 0o600
    assert value not in caplog.text
    if name.endswith("_KEY"):
        assert body(after).lower().count("not set") < not_set_before or not_set_before == 0


def test_saving_a_key_keeps_the_other_lines_and_replaces_rather_than_duplicates(make_web):
    w = make_web({"KEEP_ME": "1"}, env_lines=["# a comment", "OPENAI_API_KEY=old_value_here"])
    post_keys(w, key_form("OPENAI_API_KEY", ALLOWED_KEYS["OPENAI_API_KEY"]))
    lines = (w.ws / ".env").read_text(encoding="utf-8").splitlines()
    assert "KEEP_ME=1" in lines and "# a comment" in lines
    assert len([l for l in lines if l.startswith("OPENAI_API_KEY=")]) == 1
    assert read_env(w.ws)["OPENAI_API_KEY"] == ALLOWED_KEYS["OPENAI_API_KEY"]


def test_a_loose_env_file_is_tightened_to_0600_when_a_key_is_saved(make_web):
    import os

    w = make_web({"KEEP_ME": "1"})
    os.chmod(w.ws / ".env", 0o644)
    post_keys(w, key_form("OPENAI_MODEL", "some-model"))
    assert file_mode(w.ws / ".env") == 0o600


@pytest.mark.parametrize("name", ["EVIL_VAR", "FLASK_SECRET_KEY", "SEND_MODE"])
def test_names_outside_the_allowlist_are_never_written(web, name):
    secret_before = read_env(web.ws).get("FLASK_SECRET_KEY")
    web.post(web.url("keys_save"), {name: "live" if name == "SEND_MODE" else "overwritten"}, follow=True)
    env = read_env(web.ws)
    if name == "SEND_MODE":
        assert env.get("SEND_MODE", "test") != "live"
    elif name != "FLASK_SECRET_KEY":
        assert name not in env
    assert env.get("FLASK_SECRET_KEY") == secret_before


@pytest.mark.parametrize("bad", [" ", '"', "#", "$", "`", "\n"],
                         ids=["space", "dquote", "hash", "dollar", "backtick", "newline"])
def test_values_with_injection_characters_are_refused_naming_the_key_without_echoing_the_value(web, bad):
    value = f"MARKERxyz{bad}EVIL_LINE=1"
    r = web.post(web.url("keys_save"), key_form("OPENAI_API_KEY", value), follow=True)
    assert r.status_code < 500
    assert "OPENAI_API_KEY" in page_text(r)
    assert "MARKERxyz" not in body(r)
    text = (web.ws / ".env").read_text(encoding="utf-8")
    assert "MARKERxyz" not in text and "EVIL_LINE" not in text


# ---------------------------------------------------------------------------------------------------------------
# The generated Flask secret
# ---------------------------------------------------------------------------------------------------------------

def test_the_flask_secret_is_generated_into_env_with_mode_0600(web):
    env = read_env(web.ws)
    assert re.fullmatch(r"[0-9a-f]{64}", env.get("FLASK_SECRET_KEY", "")), "expected secrets.token_hex(32)"
    assert file_mode(web.ws / ".env") == 0o600


def test_the_flask_secret_is_kept_across_restarts_and_differs_between_workspaces(make_web):
    first = make_web(dirname="a")
    key_a = read_env(first.ws)["FLASK_SECRET_KEY"]
    create_web_app(first.ws)
    assert read_env(first.ws)["FLASK_SECRET_KEY"] == key_a
    second = make_web(dirname="b")
    assert read_env(second.ws)["FLASK_SECRET_KEY"] != key_a
    third = make_web({"KEEP_ME": "1"}, env_lines=["# my notes"])
    text = (third.ws / ".env").read_text(encoding="utf-8")
    assert "KEEP_ME=1" in text and "# my notes" in text and "FLASK_SECRET_KEY=" in text


def test_the_flask_secret_never_appears_in_a_page_and_there_is_no_literal_fallback(web):
    secret = read_env(web.ws)["FLASK_SECRET_KEY"]
    for path in ("/crm", "/settings", "/crm/send-plan", "/crm/drafts", "/replies"):
        assert secret not in web.html(path), path
    assert web.app.secret_key and web.app.secret_key != LITERAL_FALLBACK
    assert len(str(web.app.secret_key)) >= 32
