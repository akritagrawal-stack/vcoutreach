"""Secret leakage sweep for the MCP server (spec sections 4.3, 4.4 and 5.3).

Written by the spec-only test author. The workspace is seeded with distinctive FAKE values for every secret the
engine knows (see mcp_helpers.FAKE_SECRETS, the fake Gmail token file and the fake OAuth client file). Then a
real client session runs every tool on its happy and error paths, including forced internal exceptions whose
message contains a key, and the tests assert that none of those strings appears in any stdout frame or any stderr
line. Redaction is defense in depth (spec 4.3), so several tests feed secret shaped data to the tools on purpose
(a contact whose name is a key, a scraped fact that contains a key) and require "[redacted]" in its place.

Where the spec leaves a choice open the docstring says so and names the section.
No dash characters appear in this file: EM_DASH comes from web_helpers.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from mcp_helpers import (FAKE_GMAIL_TOKEN, FAKE_OAUTH_CLIENT, FAKE_SECRETS, all_fake_secret_values, assert_ok,
                         assert_refused, assert_refused_code, mcp)  # noqa: F401
from web_helpers import ADA, EM_DASH, GRACE, LINUS, MINA, csv_row

PUBLIK_KEY = FAKE_SECRETS["PUBLIK_API_KEY"]
REDACTED = "[redacted]"

ALL_TOOLS = ["setup_status", "test_connection", "set_setting", "import_contacts", "list_contacts", "enrich",
             "generate_hook", "write_drafts", "list_drafts", "get_draft", "edit_draft", "approve_draft",
             "reject_draft", "send_test", "send_plan", "open_settings"]


def strings_in(value):
    """Every string (keys and values) in a JSON-like structure."""
    out = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out.extend(strings_in(v))
    elif isinstance(value, (list, tuple)):
        for item in value:
            out.extend(strings_in(item))
    return out


def marked(value):
    """True when some string in ``value`` carries the [redacted] marker.

    Spec 4.3 says "any string containing a registered value ... is replaced by the fixed token". A literal reading
    makes the whole string equal to the token; an implementation that swaps only the secret substring is equally
    safe and is tolerated here (the stricter reading is filed as a low severity dispute in the author report)."""
    return any(REDACTED in t for t in strings_in(value))


def leaked(text, values=None):
    """Which of the fake secret values appear in ``text``."""
    return [v for v in (values if values is not None else all_fake_secret_values()) if v in text]


def finish(session):
    """Close the session cleanly so every stderr line has been read, then return stdout plus stderr."""
    assert session.close() == 0
    return session.all_output()


def keyed_row(label, secret):
    """A contact whose name carries a secret, the shape a pasted note or a scraped page can produce."""
    return csv_row(id=f"k-{label}", name=f"Casey {secret} Tester", email=f"k-{label}@example.com",
                   org="Keyed Labs", role="Partner",
                   facts="your essay on how small teams ship weekly | your role leading Keyed Labs",
                   source_urls="https://example.com/essay")


# ---------------------------------------------------------------------------------------------------------------
# The full sweep: every tool, happy and error paths
# ---------------------------------------------------------------------------------------------------------------

def run_happy_sweep(session):
    """A first time user's whole setup, one call per tool at least. Returns (results, tools called)."""
    results, called = [], set()

    def call(_tool, /, **arguments):
        called.add(_tool)
        res = session.call(_tool, **arguments)
        results.append(res)
        return res

    call("setup_status")
    for service in ("publik_api", "llm", "gmail", "pitchbook", "searxng"):
        call("test_connection", service=service)
    call("set_setting", name="PROVIDER_PREFERENCE", value="template")
    call("set_setting", name="PUBLIK_RECOMMENDATION", value="skipped")
    imported = call("import_contacts", rows=session.tool_rows([ADA, GRACE, LINUS, MINA]))
    assert imported.success, imported
    listing = call("list_contacts", page_size=50)
    by_email = {c["email"]: c for c in listing.data["contacts"]}
    ada, grace, linus = (by_email[e] for e in ("ada@example.com", "grace@example.com", "linus@example.com"))
    call("enrich", partner_id=ada["partner_id"], sources=["web"])
    call("generate_hook", partner_id=ada["partner_id"])
    call("write_drafts", provider="template")
    call("list_drafts")
    call("get_draft", slug=ada["slug"])
    call("edit_draft", slug=ada["slug"], body="Hi Ada, I read your note on shipping weekly with a small team.")
    call("approve_draft", slug=ada["slug"])
    call("reject_draft", slug=grace["slug"], notes="Not this one.")
    call("send_test", partner_id=ada["partner_id"])
    call("send_plan")
    call("open_settings", anchor="publik")
    # The error paths of the same tools.
    call("get_draft", slug="nobody-here")
    call("approve_draft", slug="nobody-here")
    call("enrich", partner_id=987654)
    call("send_test", partner_id=linus["partner_id"])
    call("edit_draft", slug=ada["slug"], body=f"Hi {EM_DASH} there")
    call("set_setting", name="PUBLIK_API_KEY", value="zzleak-attempted-key-0001")
    call("import_contacts", rows=[{"id": "bad", "name": "No Email"}])
    call("test_connection", service="nonsense")
    return results, called


