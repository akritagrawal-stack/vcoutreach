"""MCP stdio protocol conformance (spec section 5.1).

Written by the spec-only test author: these tests know the wire contract (spec sections 1.1 to 1.6) and nothing
about how the server is built. The server runs as a child process, exactly as an MCP client launches it, and the
in-process ``serve(workspace, stdin=..., stdout=...)`` seam is used in two tests.

Assumptions that the spec leaves open are written in the docstring of the test that depends on them.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from mcp_helpers import (EXPECTED_TOOLS, FORBIDDEN_TOOL_NAMES, INSTRUCTIONS, READ_ONLY_TOOLS, SUPPORTED_VERSIONS,
                         ToolResult, assert_ok, assert_refused, mcp, plain, serve_inprocess)  # noqa: F401

# Spec section 1.5: the arguments of each tool.
EXPECTED_PROPS = {
    "setup_status": set(),
    "test_connection": {"service"},
    "set_setting": {"name", "value"},
    "import_contacts": {"csv_path", "rows", "batch_name"},
    "list_contacts": {"q", "view", "page", "page_size"},
    "enrich": {"partner_id", "sources"},
    "generate_hook": {"partner_id", "provider"},
    "write_drafts": {"slugs", "provider", "overwrite"},
    "list_drafts": {"state"},
    "get_draft": {"slug"},
    "edit_draft": {"slug", "subject", "body", "lane"},
    "approve_draft": {"slug"},
    "reject_draft": {"slug", "notes"},
    "send_test": {"partner_id"},
    "send_plan": set(),
    "open_settings": {"anchor"},
}


def wait_for_stderr(session, needle: str, timeout: float = 5.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        text = session.stderr_text()
        if needle in text:
            return text
        time.sleep(0.05)
    return session.stderr_text()


# ---------------------------------------------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_initialize_echoes_each_supported_protocol_version(mcp, version):
    session = mcp(plain=True, init=False)
    reply = session.initialize(version)
    assert reply["result"]["protocolVersion"] == version


@pytest.mark.parametrize("asked", ["2030-12-31"])
def test_initialize_answers_2025_06_18_for_an_unknown_client_version(mcp, asked):
    """Spec 1.2: a client version the server does not support gets the newest supported one."""
    session = mcp(plain=True, init=False)
    reply = session.initialize(asked)
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_initialize_result_shape_and_exact_instructions(mcp):
    import vcoutreach

    session = mcp(plain=True, init=False)
    reply = session.initialize("2025-06-18")
    result = reply["result"]
    assert reply["jsonrpc"] == "2.0"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"] == {"name": "publikreach", "version": vcoutreach.__version__}
    assert isinstance(result["instructions"], str) and result["instructions"].strip()
    assert result["instructions"] == INSTRUCTIONS


def test_initialize_instructions_tell_the_assistant_the_safety_story(mcp):
    session = mcp(plain=True, init=False)
    text = session.initialize()["result"]["instructions"]
    for needle in ("setup_status", "human_action", "no live send tool", "untrusted", "Settings"):
        assert needle in text, needle


# ---------------------------------------------------------------------------------------------------------------
# framing
# ---------------------------------------------------------------------------------------------------------------

def test_every_response_is_one_newline_terminated_utf8_json_line(mcp):
    session = mcp(plain=True, init=False)
    session.initialize()
    session.request("tools/list")
    session.request("ping")
    raw_lines = session.stdout_raw
    assert len(raw_lines) == 3
    for line in raw_lines:
        assert line.endswith(b"\n") and not line.endswith(b"\r\n")
        assert line.count(b"\n") == 1, "a frame must not contain an embedded raw newline"
        parsed = json.loads(line.decode("utf-8"))
        assert parsed["jsonrpc"] == "2.0"


@pytest.mark.parametrize("request_id", [0, "abc-123"])
def test_response_echoes_the_request_id_and_its_type(mcp, request_id):
    session = mcp(plain=True, init=False)
    session.initialize()
    reply = session.request("ping", id=request_id)
    assert reply["id"] == request_id
    assert type(reply["id"]) is type(request_id)


def test_notifications_initialized_produces_no_output_and_ping_is_empty_object(mcp):
    session = mcp(plain=True, init=False)
    session.initialize(send_initialized=False)
    session.notify("notifications/initialized")
    assert session.drain(0.8) == []
    reply = session.request("ping")
    assert reply["result"] == {}


def test_tools_call_before_initialize_is_error_32002(mcp):
    session = mcp(init=False)
    reply = session.request("tools/call", {"name": "setup_status", "arguments": {}})
    assert reply["error"]["code"] == -32002
    # Spec 1.2 names the message "Server not initialized"; a trailing period is not asserted either way.
    assert reply["error"]["message"].rstrip(".") == "Server not initialized"
    # and it did not do the work
    assert session.records("http") == []


def test_malformed_json_line_is_parse_error_and_the_server_keeps_going(mcp):
    session = mcp(plain=True)
    session.send_raw('{"jsonrpc": "2.0", "id": 5, "method": "pi')
    bad = session.read_message()
    assert bad["error"]["code"] == -32700
    assert bad.get("id") is None
    again = session.request("ping")
    assert again["result"] == {}


def test_unknown_method_is_32601(mcp):
    session = mcp(plain=True)
    reply = session.request("resources/subscribe", {})
    assert reply["error"]["code"] == -32601
    assert session.request("ping")["result"] == {}


def test_exit_code_is_zero_on_clean_eof(mcp):
    session = mcp(plain=True)
    session.request("ping")
    assert session.close() == 0


# ---------------------------------------------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------------------------------------------

def test_tools_list_has_exactly_the_sixteen_tools(mcp):
    session = mcp(plain=True)
    tools = session.list_tools()
    assert sorted(t["name"] for t in tools) == sorted(EXPECTED_TOOLS)
    assert len(tools) == 16
    for tool in tools:
        assert isinstance(tool["description"], str) and tool["description"].strip()


def test_every_tool_schema_is_a_closed_object_with_the_documented_arguments(mcp):
    session = mcp(plain=True)
    for tool in session.list_tools():
        schema = tool["inputSchema"]
        name = tool["name"]
        assert schema["type"] == "object", name
        assert schema.get("additionalProperties") is False, f"{name} must reject unknown arguments"
        assert set(schema.get("properties", {})) == EXPECTED_PROPS[name], name


def test_tool_annotations_match_the_spec_table(mcp):
    session = mcp(plain=True)
    tools = {t["name"]: t for t in session.list_tools()}
    for name, tool in tools.items():
        assert isinstance(tool.get("annotations"), dict), f"{name} has no annotations"
    for name in READ_ONLY_TOOLS:
        ann = tools[name]["annotations"]
        assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False and ann["openWorldHint"] is False, name
    ann = tools["test_connection"]["annotations"]
    assert ann["readOnlyHint"] is True and ann["openWorldHint"] is True
    ann = tools["set_setting"]["annotations"]
    assert (ann["readOnlyHint"], ann["destructiveHint"], ann["idempotentHint"]) == (False, False, True)
    ann = tools["send_test"]["annotations"]
    assert (ann["readOnlyHint"], ann["destructiveHint"], ann["openWorldHint"]) == (False, False, True)
    ann = tools["enrich"]["annotations"]
    assert (ann["readOnlyHint"], ann["openWorldHint"]) == (False, True)
    ann = tools["import_contacts"]["annotations"]
    assert (ann["readOnlyHint"], ann["destructiveHint"], ann["idempotentHint"]) == (False, False, True)
    writers = set(tools) - set(READ_ONLY_TOOLS) - {"test_connection"}
    for name in writers:
        assert tools[name]["annotations"]["readOnlyHint"] is False, f"{name} writes and must not claim read only"


def test_forbidden_tool_names_are_not_advertised(mcp):
    session = mcp(plain=True)
    names = {t["name"] for t in session.list_tools()}
    assert names.isdisjoint(FORBIDDEN_TOOL_NAMES), names & set(FORBIDDEN_TOOL_NAMES)


@pytest.mark.parametrize("forbidden", ["send_live", "set_api_key", "run_shell"])
def test_calling_a_forbidden_tool_is_invalid_params(mcp, forbidden):
    session = mcp()
    res = session.call(forbidden)
    assert res.rpc_code == -32602, res
    assert session.gmail_sends() == []


# ---------------------------------------------------------------------------------------------------------------
# tools/call argument handling
# ---------------------------------------------------------------------------------------------------------------

def test_unknown_tool_is_invalid_params(mcp):
    session = mcp()
    assert session.call("does_not_exist").rpc_code == -32602


@pytest.mark.parametrize("tool", ["setup_status", "set_setting", "write_drafts", "send_test"])
def test_unknown_argument_api_key_is_invalid_params_on_every_tool(mcp, tool):
    session = mcp()
    res = session.call(tool, api_key="zzleak-should-never-be-accepted-1")
    assert res.rpc_code == -32602, f"{tool} accepted an unknown api_key argument: {res!r}"
    assert "zzleak-should-never-be-accepted-1" not in session.all_output()


@pytest.mark.parametrize("bad_arguments", [[], "text"])
def test_non_object_arguments_are_invalid_params(mcp, bad_arguments):
    session = mcp()
    res = session.call_raw("setup_status", bad_arguments)
    assert res.rpc_code == -32602, res


@pytest.mark.parametrize("partner_id", [0, 2 ** 63, "7"])
def test_partner_id_outside_one_to_2_63_minus_1_is_invalid_params(mcp, partner_id):
    session = mcp()
    for tool in ("enrich", "generate_hook", "send_test"):
        res = session.call(tool, partner_id=partner_id)
        assert_refused(res)
        assert res.rpc_code == -32602 or res.is_error
        assert res.success is False
    assert session.gmail_sends() == []


def test_more_than_500_rows_is_invalid_params_and_imports_nothing(mcp):
    session = mcp()
    rows = [{"id": f"r{i}", "name": f"Person {i}", "email": f"p{i}@example.org"} for i in range(501)]
    res = session.call("import_contacts", rows=rows)
    assert res.rpc_code == -32602, res
    total = assert_ok(session.call("list_contacts"))["total"]
    assert total == 0


def test_to_argument_on_edit_draft_is_invalid_params(mcp):
    """The recipient of a draft can never be changed through MCP (spec 1.5, edit_draft)."""
    session = mcp()
    res = session.call("edit_draft", slug="anyone", to="attacker@example.org", body="Hello")
    assert res.rpc_code == -32602, res


def test_a_handler_exception_is_internal_error_and_the_server_keeps_serving(mcp):
    """Uses the runner's fault hook, which replaces ``tools.HANDLERS[name]`` (spec 6, Lane A dispatch)."""
    session = mcp(config={"faults": {"list_drafts": "kaboom detail that must stay private"}})
    res = session.call("list_drafts")
    assert res.rpc_code == -32603
    assert res.message == "Internal error."
    # Spec 1.2: details go to stderr (redacted); only the protocol stream must not carry them.
    assert "kaboom" not in session.stdout_text()
    assert assert_ok(session.call("list_contacts"))["total"] == 0


