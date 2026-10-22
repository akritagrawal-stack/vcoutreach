"""Launch command, connect snippets, Test MCP self-check, CLI and app entry points (spec 2.2 and 5.4).

Written by the spec-only test author. Everything here is driven through public surfaces: the Flask test client for
``GET /setup/ai`` and ``POST /api/mcp/self-test``, the ``vcoutreach mcp`` subprocess, and
``app_main.main(["--mcp", ...])``.

How the launch command is controlled without reading the implementation: the spec (2.2) defines the command as a
pure function of ``sys.executable``, ``sys.argv[0]``, ``sys.frozen`` and the workspace root. The tests therefore
point ``sys.executable`` at a temporary folder that holds (or lacks) a sibling ``vcoutreach`` script. For the
self-check that sibling script is a small fake MCP server, so the real route spawns it exactly as it would spawn the
real command. ASSUMPTIONS, each also listed in the author report:

* The route and the page resolve the command at request time (not cached at import). Spec 2.2.
* ``mcp_launch_spec()`` is called with no arguments, or with the workspace path when it insists on one. Its return
  shape is not specified, so the tests flatten it into the strings it contains and check those. Spec 2.2.
* The Claude Code one-liner has no specified escaping for a quote or a backslash in the workspace path (spec 2.2
  only requires JSON and TOML escaping), so that snippet is only parsed back for an ordinary workspace path.
* A fake that crashes at once, or that answers with the wrong server name, must give ok False with one of the three
  messages of spec 2.2. Which of the three is not pinned down, except for a command that cannot be started at all.

No dash characters appear in this file.
"""
from __future__ import annotations

import datetime
import html as html_lib
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import time
import tomllib
from pathlib import Path

import pytest

from mcp_helpers import EXPECTED_TOOLS
from web_helpers import (BASE, CSP_EXPECTED, REPO_ROOT, cli_env, make_web, page_text,  # noqa: F401
                         run_cli, web_env_isolation)  # noqa: F401

EXPLANATION = (
    "Let an AI assistant (Claude, Codex or any MCP client) walk you through setup and run the safe parts for you: "
    "it can check connections, import contacts, research, write and edit drafts, and send a copy to your own test "
    "inbox; it cannot see or enter your keys, and it cannot send a real email."
)
RESTART_NOTE = (
    "Restart the assistant after saving its config. Then ask it: "
    "'Run setup_status and walk me through the remaining steps.'"
)
MSG_COULD_NOT_START = "Could not start the MCP command."
MSG_NOT_EXPECTED = "The MCP server answered but the tool list was not the expected one."
MSG_TIMEOUT = "Timed out after 10 seconds."
FAILURE_MESSAGES = {MSG_COULD_NOT_START, MSG_NOT_EXPECTED, MSG_TIMEOUT}
CHILD_MARKER = "ZZ-CHILD-STDOUT-MARKER-71c4"

TRICKY_DIRNAME = 'we"ird\\ws'


# ---------------------------------------------------------------------------------------------------------------
# A fake MCP server installed as the sibling ``vcoutreach`` script
# ---------------------------------------------------------------------------------------------------------------

FAKE_SERVER = textwrap.dedent('''\
    #!{python}
    import json, sys, time
    MODE = {mode!r}
    TOOLS = {tools!r}
    SERVER_NAME = {server_name!r}
    MARKER = {marker!r}
    LEAK = {leak!r}
    with open(MARKER, "a") as handle:
        handle.write("ran " + " ".join(sys.argv[1:]) + "\\n")
    if MODE == "crash":
        sys.stderr.write("boom\\n")
        sys.exit(3)
    if MODE == "hang":
        time.sleep(120)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {{"protocolVersion": "2025-06-18", "capabilities": {{"tools": {{"listChanged": False}}}},
                       "serverInfo": {{"name": SERVER_NAME, "version": LEAK}}, "instructions": LEAK}}
        elif method == "tools/list":
            result = {{"tools": [{{"name": name, "description": LEAK, "inputSchema": {{"type": "object"}}}}
                                for name in TOOLS]}}
        else:
            result = {{}}
        sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": result}}) + "\\n")
        sys.stdout.flush()
''')