def test_no_fake_secret_appears_anywhere_while_every_tool_runs(mcp):
    """Spec 5.3: run every tool (happy and error paths) with every fake secret seeded; none may appear in any
    stdout line or any stderr line."""
    session = mcp(send_ready=True, secrets=True, config={
        "enrich": {"web_search": {"facts": ["Wrote an essay on weekly shipping."], "url": "https://example.com/e"}}})
    results, called = run_happy_sweep(session)
    assert called == set(ALL_TOOLS), f"the sweep must touch all 16 tools, missed {set(ALL_TOOLS) - called}"
    assert any(r.success for r in results) and any(r.is_error or r.rpc_error for r in results)
    output = finish(session)
    for res in results:
        assert leaked(res.dump()) == [], f"a secret reached a tool result: {leaked(res.dump())}\n{res!r}"
    assert leaked(output) == [], f"a secret reached stdout or stderr: {leaked(output)}"
    assert leaked(session.log_path.read_text(encoding="utf-8")) == [], "a double was handed a secret it logged"


def test_a_provider_that_echoes_the_authorization_header_back_cannot_leak_the_key(mcp):
    """The Publik host answers 401 and, as some proxies do, echoes the Authorization header in the body.
    Spec 4.3: the registry replaces the key wherever it appears, in test_connection and in the hook path."""
    session = mcp(send_ready=True, secrets=True, config={"http_rules": [
        {"match": "publikhq.com", "responses": [{"status": 401, "body": '{"error":"bad key"}',
                                                  "echo_auth": True}]}]})
    session.import_rows([ADA])
    pid = session.contacts_by_email()["ada@example.com"]["partner_id"]
    results = [
        session.call("test_connection", service="publik_api"),
        session.call("generate_hook", partner_id=pid),
        session.call("write_drafts", provider="publik"),
        session.call("setup_status"),
    ]
    assert session.provider_calls(), "the double must actually have been asked, or this proves nothing"
    assert all(call.get("has_auth") for call in session.provider_calls()), "the key was sent as a header, once"
    output = finish(session)
    for res in results:
        assert PUBLIK_KEY not in res.dump(), res
        assert leaked(res.dump()) == []
    assert leaked(output) == []
    assert "Bearer " + PUBLIK_KEY not in output


# ---------------------------------------------------------------------------------------------------------------
# Forced internal exceptions
# ---------------------------------------------------------------------------------------------------------------