# ---------------------------------------------------------------------------------------------------------------
# stdout and stderr hygiene
# ---------------------------------------------------------------------------------------------------------------

def test_stdout_carries_only_protocol_frames_across_a_busy_session(mcp):
    session = mcp()
    session.call("setup_status")
    session.call("list_contacts", q="anything")
    session.call("test_connection", service="llm")
    session.call("open_settings", anchor="publik")
    session.call("not_a_tool")
    session.send_raw("not json at all")
    session.read_message()
    session.request("ping")
    assert session.bad_lines == []
    for raw in session.stdout_raw:
        json.loads(raw.decode("utf-8"))


def test_stderr_has_a_startup_line_and_one_line_per_request_without_arguments(mcp):
    import vcoutreach

    session = mcp()
    session.call("list_contacts", q="DISTINCTIVE-QUERY-9x")
    session.call("not_a_tool")
    text = wait_for_stderr(session, "list_contacts")
    wait_for_stderr(session, "not_a_tool")
    text = session.stderr_text()
    ws = session.ws
    startup = [ln for ln in text.splitlines() if ln.startswith("publikreach mcp ready")]
    assert len(startup) == 1, text
    assert re.fullmatch(r"publikreach mcp ready workspace=(.+) version=(\S+)", startup[0]), startup[0]
    found = re.fullmatch(r"publikreach mcp ready workspace=(.+) version=(\S+)", startup[0])
    assert found.group(1) in (str(ws), str(ws.resolve()))
    assert found.group(2) == vcoutreach.__version__
    request_lines = [ln for ln in text.splitlines() if "list_contacts" in ln]
    assert request_lines, "no log line for the list_contacts call"
    assert any(("ok" in ln) for ln in request_lines)
    assert "DISTINCTIVE-QUERY-9x" not in text, "arguments must never be logged"
    assert "tools/call" in text or "initialize" in text


