"""GUI-free logic tests for the Publikreach macOS app.

Written from the public contract only (packaging/mac/README.md plus the
task spec). Uses real sockets on 127.0.0.1, real files and real
subprocesses. Nothing here imports pywebview.
"""

import http.client
import json
import logging
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "packaging" / "mac" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import runtime  # noqa: E402
from vcoutreach.web.gmail import setup as gmail_setup  # noqa: E402

pytestmark = pytest.mark.local_http  # repo conftest: allow 127.0.0.1 sockets only

SECRET = "GOCSPX-unit-test-secret-9f3a"
CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com"


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def good_client(secret=SECRET, client_id=CLIENT_ID):
    return json.dumps(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": secret,
                "project_id": "demo",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    ).encode()


def http_get(base_url, path, cookie=None, host=None, max_redirects=3):
    """Plain GET against the loopback server, follows same-host redirects.

    Returns the final status code.
    """
    netloc = base_url.split("://", 1)[1]
    for _ in range(max_redirects + 1):
        conn = http.client.HTTPConnection(netloc, timeout=10)
        try:
            headers = {}
            if cookie:
                headers["Cookie"] = cookie
            if host:
                headers["Host"] = host
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            resp.read()
            location = resp.getheader("Location")
            if resp.status in (301, 302, 303, 307, 308) and location:
                path = "/" + location.split("://", 1)[-1].split("/", 1)[-1].lstrip("/") \
                    if "://" in location else location
                continue
            return resp.status
        finally:
            conn.close()
    return resp.status


def run_py(code, *args, env_extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), str(APP_DIR)])
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------- free port


class TestFreePort:
    def test_returns_usable_loopback_port(self):
        port = runtime.pick_free_port()
        assert isinstance(port, int) and 1024 <= port <= 65535
        # A real user's server must be able to bind it right away.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))

    def test_never_returns_a_port_someone_else_is_listening_on(self):
        busy = []
        try:
            for _ in range(5):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", 0))
                s.listen(1)
                busy.append(s)
            busy_ports = {s.getsockname()[1] for s in busy}
            for _ in range(25):
                assert runtime.pick_free_port() not in busy_ports
        finally:
            for s in busy:
                s.close()

    def test_two_apps_get_working_ports_in_a_row(self):
        for _ in range(10):
            port = runtime.pick_free_port()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))


# ------------------------------------------------------ workspace bootstrap


class TestBootstrapWorkspace:
    def test_creates_layout_under_a_new_root(self, tmp_path):
        root = tmp_path / "Documents" / "Publikreach"
        runtime.bootstrap_workspace(root)
        assert (root / ".env").is_file()
        for name in ("data", "drafts", "outputs"):
            assert (root / name).is_dir()

    def test_env_file_is_owner_only(self, tmp_path):
        root = tmp_path / "ws"
        runtime.bootstrap_workspace(root)
        assert mode_of(root / ".env") == 0o600

    def test_second_launch_does_not_clobber_user_files(self, tmp_path):
        root = tmp_path / "ws"
        runtime.bootstrap_workspace(root)
        (root / ".env").write_text("MY_SETTING=keep-me\n")
        os.chmod(root / ".env", 0o600)
        (root / "drafts" / "acme.md").write_text("hello Acme\n")
        (root / "data" / "contacts.csv").write_text("a,b\n1,2\n")
        (root / "outputs" / "report.txt").write_text("done\n")

        runtime.bootstrap_workspace(root)
        after_first_relaunch = (root / ".env").read_text()
        runtime.bootstrap_workspace(root)

        env_text = (root / ".env").read_text()
        # the user's own line survives; any safety default is only appended
        assert env_text.startswith("MY_SETTING=keep-me\n")
        # further launches change nothing (no duplicated defaults)
        assert env_text == after_first_relaunch
        assert (root / "drafts" / "acme.md").read_text() == "hello Acme\n"
        assert (root / "data" / "contacts.csv").read_text() == "a,b\n1,2\n"
        assert (root / "outputs" / "report.txt").read_text() == "done\n"
        assert mode_of(root / ".env") == 0o600

    def test_idempotent_listing_is_stable(self, tmp_path):
        root = tmp_path / "ws"
        runtime.bootstrap_workspace(root)
        first = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        runtime.bootstrap_workspace(root)
        second = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        assert first == second

    def test_preexisting_empty_folder_is_adopted(self, tmp_path):
        root = tmp_path / "ws"
        root.mkdir()
        (root / "data").mkdir()
        (root / "data" / "keep.txt").write_text("x")
        runtime.bootstrap_workspace(root)
        assert (root / "data" / "keep.txt").read_text() == "x"
        assert (root / ".env").is_file()

    def test_default_root_is_documents_publikreach_in_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        runtime.bootstrap_workspace()
        root = tmp_path / "Documents" / "Publikreach"
        assert (root / ".env").is_file()
        assert (root / "data").is_dir()