FAULT_ARGS = {
    "setup_status": {},
    "test_connection": {"service": "gmail"},
    "set_setting": {"name": "PROVIDER_PREFERENCE", "value": "template"},
    "import_contacts": {"rows": [{"id": "z", "name": "Zed Tester", "email": "zed@example.com", "org": "Z Labs"}]},
    "list_contacts": {},
    "enrich": {"partner_id": 1},
    "generate_hook": {"partner_id": 1},
    "write_drafts": {},
    "list_drafts": {},
    "get_draft": {"slug": "zed"},
    "edit_draft": {"slug": "zed", "body": "Hello there."},
    "approve_draft": {"slug": "zed"},
    "reject_draft": {"slug": "zed"},
    "send_test": {"partner_id": 1},
    "send_plan": {},
    "open_settings": {},
}


def test_an_exception_message_that_contains_a_key_never_reaches_the_client_or_stderr(mcp):
    """Spec 1.2 and 5.3: an internal exception is -32603 with the fixed text "Internal error."; the detail goes to
    stderr redacted. The exception message here deliberately contains a registered key and a Gmail token."""
    secret_text = {t: f"boom in {t} using {PUBLIK_KEY} and {FAKE_GMAIL_TOKEN['refresh_token']}" for t in ALL_TOOLS}
    session = mcp(send_ready=True, secrets=True, config={"faults": secret_text})
    for tool in ALL_TOOLS:
        res = session.call(tool, **FAULT_ARGS[tool])
        assert res.rpc_code == -32603, f"{tool}: a handler crash must be -32603, got {res!r}"
        assert res.message == "Internal error.", f"{tool}: {res!r}"
        assert leaked(res.dump()) == []
    assert len(session.records("fault")) == len(ALL_TOOLS), "every handler must really have been invoked"
    # Every tool is faulted in this session, so liveness is shown with a protocol request that has no handler.
    assert session.list_tools(), "the server keeps serving after every handler crash"


def test_an_exception_detail_is_emitted_to_stderr_as_redacted(mcp):
    """Spec 5.3: "an exception message deliberately containing a key value is emitted as [redacted]". The spec
    sends the detail to stderr (section 1.2), so the redacted marker is expected there and the key is not."""
    session = mcp(send_ready=True, secrets=True, config={"faults": {"send_plan": f"token {PUBLIK_KEY} rejected"}})
    res = session.call("send_plan")
    assert res.rpc_code == -32603
    session.drain(0.3)
    err = session.stderr_text()
    out = finish(session)
    assert PUBLIK_KEY not in out
    assert REDACTED in session.stderr_text() or REDACTED in err, \
        "the exception detail on stderr must show the redaction marker where the key was"


# ---------------------------------------------------------------------------------------------------------------
# Secret shaped data that the tools legitimately carry (defense in depth)
# ---------------------------------------------------------------------------------------------------------------

def test_every_registered_secret_is_replaced_wherever_it_shows_up(mcp):
    """Spec 4.3 lists the registry: every KEY_NAMES secret ending in _KEY, _TOKEN or _SECRET, FLASK_SECRET_KEY, the
    Gmail token file contents and the OAuth client secret. Each is smuggled in as a contact name, one contact per
    secret, and none may come back."""
    flask_secret = "zzleak-flask-secret-key-5d2e90"
    session = mcp(send_ready=True, secrets=True, env={"FLASK_SECRET_KEY": flask_secret})
    secrets = {name: value for name, value in FAKE_SECRETS.items()}
    secrets["FLASK_SECRET_KEY"] = flask_secret
    secrets["gmail.token"] = FAKE_GMAIL_TOKEN["token"]
    secrets["gmail.refresh_token"] = FAKE_GMAIL_TOKEN["refresh_token"]
    secrets["gmail.client_secret"] = FAKE_GMAIL_TOKEN["client_secret"]
    secrets["oauth.client_secret"] = FAKE_OAUTH_CLIENT["installed"]["client_secret"]
    rows = [keyed_row(label.lower().replace("_", "-").replace(".", "-"), value) for label, value in secrets.items()]
    assert_ok(session.import_rows(rows))
    res = session.call("list_contacts", page_size=100)
    assert res.success
    assert len(res.data["contacts"]) == len(secrets)
    missed = [label for label, value in secrets.items() if value in res.dump() or value in res.text]
    assert missed == [], f"these registered secrets came back in a tool result: {missed}"
    assert leaked(finish(session), list(secrets.values())) == []