class Fake:
    """A temporary 'python' plus sibling 'vcoutreach' fake. ``sys.executable`` is pointed at the python."""

    def __init__(self, root: Path, *, mode="good", tools=None, server_name="publikreach", executable=True):
        self.root = root.resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.python = self.bin / "python"
        self.python.write_text("#!/bin/sh\nexit 0\n")
        self.python.chmod(0o755)
        self.script = self.bin / "vcoutreach"
        self.marker = self.root / "ran.txt"
        body = FAKE_SERVER.format(python=os.path.realpath(sys.executable), mode=mode,
                                  tools=list(EXPECTED_TOOLS if tools is None else tools),
                                  server_name=server_name, marker=str(self.marker), leak=CHILD_MARKER)
        self.script.write_text(body)
        self.script.chmod(0o755 if executable else 0o644)

    @property
    def ran(self) -> bool:
        return self.marker.exists()

    def runs(self):
        lines = self.marker.read_text().splitlines() if self.marker.exists() else []
        return [line[4:] if line.startswith("ran ") else line for line in lines]


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """Factory: fake_cli(...) installs the fake and points sys.executable at its python (non frozen)."""
    counter = {"n": 0}

    def factory(**kwargs) -> Fake:
        counter["n"] += 1
        fake = Fake(tmp_path / f"fake{counter['n']}", **kwargs)
        monkeypatch.setattr(sys, "executable", str(fake.python))
        monkeypatch.delattr(sys, "frozen", raising=False)
        return fake

    return factory


def workspace_settings(ws: Path) -> dict:
    path = ws / "data" / "workspace-settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def self_test(web, *, csrf=True):
    response = web.post("/api/mcp/self-test", {}, csrf=csrf)
    return response


# ---------------------------------------------------------------------------------------------------------------
# Reading the snippets back out of the page
# ---------------------------------------------------------------------------------------------------------------

def unescaped(markup: str) -> str:
    return html_lib.unescape(markup)


def json_snippet(markup: str) -> dict:
    text = unescaped(markup)
    match = re.search(r'\{\s*"mcpServers"', text)
    assert match, "no Claude Desktop JSON snippet ({\"mcpServers\": ...}) on /setup/ai"
    value, _ = json.JSONDecoder().raw_decode(text[match.start():])
    return value


def toml_snippet(markup: str) -> dict:
    text = unescaped(markup)
    match = re.search(r"\[mcp_servers\.publikreach\]", text)
    assert match, "no Codex TOML snippet ([mcp_servers.publikreach]) on /setup/ai"
    tail = text[match.start():]
    end = re.search(r"<[A-Za-z/!]", tail)
    block = tail[:end.start()] if end else tail
    return tomllib.loads(block)


def claude_code_line(markup: str) -> str:
    text = unescaped(markup)
    match = re.search(r"claude mcp add publikreach --[^\n<]*", text)
    assert match, "no Claude Code one-liner (claude mcp add publikreach -- ...) on /setup/ai"
    return match.group(0).strip()


def desktop_argv(markup: str) -> list:
    server = json_snippet(markup)["mcpServers"]["publikreach"]
    return [server["command"], *server["args"]]


def codex_argv(markup: str) -> list:
    server = toml_snippet(markup)["mcp_servers"]["publikreach"]
    return [server["command"], *server["args"]]


def same_path(left, right) -> bool:
    return Path(left).resolve() == Path(right).resolve()


# ---------------------------------------------------------------------------------------------------------------
# mcp_launch_spec()
# ---------------------------------------------------------------------------------------------------------------

def frozen_app(tmp_path: Path, monkeypatch, *, symlink=False) -> Path:
    real = (tmp_path / "Publikreach.app" / "Contents" / "MacOS").resolve()
    real.mkdir(parents=True)
    binary = real / "Publikreach"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    shown = binary
    if symlink:
        link = tmp_path / "Applications" / "PublikreachLink"
        link.parent.mkdir(parents=True)
        link.symlink_to(binary)
        shown = link
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(shown))
    return binary


# ---------------------------------------------------------------------------------------------------------------
# GET /setup/ai
# ---------------------------------------------------------------------------------------------------------------

def test_the_page_exists_and_leads_with_the_exact_explanation(make_web, fake_cli):
    web = make_web()
    fake_cli()
    response = web.get("/setup/ai")
    assert response.status_code == 200
    text = page_text(response.get_data(as_text=True))
    assert EXPLANATION in text
    assert text.index(EXPLANATION) < text.index("claude mcp add"), "spec 2.2: the sentence is at the top"


