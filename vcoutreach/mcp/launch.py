"""How an AI assistant launches the Publikreach MCP server, and a self-check for it.

Everything except ``run_self_test`` is pure: no I/O beyond ``Path.is_file`` checks
on the resolved executable. The snippet renderers always build their output with
``json.dumps`` or explicit escaping, never string concatenation of raw paths.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from vcoutreach.mcp import PROTOCOL_VERSIONS, SERVER_NAME
from vcoutreach.mcp.schemas import FORBIDDEN_TOOL_NAMES

SERVER_KEY = SERVER_NAME  # the key under mcpServers / mcp_servers
SELF_TEST_TIMEOUT_SECONDS = 10

MSG_COULD_NOT_START = "Could not start the MCP command."
MSG_UNEXPECTED_TOOLS = "The MCP server answered but the tool list was not the expected one."
MSG_TIMEOUT = "Timed out after 10 seconds."

PYTHON_MODULE_HINT = (
    "The vcoutreach command was not found next to this Python, so the snippets use "
    "python -m vcoutreach.cli mcp instead. If you installed with pip, make sure the "
    "environment that runs this app is the one your assistant should launch."
)


class LaunchSpec(dict):
    """A dict that also allows attribute access (``spec.command``)."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def mcp_launch_spec(workspace_root=None, *, executable=None, argv0=None, frozen=None) -> LaunchSpec:
    """Resolve the command an assistant must run to start the server.

    Inputs default to ``sys.executable``, ``sys.argv[0]``, ``getattr(sys, 'frozen', False)``
    and the current workspace root. Keys of the result:

    * ``executable``: absolute command path (also under ``command``)
    * ``args``: list of arguments after the command, ending with ``--workspace <root>``
    * ``argv``: ``[executable, *args]``
    * ``mode_arg``: ``--mcp`` (app bundle) or ``mcp``
    * ``workspace``: resolved workspace path
    * ``form``: ``bundle``, ``console_script`` or ``python_module``
    * ``frozen``: bool
    * ``hint``: explanation string for the ``python_module`` form, else ``None``
    """
    if workspace_root is None:
        from vcoutreach.web.config import get_workspace
        workspace_root = get_workspace().root
    workspace = str(Path(workspace_root).expanduser().resolve())
    exe_text = str(sys.executable if executable is None else executable)
    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    script_arg = sys.argv[0] if argv0 is None and sys.argv else (argv0 or "")

    hint = None
    if is_frozen:
        command = str(Path(exe_text).resolve())
        lead: list = []
        mode_arg = "--mcp"
        form = "bundle"
    else:
        sibling = Path(exe_text).parent / "vcoutreach"
        found = None
        if sibling.is_file():
            found = sibling
        else:
            candidate = Path(script_arg) if script_arg else None
            if candidate is not None and candidate.name == "vcoutreach" and candidate.is_file():
                found = candidate.resolve()
        if found is not None:
            command = str(found)
            lead = []
            form = "console_script"
        else:
            command = exe_text
            lead = ["-m", "vcoutreach.cli"]
            form = "python_module"
            hint = PYTHON_MODULE_HINT
        mode_arg = "mcp"

    args = lead + [mode_arg, "--workspace", workspace]
    return LaunchSpec(
        executable=command,
        command=command,
        args=args,
        argv=[command] + args,
        mode_arg=mode_arg,
        workspace=workspace,
        form=form,
        frozen=is_frozen,
        hint=hint,
    )


# ---------------------------------------------------------------------------
# snippet renderers
# ---------------------------------------------------------------------------

def _argv_parts(spec) -> tuple:
    return str(spec["executable"]), [str(a) for a in spec["args"]]


def render_claude_desktop_json(spec) -> str:
    """Contents to merge into claude_desktop_config.json (valid JSON)."""
    command, args = _argv_parts(spec)
    return json.dumps({"mcpServers": {SERVER_KEY: {"command": command, "args": args}}},
                      indent=2, ensure_ascii=False)