def test_a_key_that_reaches_a_draft_body_is_redacted_when_the_draft_is_read(mcp):
    """get_draft, edit_draft and list_drafts all carry draft text. A pasted key in a body is the user's data, but
    it must not travel to the assistant (spec 4.3)."""
    session = mcp(send_ready=True, secrets=True)
    ids = session.seed([ADA])
    slug = ids["ada"]["slug"]
    edit = session.call("edit_draft", slug=slug, body=f"Hi Ada, my login is {PUBLIK_KEY} for the demo.")
    got = session.call("get_draft", slug=slug)
    listing = session.call("list_drafts")
    for res in (edit, got, listing):
        assert PUBLIK_KEY not in res.dump(), res
    assert got.success and marked(got.data)
    assert PUBLIK_KEY not in finish(session)


def test_an_edit_that_is_refused_does_not_quote_a_key_back(mcp):
    """A refused edit (a forbidden dash) must not echo the body, which here contains a key (spec 4.3)."""
    session = mcp(send_ready=True, secrets=True)
    ids = session.seed([ADA])
    res = session.call("edit_draft", slug=ids["ada"]["slug"], body=f"Hi {EM_DASH} {PUBLIK_KEY}")
    assert_refused(res)
    assert PUBLIK_KEY not in res.dump()
    assert PUBLIK_KEY not in finish(session)


def test_a_scraped_fact_that_contains_a_key_is_redacted_inside_the_untrusted_wrapper(mcp):
    """A web page the enrich tool reads can contain anything, including a key the user's own site once leaked.
    The facts are wrapped as untrusted (spec 4.1) and still pass through redact (spec 4.3)."""
    leak = f"The staging admin password is {PUBLIK_KEY} according to the page."
    session = mcp(send_ready=True, secrets=True, config={"enrich": {"web_search": {
        "facts": [leak, "Wrote an essay on weekly shipping."], "url": "https://example.com/e"}}})
    session.import_rows([MINA])
    pid = session.contacts_by_email()["mina@example.com"]["partner_id"]
    res = session.call("enrich", partner_id=pid, sources=["web"])
    assert res.success
    assert PUBLIK_KEY not in res.dump()
    wrapped = [f["text"] for f in res.data["facts"]]
    assert all(isinstance(w, dict) and w.get("untrusted") is True for w in wrapped)
    assert any(REDACTED in w["text"] and PUBLIK_KEY not in w["text"] for w in wrapped)
    assert any(w["text"] == "Wrote an essay on weekly shipping." for w in wrapped)
    listing = session.call("list_contacts")
    assert PUBLIK_KEY not in listing.dump()
    assert PUBLIK_KEY not in finish(session)


def test_a_key_pasted_into_settings_after_the_server_started_is_still_redacted(mcp):
    """Spec 4.3: the registry is loaded at call time from the workspace and never cached across calls. A user who
    pastes a key into Settings while the assistant is connected is protected from that moment."""
    session = mcp(send_ready=True)
    late_key = "zzleak-late-pasted-key-4417"
    assert_ok(session.import_rows([keyed_row("before", late_key)]))
    before = session.call("list_contacts")
    assert late_key in before.dump(), "not a secret yet, so it is plain data"
    with open(session.ws / ".env", "a", encoding="utf-8") as handle:
        handle.write("\nOPENAI_API_KEY=" + late_key + "\n")
    after = session.call("list_contacts")
    assert after.success
    assert late_key not in after.dump(), "the key is now registered, so it must be redacted"
    assert marked(after.data)
    assert late_key not in session.stderr_text()


# ---------------------------------------------------------------------------------------------------------------
# Key shaped fields and .env shaped lines the server did not know about in advance
# ---------------------------------------------------------------------------------------------------------------