# ---------------------------------------------------------- single instance

HOLD_LOCK = """
    import sys, time
    import runtime
    lock = runtime.SingleInstanceLock(sys.argv[1])
    ok = lock.acquire()
    print("HELD" if ok else "DENIED", flush=True)
    if ok:
        time.sleep(float(sys.argv[2]))
        lock.release()
"""

CONTEND = """
    import sys, time
    import runtime
    start_at = float(sys.argv[2])
    while time.time() < start_at:
        pass
    lock = runtime.SingleInstanceLock(sys.argv[1])
    ok = lock.acquire()
    print("WON" if ok else "LOST", flush=True)
    if ok:
        time.sleep(3.0)
        lock.release()
"""


class TestSingleInstanceLock:
    def test_second_acquire_fails_cleanly_while_first_holds(self, tmp_path):
        path = tmp_path / "app.lock"
        first = runtime.SingleInstanceLock(path)
        second = runtime.SingleInstanceLock(path)
        assert first.acquire()
        try:
            try:
                got = second.acquire()
            except Exception as exc:  # clean failure may also be an exception
                got = False
                assert not isinstance(exc, (AttributeError, NameError, TypeError))
            assert not got
        finally:
            first.release()

    def test_release_frees_it_for_the_next_launch(self, tmp_path):
        path = tmp_path / "app.lock"
        a = runtime.SingleInstanceLock(path)
        b = runtime.SingleInstanceLock(path)
        assert a.acquire()
        a.release()
        assert b.acquire()
        b.release()

    def test_release_without_acquire_or_twice_does_not_blow_up(self, tmp_path):
        lock = runtime.SingleInstanceLock(tmp_path / "app.lock")
        lock.release()
        assert lock.acquire()
        lock.release()
        lock.release()

    def test_context_manager_holds_then_frees(self, tmp_path):
        path = tmp_path / "app.lock"
        probe = runtime.SingleInstanceLock(path)
        with runtime.SingleInstanceLock(path):
            assert not probe.acquire()
        assert probe.acquire()
        probe.release()

    def test_other_process_cannot_start_while_we_hold_it(self, tmp_path):
        path = tmp_path / "app.lock"
        mine = runtime.SingleInstanceLock(path)
        assert mine.acquire()
        try:
            proc = run_py(HOLD_LOCK, path, 0)
            out, err = proc.communicate(timeout=30)
            assert out.strip() == "DENIED", err
        finally:
            mine.release()
        proc = run_py(HOLD_LOCK, path, 0)
        out, err = proc.communicate(timeout=30)
        assert out.strip() == "HELD", err

    def test_second_process_is_refused_then_allowed_after_first_exits(self, tmp_path):
        path = tmp_path / "app.lock"
        holder = run_py(HOLD_LOCK, path, 600)
        try:
            assert holder.stdout.readline().strip() == "HELD"
            late = run_py(HOLD_LOCK, path, 0)
            out, err = late.communicate(timeout=30)
            assert out.strip() == "DENIED", err
        finally:
            holder.terminate()
            holder.wait(timeout=30)
        after = run_py(HOLD_LOCK, path, 0)
        out, err = after.communicate(timeout=30)
        assert out.strip() == "HELD", err

    def test_crashed_holder_does_not_wedge_the_app_forever(self, tmp_path):
        path = tmp_path / "app.lock"
        holder = run_py(HOLD_LOCK, path, 600)
        try:
            assert holder.stdout.readline().strip() == "HELD"
            os.kill(holder.pid, signal.SIGKILL)
        finally:
            holder.wait(timeout=30)
        fresh = runtime.SingleInstanceLock(path)
        assert fresh.acquire()
        fresh.release()

    def test_stale_lock_file_from_dead_pid_is_recoverable(self, tmp_path):
        path = tmp_path / "app.lock"
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=30)
        path.write_text(f"{dead.pid}\n")
        lock = runtime.SingleInstanceLock(path)
        assert lock.acquire()
        lock.release()

    def test_garbage_lock_file_is_recoverable(self, tmp_path):
        path = tmp_path / "app.lock"
        path.write_bytes(b"\x00\xffnot a pid")
        lock = runtime.SingleInstanceLock(path)
        assert lock.acquire()
        lock.release()

    def test_six_simultaneous_launches_exactly_one_wins(self, tmp_path):
        path = tmp_path / "app.lock"
        start_at = time.time() + 2.0
        procs = [run_py(CONTEND, path, start_at) for _ in range(6)]
        results = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            results.append(out.strip() or f"ERR:{err}")
        assert results.count("WON") == 1, results
        assert results.count("LOST") == 5, results