def test_stderr_never_contains_results(mcp):
    session = mcp()
    session.import_rows([{"id": "z1", "name": "Resultname Ofzed", "email": "resultname@example.org"}])
    session.call("list_contacts")
    text = wait_for_stderr(session, "list_contacts")
    assert "Resultname" not in text and "resultname@example.org" not in text


# ---------------------------------------------------------------------------------------------------------------
# UTF-8
# ---------------------------------------------------------------------------------------------------------------

def test_non_ascii_contact_survives_the_round_trip_without_locale_settings(mcp):
    """The child has no LANG and no PYTHONIOENCODING (spec 1.1: UTF-8 regardless of locale)."""
    session = mcp()
    name = "Zoë Müller 李雷"
    data = assert_ok(session.import_rows([{"id": "u1", "name": name, "email": "zoe@example.org",
                                           "org": "Café Labs"}]))
    assert data["imported"] == 1
    contacts = assert_ok(session.call("list_contacts"))["contacts"]
    assert plain(contacts[0]["name"]) == name
    assert plain(contacts[0]["org"]) == "Café Labs"
    session.stdout_text()  # decodes as strict UTF-8 or raises


# ---------------------------------------------------------------------------------------------------------------
# the in-process seam (spec 1.1: serve(workspace, stdin=None, stdout=None))
# ---------------------------------------------------------------------------------------------------------------

def _rpc(i, method, params=None):
    msg = {"jsonrpc": "2.0", "id": i, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def test_serve_seam_handles_initialize_list_and_ping_then_returns_zero_on_eof(tmp_path, monkeypatch):
    ws = tmp_path / "inproc"
    ws.mkdir()
    lines = [
        _rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        _rpc(2, "tools/list"),
        _rpc(3, "ping"),
    ]
    code, frames, raw = serve_inprocess(ws, lines, monkeypatch)
    assert code in (0, None)
    assert [f["id"] for f in frames] == [1, 2, 3]
    assert frames[0]["result"]["protocolVersion"] == "2024-11-05"
    assert sorted(t["name"] for t in frames[1]["result"]["tools"]) == sorted(EXPECTED_TOOLS)
    assert frames[2]["result"] == {}
    assert raw.endswith(b"\n") and raw.count(b"\n") == 3