def test_dict_keys_that_look_like_secrets_are_redacted_even_when_the_value_is_unknown(mcp):
    """Spec 4.3: a dict key matching /(api[_-]?key|token|secret|password|authorization)/i has its value replaced
    by "[redacted]" even if the value is not a registered secret. Uses a handler that returns canned data."""
    canned = {
        "ok": True,
        "note": "plain text stays",
        "count": 3,
        "authorization": "Bearer some-unregistered-value",
        "api_key": "short",
        "apiKey": "camelcase-unregistered-value",
        "api-key": "dashed-unregistered-value",
        "nested": {"X-Token": "tok-unregistered-123", "password": "hunter2", "client_secret": "s3cr3t"},
        "items": [{"refresh_token": "r-unregistered-1", "label": "keep me"}],
    }
    session = mcp(config={"returns": {"setup_status": canned}})
    res = session.call("setup_status")
    assert res.success, res
    data = res.data
    assert data["note"] == "plain text stays" and data["count"] == 3
    assert data["authorization"] == REDACTED
    assert data["api_key"] == REDACTED
    assert data["apiKey"] == REDACTED
    assert data["api-key"] == REDACTED
    assert data["nested"]["X-Token"] == REDACTED
    assert data["nested"]["password"] == REDACTED
    assert data["nested"]["client_secret"] == REDACTED
    assert data["items"][0]["refresh_token"] == REDACTED
    assert data["items"][0]["label"] == "keep me"
    for raw in ("some-unregistered-value", "hunter2", "tok-unregistered-123", "r-unregistered-1", "s3cr3t",
                "camelcase-unregistered-value", "dashed-unregistered-value"):
        assert raw not in res.dump() and raw not in res.text


def test_env_shaped_lines_are_replaced_wholesale(mcp):
    """Spec 4.3: .env shaped lines (NAME=value where NAME is a known key name) are replaced wholesale. The values
    here are not registered, so only the line rule can catch them."""
    canned = {"ok": True,
              "log": "starting\nOPENAI_API_KEY=sk-unregistered-fake-abcdef123456\nSENDER_EMAIL=me@example.com\ndone",
              "other": "PUBLIK_API_KEY=pk-unregistered-0987654321"}
    session = mcp(config={"returns": {"setup_status": canned}})
    res = session.call("setup_status")
    assert res.success, res
    for raw in ("sk-unregistered-fake-abcdef123456", "pk-unregistered-0987654321"):
        assert raw not in res.dump() and raw not in res.text, f"{raw} survived: {res!r}"
    assert REDACTED in res.dump()


def test_ordinary_values_with_numbers_booleans_and_nulls_pass_through_unchanged(mcp):
    """Redaction must not eat normal data: only strings that hold a secret and key named fields change."""
    canned = {"ok": True, "n": 7, "ratio": 0.5, "flag": False, "nothing": None, "list": [1, "two", {"three": 3}],
              "text": "Plain sentence with no secret."}
    session = mcp(config={"returns": {"setup_status": canned}})
    res = session.call("setup_status")
    assert res.success
    for key, value in canned.items():
        assert res.data[key] == value, key


# ---------------------------------------------------------------------------------------------------------------
# A client that tries to hand the server a secret
# ---------------------------------------------------------------------------------------------------------------

SMUGGLED = "zzleak-smuggled-by-client-6620"


def test_a_secret_offered_through_set_setting_is_refused_not_stored_and_not_echoed(mcp):
    """Spec 1.4 and 4.4: no tool accepts a secret. The refusal must not quote the value, and nothing is written."""
    session = mcp(send_ready=True)
    before = session.env_bytes()
    settings_before = session.settings_file()
    results = [session.call("set_setting", name=name, value=SMUGGLED) for name in
               ("PUBLIK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FLASK_SECRET_KEY", "CALENDLY_API_TOKEN",
                "SEND_MODE")]
    for res in results:
        assert_refused(res)
        assert SMUGGLED not in res.dump(), res
    assert session.env_bytes() == before
    assert session.settings_file() == settings_before
    assert SMUGGLED.encode() not in session.env_bytes()
    assert SMUGGLED not in finish(session)