def test_the_restart_note_is_shown_below_the_snippets(make_web, fake_cli):
    web = make_web()
    fake_cli()
    markup = web.html("/setup/ai")
    text = page_text(markup)
    assert RESTART_NOTE in text
    assert text.index(RESTART_NOTE) > text.index("claude mcp add"), "spec 2.2: below the snippets"


def test_three_snippets_each_with_a_copy_button(make_web, fake_cli):
    web = make_web()
    fake_cli()
    markup = web.html("/setup/ai")
    assert len(re.findall(r"\bdata-copy\b", markup)) >= 3
    for label in ("claude_desktop_config.json", "claude mcp add publikreach", "config.toml"):
        assert label in unescaped(markup), label
    json_snippet(markup)
    toml_snippet(markup)
    claude_code_line(markup)


def test_a_source_install_shows_the_sibling_script_in_all_three_snippets(make_web, fake_cli):
    web = make_web()
    fake = fake_cli()
    markup = web.html("/setup/ai")
    ws = str(web.ws.resolve())
    desktop = desktop_argv(markup)
    assert same_path(desktop[0], fake.script)
    assert desktop[1:] == ["mcp", "--workspace", ws]
    codex = codex_argv(markup)
    assert same_path(codex[0], fake.script)
    assert codex[1:] == ["mcp", "--workspace", ws]
    words = shlex.split(claude_code_line(markup))
    assert words[:5] == ["claude", "mcp", "add", "publikreach", "--"], words
    assert same_path(words[5], fake.script)
    assert words[6:] == ["mcp", "--workspace", ws], words


def test_the_frozen_app_shows_the_bundle_executable_and_the_mcp_flag(make_web, tmp_path, monkeypatch):
    web = make_web()
    binary = frozen_app(tmp_path, monkeypatch)
    markup = web.html("/setup/ai")
    ws = str(web.ws.resolve())
    for argv in (desktop_argv(markup), codex_argv(markup)):
        assert same_path(argv[0], binary)
        assert argv[1:] == ["--mcp", "--workspace", ws]
    words = shlex.split(claude_code_line(markup))
    assert same_path(words[5], binary) and words[6:] == ["--mcp", "--workspace", ws], words


def test_without_a_script_the_page_shows_the_python_dash_m_form(make_web, tmp_path, monkeypatch):
    web = make_web()
    bare = (tmp_path / "bare2" / "bin").resolve()
    bare.mkdir(parents=True)
    python = bare / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.delattr(sys, "frozen", raising=False)
    markup = web.html("/setup/ai")
    ws = str(web.ws.resolve())
    expected = [str(python), "-m", "vcoutreach.cli", "mcp", "--workspace", ws]
    for argv in (desktop_argv(markup), codex_argv(markup)):
        assert argv[0] == str(python) or same_path(argv[0], python)
        assert argv[1:] == expected[1:], argv
    assert "python -m vcoutreach.cli mcp" in page_text(markup) or "-m vcoutreach.cli mcp" in unescaped(markup)


def test_a_workspace_path_with_a_quote_and_a_backslash_round_trips_through_json_and_toml(make_web, fake_cli):
    web = make_web(dirname=TRICKY_DIRNAME)
    fake_cli()
    markup = web.html("/setup/ai")
    ws = str(web.ws.resolve())
    assert '"' in ws and "\\" in ws, "the fixture path must really contain a quote and a backslash"
    assert desktop_argv(markup)[-1] == ws, "spec 2.2: JSON escaping is done by json.dumps"
    assert codex_argv(markup)[-1] == ws, "spec 2.2: TOML escaping is explicit and correct"
    assert desktop_argv(markup) == codex_argv(markup)