# ----------------------------------------------------------- token exchange


@pytest.fixture
def server(tmp_path):
    ws = runtime.bootstrap_workspace(tmp_path / "ws")
    srv = runtime.start_server(ws.root)
    try:
        yield srv
    finally:
        srv.stop()


class TestTokenExchange:
    def test_server_listens_on_loopback_only(self, server):
        assert server.base_url.startswith("http://127.0.0.1:")
        assert http_get(server.base_url, "/healthz") == 200

    def test_pages_refuse_visitors_without_a_session(self, server):
        assert http_get(server.base_url, "/") in (401, 403)

    def test_exchange_yields_a_cookie_that_opens_the_app(self, server):
        cookie = runtime.exchange_token(server.app, server.base_url)
        assert cookie.name and cookie.value and cookie.csrf
        assert http_get(server.base_url, "/", f"{cookie.name}={cookie.value}") == 200

    def test_forged_cookie_value_is_refused(self, server):
        cookie = runtime.exchange_token(server.app, server.base_url)
        forged = cookie.value[:-4] + ("AAAA" if not cookie.value.endswith("AAAA") else "BBBB")
        assert http_get(server.base_url, "/", f"{cookie.name}={forged}") in (401, 403)

    def test_token_cannot_be_used_twice(self, server):
        first = runtime.exchange_token(server.app, server.base_url)
        with pytest.raises(Exception) as caught:
            runtime.exchange_token(server.app, server.base_url)
        # the failure must not hand the live session value back out
        assert first.value not in str(caught.value)
        assert first.csrf not in str(caught.value)

    def test_each_launch_gets_its_own_independent_session(self, tmp_path):
        servers = []
        try:
            cookies = []
            for i in range(2):
                ws = runtime.bootstrap_workspace(tmp_path / f"ws{i}")
                srv = runtime.start_server(ws.root)
                servers.append(srv)
                cookies.append(runtime.exchange_token(srv.app, srv.base_url))
            assert cookies[0].value != cookies[1].value
            assert servers[0].base_url != servers[1].base_url
            # a session from app A is not valid on app B
            a, b = cookies
            assert http_get(servers[1].base_url, "/", f"{a.name}={a.value}") in (401, 403)
            assert http_get(servers[1].base_url, "/", f"{b.name}={b.value}") == 200
        finally:
            for s in servers:
                s.stop()

    def test_host_header_guard_blocks_dns_rebinding_style_requests(self, server):
        cookie = runtime.exchange_token(server.app, server.base_url)
        status = http_get(
            server.base_url, "/", f"{cookie.name}={cookie.value}", host="evil.example"
        )
        assert status != 200

    def test_stop_actually_closes_the_port(self, tmp_path):
        ws = runtime.bootstrap_workspace(tmp_path / "ws")
        srv = runtime.start_server(ws.root)
        port = int(srv.base_url.rsplit(":", 1)[1])
        srv.stop()
        srv.stop()  # a second stop (window close plus quit) must be harmless
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            assert s.connect_ex(("127.0.0.1", port)) != 0


# --------------------------------------------------------- OAuth client file


@pytest.fixture
def workspace(tmp_path):
    return runtime.bootstrap_workspace(tmp_path / "ws")