@pytest.mark.parametrize("tool,arguments", [
    ("setup_status", {"api_key": SMUGGLED}),
    ("write_drafts", {"provider": "publik", "api_key": SMUGGLED}),
    ("send_plan", {"authorization": "Bearer " + SMUGGLED}),
])
def test_a_secret_smuggled_as_an_extra_argument_is_rejected_before_any_handler_runs(mcp, tool, arguments):
    """Spec 4.4: additionalProperties false everywhere, so a stray api_key is -32602 and is never echoed."""
    session = mcp(send_ready=True)
    before = session.env_bytes()
    res = session.call(tool, **arguments)
    assert res.rpc_code == -32602, res
    assert SMUGGLED not in res.dump(), res
    assert session.env_bytes() == before
    assert SMUGGLED not in finish(session)


def test_a_secret_column_in_an_imported_row_is_never_stored_or_echoed(mcp):
    """Spec 4.4 and the import_contacts input (section 2): a row is an open object ("{...}") that is serialized to
    CSV, so an unknown column is not necessarily a schema violation. Either -32602 or a clean import is accepted
    here; what must hold is that the value is not echoed, not written to .env and not left in any output."""
    session = mcp(send_ready=True)
    before = session.env_bytes()
    res = session.call("import_contacts", rows=[
        {"id": "x", "name": "X Y", "email": "xy@example.com", "api_key": SMUGGLED}])
    assert res.rpc_code in (None, -32602), res
    assert SMUGGLED not in res.dump(), res
    assert session.env_bytes() == before
    assert SMUGGLED not in finish(session)


# ---------------------------------------------------------------------------------------------------------------
# The redaction functions themselves (spec 1.1 and 4.3: redact(obj), wrap_untrusted(text, source))
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def workspace_with_secrets(tmp_path, monkeypatch, request):
    """An in process workspace for calling redact() directly. The server sets OUTREACH_HOME at startup (spec 1.1),
    so the same variable is what a direct caller sets. The Keychain is stubbed as in the child process."""
    from web_helpers import write_env
    from mcp_helpers import write_gmail_files
    from vcoutreach.web import config as _cfg
    from vcoutreach.web.server import runtime_secrets

    ws = tmp_path / "redact-ws"
    ws.mkdir()
    write_env(ws, dict(FAKE_SECRETS))
    write_gmail_files(ws)
    monkeypatch.setenv("OUTREACH_HOME", str(ws))
    # Drop any workspace cached by an earlier test in this context so get_workspace() reads OUTREACH_HOME again,
    # and restore the previous cache value when this test ends.
    token = _cfg._current.set(None)
    request.addfinalizer(lambda: _cfg._current.reset(token))
    monkeypatch.setattr(runtime_secrets, "_run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 2, b"", b"keychain_item_missing"))
    return ws


def test_redact_replaces_registered_values_in_strings_lists_and_dicts(workspace_with_secrets):
    from vcoutreach.mcp.redact import redact

    key = PUBLIK_KEY
    out = redact({"a": f"prefix {key} suffix", "b": [key, {"c": f"x{key}y"}], "d": "clean", "n": 4})
    assert key not in json.dumps(out)
    assert REDACTED in out["a"] and key not in out["a"]
    assert out["b"][0] == REDACTED
    assert REDACTED in out["b"][1]["c"] and key not in out["b"][1]["c"]
    assert out["d"] == "clean" and out["n"] == 4
    assert redact(key) == REDACTED
    assert redact("nothing secret here") == "nothing secret here"


def test_redact_keeps_non_secret_keys_that_only_share_a_prefix(workspace_with_secrets):
    """Keys such as "partner_id" or "key_status_text" are ordinary; only the documented words trigger the rule."""
    from vcoutreach.mcp.redact import redact

    out = redact({"partner_id": 5, "monkey": "banana", "slug": "ada-example"})
    assert out == {"partner_id": 5, "monkey": "banana", "slug": "ada-example"}