def test_the_page_loads_no_script_from_another_origin(make_web, fake_cli):
    web = make_web()
    fake_cli()
    markup = web.html("/setup/ai")
    sources = re.findall(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", markup, re.I)
    assert sources, "the copy buttons and Test MCP need a local script"
    for src in sources:
        assert not re.match(r"(?i)^(https?:)?//", src), f"non local script: {src}"
    assert not re.search(r"""(?i)\b(?:href|src)\s*=\s*["']https?://(?!publikhq\.com)[^"']*\.(?:js|css)""", markup)


def test_the_publik_card_is_repeated_on_the_page_at_the_publik_anchor(make_web, fake_cli):
    web = make_web()
    fake_cli()
    markup = web.html("/setup/ai")
    assert re.search(r"""\bid\s*=\s*["']publik["']""", markup), "spec 2.3: the card is anchored #publik"
    assert "Recommended: connect the Publik API for better personalization" in page_text(markup)


def test_the_page_is_not_served_without_a_session(make_web, fake_cli):
    web = make_web(login=False)
    fake_cli()
    response = web.get("/setup/ai")
    assert response.status_code != 200
    assert EXPLANATION not in page_text(response.get_data(as_text=True))


def test_the_settings_card_links_to_the_setup_page(make_web, fake_cli):
    web = make_web()
    fake_cli()
    markup = web.html("/settings")
    card = re.search(r"""id\s*=\s*["']svc-ai-setup["'](.*?)(?=id\s*=\s*["']svc-|\Z)""", markup, re.S)
    assert card, "spec 2.1: the Settings page has the svc-ai-setup card"
    assert "/setup/ai" in card.group(1)
    assert "Set up with AI" in page_text(card.group(1))


# ---------------------------------------------------------------------------------------------------------------
# POST /api/mcp/self-test
# ---------------------------------------------------------------------------------------------------------------

def test_a_correct_server_gives_ok_sixteen_tools_and_records_the_time(make_web, fake_cli):
    web = make_web()
    fake = fake_cli()
    assert workspace_settings(web.ws).get("mcp_last_self_test_ok", "") == ""
    response = self_test(web)
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True, data
    assert data["tool_count"] == 16
    assert isinstance(data["message"], str) and "\n" not in data["message"]
    stamp = workspace_settings(web.ws).get("mcp_last_self_test_ok")
    assert stamp, "spec 2.2: a success stores mcp_last_self_test_ok"
    when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    now = datetime.datetime.now(when.tzinfo) if when.tzinfo else datetime.datetime.now()
    assert abs((now - when).total_seconds()) < 120
    assert fake.ran


def test_the_check_runs_the_exact_command_the_snippets_show(make_web, fake_cli):
    web = make_web()
    fake = fake_cli()
    self_test(web)
    runs = fake.runs()
    assert runs, "the fake MCP command was never started"
    assert runs[0] == f"mcp --workspace {web.ws.resolve()}", runs


def test_the_card_changes_from_not_connected_to_ready_after_a_good_check(make_web, fake_cli):
    web = make_web()
    fake_cli()

    def chip():
        markup = web.html("/settings")
        card = re.search(r"""id\s*=\s*["']svc-ai-setup["'](.*?)(?=id\s*=\s*["']svc-|\Z)""", markup, re.S)
        assert card, "svc-ai-setup card missing"
        return page_text(card.group(1))

    before = chip()
    assert "Not connected" in before and "Ready" not in before
    assert self_test(web).get_json()["ok"] is True
    after = chip()
    assert "Ready" in after and "Not connected" not in after


def test_a_server_that_advertises_send_live_is_rejected(make_web, fake_cli):
    web = make_web()
    fake_cli(tools=EXPECTED_TOOLS + ["send_live"])
    data = self_test(web).get_json()
    assert data["ok"] is False
    assert data["message"] == MSG_NOT_EXPECTED
    assert workspace_settings(web.ws).get("mcp_last_self_test_ok", "") == ""


@pytest.mark.parametrize("forbidden", ["set_api_key", "go_live", "run_shell"])
def test_every_forbidden_tool_name_fails_the_check(make_web, fake_cli, forbidden):
    web = make_web()
    fake_cli(tools=EXPECTED_TOOLS + [forbidden])
    data = self_test(web).get_json()
    assert data["ok"] is False, forbidden
    assert data["message"] == MSG_NOT_EXPECTED
    assert forbidden not in json.dumps(data), "the page must not echo the child's tool names"


def test_a_hanging_server_gets_the_timeout_message_within_eleven_seconds(make_web, fake_cli):
    web = make_web()
    fake_cli(mode="hang")
    started = time.monotonic()
    data = self_test(web).get_json()
    elapsed = time.monotonic() - started
    assert elapsed < 11, f"spec 5.4: within 11 s, took {elapsed:.1f}"
    assert data["ok"] is False
    assert data["message"] == MSG_TIMEOUT
    assert workspace_settings(web.ws).get("mcp_last_self_test_ok", "") == ""


def test_a_command_that_cannot_be_started_says_so(make_web, fake_cli):
    web = make_web()
    fake = fake_cli(executable=False)
    data = self_test(web).get_json()
    assert data["ok"] is False
    assert data["message"] == MSG_COULD_NOT_START
    assert not fake.ran
    assert workspace_settings(web.ws).get("mcp_last_self_test_ok", "") == ""


def test_nothing_the_child_prints_reaches_the_response(make_web, fake_cli):
    web = make_web()
    fake_cli()
    response = self_test(web)
    assert CHILD_MARKER not in response.get_data(as_text=True)
    assert CHILD_MARKER not in web.html("/setup/ai")
    assert CHILD_MARKER not in web.html("/settings")


def test_without_a_csrf_token_the_request_is_a_400_and_nothing_is_started(make_web, fake_cli):
    web = make_web()
    fake = fake_cli()
    response = self_test(web, csrf=False)
    assert response.status_code == 400
    assert not fake.ran
    assert workspace_settings(web.ws).get("mcp_last_self_test_ok", "") == ""


def test_without_a_session_nothing_is_started(make_web, fake_cli):
    web = make_web(login=False)
    fake = fake_cli()
    response = web.request("post", "/api/mcp/self-test", data={"csrf_token": "x"})
    assert response.status_code != 200
    assert not fake.ran


def test_request_data_cannot_change_the_command_that_is_spawned(make_web, fake_cli, tmp_path):
    """Spec 4 item 7: a fixed argument list, no user controlled argv."""
    web = make_web()
    fake = fake_cli()
    evil = tmp_path / "evil-ran.txt"
    response = web.post("/api/mcp/self-test", {
        "command": f"touch {evil}", "workspace": str(tmp_path / "other"), "args": "--evil",
        "executable": "/bin/sh", "cmd": f"touch {evil}"})
    assert response.status_code == 200
    assert not evil.exists()
    runs = fake.runs()
    assert runs and runs[0] == f"mcp --workspace {web.ws.resolve()}", runs


def test_the_check_works_for_the_frozen_app_command_too(make_web, tmp_path, monkeypatch):
    """The frozen command is ``<bundle executable> --mcp --workspace <ws>``; a fake bundle answers like the app."""
    web = make_web()
    root = (tmp_path / "frozen" / "Publikreach.app" / "Contents" / "MacOS").resolve()
    root.mkdir(parents=True)
    binary = root / "Publikreach"
    marker = tmp_path / "frozen-ran.txt"
    binary.write_text(FAKE_SERVER.format(python=os.path.realpath(sys.executable), mode="good",
                                         tools=list(EXPECTED_TOOLS), server_name="publikreach",
                                         marker=str(marker), leak=CHILD_MARKER))
    binary.chmod(0o755)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(binary))
    data = self_test(web).get_json()
    assert data["ok"] is True, data
    assert marker.read_text().splitlines()[0] == f"ran --mcp --workspace {web.ws.resolve()}"


