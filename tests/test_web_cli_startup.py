"""`vcoutreach ui` as a real process on a real loopback socket (SPEC_V3 section 4, SPEC.md sections 1 and 3).

Each test starts the command in a subprocess with a throwaway HOME, reads the printed link, talks to the server with
urllib or http.client on 127.0.0.1 only, and stops it. No outside network, no real home directory.
No dash characters appear in this file.
"""
from __future__ import annotations

import contextlib
import http.cookiejar
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    REPO_ROOT, UiProcess, cli_env, file_mode, free_port, header_values, http_request, read_env, run_cli,
)

pytestmark = pytest.mark.local_http

DENIED_MESSAGE = "Open the newest link printed in the Terminal window."


def _restore_sigint_default():
    """Runs in the child just before exec. A background job starts with SIGINT set to SIG_IGN, which the server would
    inherit and then ignore Ctrl-C. Resetting it to the default lets Python install its normal interrupt handler."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class _SigintDefaultPopen(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        if os.name == "posix":
            kwargs["preexec_fn"] = _restore_sigint_default
        super().__init__(*args, **kwargs)


@contextlib.contextmanager
def _server_children_start_with_default_sigint():
    """Swap in the Popen subclass only while UiProcess spawns the server, so other subprocess calls are untouched."""
    original = subprocess.Popen
    subprocess.Popen = _SigintDefaultPopen
    try:
        yield
    finally:
        subprocess.Popen = original


@pytest.fixture
def ui(tmp_path):
    """Factory for running `vcoutreach ui`; every process is killed at the end of the test."""
    started = []
    home = Path(os.environ["HOME"])
    cwd = Path.cwd()

    def start(*, port=None, workspace=None, extra_args=(), extra_env=None, wait=True):
        ws = workspace or (tmp_path / "ui-ws")
        ws.mkdir(parents=True, exist_ok=True)
        with _server_children_start_with_default_sigint():
            proc = UiProcess(ws, home, cwd, port=port if port is not None else free_port(), extra_args=extra_args,
                             extra_env=extra_env)
        started.append(proc)
        proc.workspace = ws
        if wait:
            proc.wait_for_link()
        return proc

    yield start
    for proc in started:
        proc.kill()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def browser(jar):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar),
                                       NoRedirect)


def fetch(opener, url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode("utf-8", "replace")


def wait_for_server(port, attempts=40):
    for _ in range(attempts):
        try:
            return http_request(port, "GET", "/healthz")
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the server never answered /healthz")


def test_ui_help_lists_the_flags_and_the_command_prints_the_one_time_link_on_the_requested_port(ui, tmp_path):
    result = run_cli(["ui", "--help"], cwd=Path.cwd(), home=Path(os.environ["HOME"]))
    assert result.returncode == 0
    for flag in ("--workspace", "--port", "--no-browser"):
        assert flag in result.stdout
    proc = ui()
    assert re.fullmatch(r"http://127\.0\.0\.1:%d/\?token=\S{16,}" % proc.port, proc.link)
    assert any(line.startswith("Open this link: ") for line in proc.seen)


def test_the_link_is_exchanged_for_an_http_only_strict_cookie_and_the_pages_then_open(ui):
    proc = ui()
    wait_for_server(proc.port)
    jar = http.cookiejar.CookieJar()
    opener = browser(jar)
    status, headers, _ = fetch(opener, proc.link)
    assert status in (302, 303)
    location = headers.get("Location", "")
    assert proc.token not in location and "token" not in location
    cookies = list(jar)
    assert cookies, "no session cookie was set"
    assert all(c.has_nonstandard_attr("HttpOnly") for c in cookies)
    assert any((c.get_nonstandard_attr("SameSite") or "").lower() == "strict" for c in cookies)
    base = f"http://127.0.0.1:{proc.port}"
    status, _, page = fetch(opener, base + "/")
    assert status in (200, 302)
    status, _, page = fetch(opener, base + "/crm")
    assert status == 200 and "publik" in page.lower()
    status, _, text = fetch(urllib.request.build_opener(urllib.request.ProxyHandler({})), base + "/healthz")
    assert status == 200 and json.loads(text) == {"ok": True}


def test_a_page_without_the_cookie_or_with_a_wrong_token_is_403_with_the_plain_message(ui):
    proc = ui()
    wait_for_server(proc.port)
    plain = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    for url in (f"http://127.0.0.1:{proc.port}/crm", f"http://127.0.0.1:{proc.port}/?token=wrong"):
        status, _, page = fetch(plain, url)
        assert status == 403 and DENIED_MESSAGE in page, url


def test_a_foreign_host_header_is_refused_even_with_a_valid_session_cookie(ui):
    proc = ui()
    wait_for_server(proc.port)
    jar = http.cookiejar.CookieJar()
    fetch(browser(jar), proc.link)
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    status, _, page = http_request(proc.port, "GET", "/crm", headers={"Cookie": cookie})
    assert status == 200
    for host in ("evil.example.com", f"evil.example.com:{proc.port}", f"127.0.0.1.evil.example.com:{proc.port}"):
        status, _, page = http_request(proc.port, "GET", "/crm", headers={"Cookie": cookie, "Host": host})
        assert status == 403, host
    status, headers, _ = http_request(proc.port, "GET", f"/?token={proc.token}", headers={"Host": "evil.example.com"})
    assert status == 403 and not header_values(headers, "Set-Cookie")


def test_the_server_listens_on_loopback_only(ui):
    if shutil.which("lsof") is None:
        pytest.skip("lsof is not available")
    proc = ui()
    wait_for_server(proc.port)
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{proc.port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    assert f"127.0.0.1:{proc.port}" in out, out
    assert f"*:{proc.port}" not in out and f"0.0.0.0:{proc.port}" not in out, out


def test_a_busy_port_given_with_port_is_one_line_and_exit_code_2(tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        port = blocker.getsockname()[1]
        ws = tmp_path / "busy-ws"
        ws.mkdir()
        result = run_cli(["ui", "--workspace", str(ws), "--no-browser", "--port", str(port)], cwd=Path.cwd(),
                         home=Path(os.environ["HOME"]), timeout=40)
    finally:
        blocker.close()
    assert result.returncode == 2
    lines = [l for l in (result.stdout + result.stderr).splitlines() if l.strip()]
    assert len(lines) == 1, lines
    assert "Traceback" not in lines[0]


@pytest.mark.parametrize("value", ["70000", "-1"])
def test_an_impossible_port_exits_with_code_2_and_no_traceback(tmp_path, value):
    ws = tmp_path / "bad-port-ws"
    ws.mkdir()
    result = run_cli(["ui", "--workspace", str(ws), "--no-browser", "--port", value], cwd=Path.cwd(),
                     home=Path(os.environ["HOME"]), timeout=40)
    assert result.returncode == 2
    assert "Traceback" not in result.stdout + result.stderr


def test_without_port_the_next_free_port_is_used_when_the_default_is_busy(tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", 8765))
    except OSError:
        blocker.close()
        pytest.skip("port 8765 is in use on this machine, cannot stage the busy default")
    blocker.listen(1)
    proc = None
    try:
        ws = tmp_path / "fallback-ws"
        ws.mkdir()
        proc = UiProcess(ws, Path(os.environ["HOME"]), Path.cwd())
        proc.wait_for_link()
        assert 8766 <= proc.port <= 8785, proc.port
    finally:
        blocker.close()
        if proc:
            proc.kill()


def test_ctrl_c_stops_the_server_with_exit_code_0_and_no_traceback(ui):
    proc = ui()
    wait_for_server(proc.port)
    code = proc.stop(signal.SIGINT)
    assert code == 0
    assert "Traceback" not in proc.drain()


def test_without_flask_the_command_prints_the_install_hint_and_exits_2(tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text("import sys\nsys.modules['flask'] = None\n", encoding="utf-8")
    ws = tmp_path / "noflask-ws"
    ws.mkdir()
    result = run_cli(["ui", "--workspace", str(ws), "--no-browser"], cwd=Path.cwd(), home=Path(os.environ["HOME"]),
                     extra_env={"PYTHONPATH": f"{shim}{os.pathsep}{REPO_ROOT}"}, timeout=40)
    assert result.returncode == 2
    assert 'The web app needs Flask. Run: pip install -e ".[web]"' in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_starting_the_server_creates_the_secret_inside_the_workspace_and_nothing_in_home(ui, tmp_path):
    proc = ui()
    wait_for_server(proc.port)
    env = read_env(proc.workspace)
    assert re.fullmatch(r"[0-9a-f]{64}", env.get("FLASK_SECRET_KEY", ""))
    assert file_mode(proc.workspace / ".env") == 0o600
    assert list(Path(os.environ["HOME"]).iterdir()) == []