def _shell_quote(value: str) -> str:
    """Quote for a POSIX shell. Double quotes (the familiar look) unless the value holds
    a character that stays active inside them ($, backtick) or a control character;
    then single-quote form via shlex."""
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in "$`" for ch in value):
        return shlex.quote(value)
    return '"' + "".join("\\" + ch if ch in '\\"' else ch for ch in value) + '"'


def render_claude_code_command(spec) -> str:
    """One shell command for `claude mcp add`."""
    command, args = _argv_parts(spec)
    fixed = {"-m", "vcoutreach.cli", "mcp", "--mcp", "--workspace"}
    pieces = [_shell_quote(command)]
    for arg in args:
        pieces.append(arg if arg in fixed else _shell_quote(arg))
    return "claude mcp add %s -- %s" % (SERVER_KEY, " ".join(pieces))


_TOML_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(value: str) -> str:
    """A TOML basic string with every character that needs it escaped."""
    out = ['"']
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def render_codex_toml(spec) -> str:
    """A [mcp_servers.publikreach] table for ~/.codex/config.toml."""
    command, args = _argv_parts(spec)
    return "[mcp_servers.%s]\ncommand = %s\nargs = [%s]\n" % (
        SERVER_KEY, toml_string(command), ", ".join(toml_string(a) for a in args))


def render_snippets(spec=None) -> dict:
    """All three snippets plus the spec they were built from."""
    spec = mcp_launch_spec() if spec is None else spec
    return {
        "claude_desktop": render_claude_desktop_json(spec),
        "claude_code": render_claude_code_command(spec),
        "codex": render_codex_toml(spec),
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# self test
# ---------------------------------------------------------------------------

def _self_test_input() -> bytes:
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL_VERSIONS[0], "capabilities": {},
                    "clientInfo": {"name": "publikreach-self-test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    return ("\n".join(json.dumps(f, separators=(",", ":")) for f in frames) + "\n").encode("utf-8")


def _failure(message: str, count: int = 0) -> dict:
    return {"ok": False, "message": message, "tool_count": count}


def run_self_test(argv: Sequence[str] | None = None, *, timeout: float = SELF_TEST_TIMEOUT_SECONDS) -> dict:
    """Spawn the fixed launch argv and check initialize plus tools/list.

    No shell, no user-controlled arguments, and nothing the child prints is returned
    except the parsed counts. Returns ``{"ok": bool, "message": str, "tool_count": int}``.
    """
    command = [str(a) for a in (mcp_launch_spec()["argv"] if argv is None else argv)]
    try:
        done = subprocess.run(command, input=_self_test_input(), capture_output=True,
                              timeout=timeout, shell=False, check=False)
    except subprocess.TimeoutExpired:
        return _failure(MSG_TIMEOUT)
    except (OSError, ValueError):
        return _failure(MSG_COULD_NOT_START)
    if done.returncode != 0:
        return _failure(MSG_COULD_NOT_START)

    replies: dict = {}
    for raw in done.stdout.splitlines():
        try:
            frame = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _failure(MSG_COULD_NOT_START)
        if isinstance(frame, dict) and "id" in frame:
            replies[frame["id"]] = frame

    init = replies.get(1, {}).get("result")
    listing = replies.get(2, {}).get("result")
    if not isinstance(init, dict) or not isinstance(listing, dict):
        return _failure(MSG_COULD_NOT_START)
    info = init.get("serverInfo")
    tools = listing.get("tools")
    if not isinstance(info, dict) or info.get("name") != SERVER_NAME or not isinstance(tools, list):
        return _failure(MSG_UNEXPECTED_TOOLS)
    names = [t.get("name") for t in tools if isinstance(t, dict)]
    if len(names) != len(tools) or "setup_status" not in names or any(n in FORBIDDEN_TOOL_NAMES for n in names):
        return _failure(MSG_UNEXPECTED_TOOLS, len(names))
    return {"ok": True, "message": "The MCP server started and listed %d tools." % len(names),
            "tool_count": len(names)}


__all__ = [
    "LaunchSpec", "mcp_launch_spec", "render_claude_desktop_json", "render_claude_code_command",
    "render_codex_toml", "render_snippets", "toml_string", "run_self_test",
    "MSG_COULD_NOT_START", "MSG_UNEXPECTED_TOOLS", "MSG_TIMEOUT",
]