# ---------------------------------------------------------------------------------------------------------------
# vcoutreach mcp (CLI)
# ---------------------------------------------------------------------------------------------------------------

def test_mcp_help_exits_zero_and_mentions_the_workspace_option(tmp_path):
    result = run_cli(["mcp", "--help"], tmp_path, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--workspace" in result.stdout


def test_a_clean_end_of_input_exits_zero_with_nothing_on_stdout(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result = subprocess.run([sys.executable, "-m", "vcoutreach", "mcp", "--workspace", str(ws)], input="",
                            cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "stdout is reserved for protocol frames (spec 1.1)"
    assert "publikreach mcp ready" in result.stderr


def test_the_cli_answers_an_initialize_request_on_stdout_only(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}}) + "\n"
    result = subprocess.run([sys.executable, "-m", "vcoutreach", "mcp", "--workspace", str(ws)], input=request,
                            cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    assert json.loads(lines[0])["result"]["serverInfo"]["name"] == "publikreach"


# ---------------------------------------------------------------------------------------------------------------
# The Mac app entry point: app_main --mcp
# ---------------------------------------------------------------------------------------------------------------

APP_DIR = REPO_ROOT / "packaging" / "mac" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


@pytest.fixture
def app_main(monkeypatch):
    import importlib

    module = importlib.import_module("app_main")
    for name in ("webview", "AppKit"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return module


class Recorder:
    def __init__(self, result=0):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def patch_serve(monkeypatch, result=0):
    import vcoutreach.mcp.server as server

    recorder = Recorder(result)
    monkeypatch.setattr(server, "serve", recorder)
    return recorder


def served_workspace(recorder: Recorder) -> Path:
    args, kwargs = recorder.calls[0]
    value = args[0] if args else kwargs["workspace"]
    return Path(value).resolve()


def forbid_gui_paths(app_main, monkeypatch):
    """Anything the GUI or self-test path would touch now fails the test if --mcp reaches it."""
    def boom(*a, **k):
        raise AssertionError("the --mcp path must not reach the window, lock, server or self-test code")

    for name in ("SingleInstanceLock", "bootstrap_workspace", "launch", "self_test", "start_server",
                 "exchange_token"):
        if hasattr(app_main, name):
            monkeypatch.setattr(app_main, name, boom)


def test_app_main_mcp_calls_serve_with_the_workspace_and_returns_its_code(app_main, monkeypatch, tmp_path):
    forbid_gui_paths(app_main, monkeypatch)
    recorder = patch_serve(monkeypatch, 0)
    assert app_main.main(["--mcp", "--workspace", str(tmp_path)]) == 0
    assert len(recorder.calls) == 1
    assert served_workspace(recorder) == tmp_path.resolve()


def test_app_main_mcp_never_imports_the_window_toolkits(app_main, monkeypatch, tmp_path):
    forbid_gui_paths(app_main, monkeypatch)
    patch_serve(monkeypatch, 0)
    app_main.main(["--mcp", "--workspace", str(tmp_path)])
    assert "webview" not in sys.modules
    assert "AppKit" not in sys.modules


def test_app_main_mcp_takes_no_single_instance_lock(app_main, monkeypatch, tmp_path):
    """Spec 1.2: the window may be open at the same time. A held lock must not stop the MCP server."""
    forbid_gui_paths(app_main, monkeypatch)
    recorder = patch_serve(monkeypatch, 0)
    lock = tmp_path / "data" / ".app.lock"
    lock.parent.mkdir(parents=True)
    import fcntl

    handle = open(lock, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert app_main.main(["--mcp", "--workspace", str(tmp_path)]) == 0
        assert len(recorder.calls) == 1
    finally:
        handle.close()


def test_app_main_mcp_prints_nothing_to_stdout(app_main, monkeypatch, tmp_path, capsys):
    forbid_gui_paths(app_main, monkeypatch)
    patch_serve(monkeypatch, 0)
    app_main.main(["--mcp", "--workspace", str(tmp_path)])
    out = capsys.readouterr().out
    assert out == ""
    assert "Publikreach self-test passed." not in out


def test_app_main_mcp_failure_goes_to_stderr_with_exit_one(app_main, monkeypatch, tmp_path, capsys):
    forbid_gui_paths(app_main, monkeypatch)
    patch_serve(monkeypatch, RuntimeError("zz-startup-failure"))
    code = app_main.main(["--mcp", "--workspace", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "zz-startup-failure" not in captured.out


def test_app_main_self_test_still_prints_its_line_without_the_mcp_flag(app_main, monkeypatch, tmp_path, capsys):
    """Regression guard: adding --mcp must not disturb the existing self-test mode."""
    recorder = patch_serve(monkeypatch, 0)
    monkeypatch.setattr(app_main, "self_test", lambda *a, **k: None)
    assert app_main.main(["--self-test", "--workspace", str(tmp_path)]) == 0
    assert "Publikreach self-test passed." in capsys.readouterr().out
    assert recorder.calls == []


def test_the_app_entry_script_serves_mcp_over_stdio_as_a_subprocess(tmp_path):
    """The real entry script, run as a child, answers a client and exits 0 on end of input."""
    ws = tmp_path / "ws"
    ws.mkdir()
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}}}) + "\n"
    result = subprocess.run([sys.executable, str(APP_DIR / "app_main.py"), "--mcp", "--workspace", str(ws)],
                            input=request, cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (lines, result.stderr)
    assert json.loads(lines[0])["result"]["serverInfo"]["name"] == "publikreach"
    assert "self-test passed" not in result.stdout