def creds_path(ws):
    return ws.root / "data" / ".gmail_credentials.json"


def web_client():
    return json.dumps(
        {"web": {"client_id": CLIENT_ID, "client_secret": SECRET,
                 "redirect_uris": ["https://example.com/cb"]}}
    ).encode()


def bad_inputs():
    return {
        "not_json": b"this is { not json, secret " + SECRET.encode(),
        "web_client": web_client(),
        "service_account": json.dumps(
            {"type": "service_account", "client_id": CLIENT_ID,
             "private_key": SECRET}).encode(),
        "json_list": b"[1, 2, 3]",
        "json_string": b'"installed"',
        "empty_object": b"{}",
        "installed_not_object": json.dumps({"installed": SECRET}).encode(),
        "missing_client_id": json.dumps(
            {"installed": {"client_secret": SECRET}}).encode(),
        "missing_client_secret": json.dumps(
            {"installed": {"client_id": CLIENT_ID}}).encode(),
        "empty_client_id": json.dumps(
            {"installed": {"client_id": "", "client_secret": SECRET}}).encode(),
        "empty_client_secret": json.dumps(
            {"installed": {"client_id": CLIENT_ID, "client_secret": ""}}).encode(),
        "zero_bytes": b"",
        "whitespace_only": b"   \n  ",
        "oversized": good_client() + b" " * (6 * 1024 * 1024),
        "binary_blob": bytes(range(256)) * 50,
    }


class TestOAuthClientImport:
    def test_valid_desktop_client_is_accepted(self):
        result = gmail_setup.validate_oauth_client(good_client())
        assert result is not None

    def test_store_writes_credentials_into_workspace_data(self, workspace):
        gmail_setup.store_oauth_client(good_client(), workspace)
        target = creds_path(workspace)
        assert target.is_file()
        saved = json.loads(target.read_text())
        assert saved["installed"]["client_id"] == CLIENT_ID
        assert saved["installed"]["client_secret"] == SECRET

    def test_stored_credentials_are_owner_only(self, workspace):
        gmail_setup.store_oauth_client(good_client(), workspace)
        assert mode_of(creds_path(workspace)) == 0o600

    def test_overwriting_a_loose_permission_file_tightens_it(self, workspace):
        target = creds_path(workspace)
        target.write_text("old")
        os.chmod(target, 0o644)
        gmail_setup.store_oauth_client(good_client(), workspace)
        assert mode_of(target) == 0o600

    def test_copy_from_a_downloaded_file_in_downloads(self, workspace, tmp_path):
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        src = downloads / "client_secret_1234.json"
        src.write_bytes(good_client())
        os.chmod(src, 0o644)
        gmail_setup.copy_oauth_client(src, workspace)
        target = creds_path(workspace)
        assert json.loads(target.read_text())["installed"]["client_id"] == CLIENT_ID
        assert mode_of(target) == 0o600
        # the user's original download is left alone
        assert src.read_bytes() == good_client()

    def test_importing_a_new_client_replaces_the_old_one(self, workspace):
        gmail_setup.store_oauth_client(good_client(secret="old-secret-value"), workspace)
        gmail_setup.store_oauth_client(good_client(secret="new-secret-value"), workspace)
        saved = json.loads(creds_path(workspace).read_text())
        assert saved["installed"]["client_secret"] == "new-secret-value"

    @pytest.mark.parametrize("name", sorted(bad_inputs()))
    def test_rejects_bad_files_and_writes_nothing(self, workspace, name):
        raw = bad_inputs()[name]
        with pytest.raises(Exception):
            gmail_setup.store_oauth_client(raw, workspace)
        assert not creds_path(workspace).exists()

    @pytest.mark.parametrize("name", sorted(bad_inputs()))
    def test_validate_rejects_bad_files(self, name):
        with pytest.raises(Exception):
            gmail_setup.validate_oauth_client(bad_inputs()[name])

    @pytest.mark.parametrize("name", sorted(bad_inputs()))
    def test_rejection_does_not_echo_the_secret(self, workspace, name, caplog):
        caplog.set_level(logging.DEBUG)
        raw = bad_inputs()[name]
        with pytest.raises(Exception) as caught:
            gmail_setup.store_oauth_client(raw, workspace)
        shown = f"{caught.value!s} {caught.value!r} {caught.type.__name__}"
        assert SECRET not in shown
        assert SECRET not in caplog.text

    def test_rejected_import_keeps_the_previous_good_client(self, workspace):
        gmail_setup.store_oauth_client(good_client(), workspace)
        before = creds_path(workspace).read_bytes()
        with pytest.raises(Exception):
            gmail_setup.store_oauth_client(web_client(), workspace)
        assert creds_path(workspace).read_bytes() == before
        assert mode_of(creds_path(workspace)) == 0o600

    def test_success_return_values_and_logs_do_not_contain_the_secret(
        self, workspace, tmp_path, caplog
    ):
        caplog.set_level(logging.DEBUG)
        src = tmp_path / "client.json"
        src.write_bytes(good_client())
        returned = [
            gmail_setup.store_oauth_client(good_client(), workspace),
            gmail_setup.copy_oauth_client(src, workspace),
        ]
        for value in returned:
            assert SECRET not in f"{value!s} {value!r}"
        assert SECRET not in caplog.text

    def test_copy_of_a_missing_file_fails_without_creating_credentials(
        self, workspace, tmp_path
    ):
        with pytest.raises(Exception) as caught:
            gmail_setup.copy_oauth_client(tmp_path / "nope.json", workspace)
        assert SECRET not in str(caught.value)
        assert not creds_path(workspace).exists()

    def test_copy_of_a_web_client_file_is_rejected(self, workspace, tmp_path):
        src = tmp_path / "web.json"
        src.write_bytes(web_client())
        with pytest.raises(Exception) as caught:
            gmail_setup.copy_oauth_client(src, workspace)
        assert SECRET not in f"{caught.value!s} {caught.value!r}"
        assert not creds_path(workspace).exists()


# ---------------------------------------------------------------- self test


class TestSelfTestFlag:
    def test_headless_self_test_exits_zero(self, tmp_path):
        fake_home = tmp_path / "selftest_home"
        fake_home.mkdir()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        env["HOME"] = str(fake_home)
        proc = subprocess.run(
            [sys.executable, str(APP_DIR / "app_main.py"), "--self-test"],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(tmp_path),
        )
        combined = proc.stdout + proc.stderr
        if proc.returncode != 0 and "No module named 'webview'" in combined:
            pytest.skip("app_main --self-test imports pywebview, which is not installed")
        assert proc.returncode == 0, combined[-2000:]
        # self-test must use a throwaway workspace, not the user's real Documents
        assert not (fake_home / "Documents" / "Publikreach").exists()

    def test_self_test_with_explicit_workspace_leaves_loopback_free(self, tmp_path):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        proc = subprocess.run(
            [sys.executable, str(APP_DIR / "app_main.py"), "--self-test",
             "--workspace", str(tmp_path / "ws")],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(tmp_path),
        )
        combined = proc.stdout + proc.stderr
        if proc.returncode != 0 and "No module named 'webview'" in combined:
            pytest.skip("app_main --self-test imports pywebview, which is not installed")
        assert proc.returncode == 0, combined[-2000:]
        assert (tmp_path / "ws" / "data").is_dir()


# ------------------------------------------------------------------ hygiene

SHIPPED = [
    APP_DIR / "app_main.py",
    APP_DIR / "runtime.py",
    APP_DIR / "make_icon.py",
    APP_DIR / "Publikreach.spec",
    REPO / "packaging" / "mac" / "build_app.sh",
    REPO / "packaging" / "mac" / "uninstall-mac.sh",
    REPO / "packaging" / "mac" / "README.md",
    REPO / "vcoutreach" / "web" / "gmail" / "setup.py",
]
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
HOME_PREFIX = "/" + "Users" + "/"


class TestShippedFileHygiene:
    @pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
    def test_no_em_or_en_dashes(self, path):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            assert EM_DASH not in line and EN_DASH not in line, f"{path.name}:{lineno}"

    @pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
    def test_no_developer_home_paths(self, path):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            assert HOME_PREFIX not in line, f"{path.name}:{lineno}"

    def test_no_dashes_or_home_paths_in_this_test_file(self):
        text = Path(__file__).read_text(encoding="utf-8")
        assert EM_DASH not in text and EN_DASH not in text
        assert HOME_PREFIX not in text
