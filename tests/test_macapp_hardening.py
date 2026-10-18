"""Spec-only hardening checks using public APIs, real files and loopback HTTP."""

import errno
import fcntl
import http.client
import http.server
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from flask import Response
from werkzeug.serving import make_server

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "packaging" / "mac" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import runtime  # noqa: E402
from vcoutreach.web.gmail import scheduler, setup as gmail_setup  # noqa: E402

pytestmark = pytest.mark.local_http
SECRET = "GOCSPX-unit-test-secret-9f3a"
CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com"


def good_client():
    return json.dumps({"installed": {
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "project_id": "demo",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}).encode()


@pytest.fixture
def workspace(tmp_path):
    return runtime.bootstrap_workspace(tmp_path / "ws")


@pytest.fixture
def server(workspace):
    srv = runtime.start_server(workspace.root)
    try:
        yield srv
    finally:
        srv.stop()


def open_fds():
    result = set()
    for name in os.listdir("/dev/fd"):
        try:
            fd = int(name)
            os.fstat(fd)
        except (ValueError, OSError):
            continue
        result.add(fd)
    return result


def denied(lock):
    try:
        return not lock.acquire()
    except (OSError, RuntimeError, ValueError):
        return True


def port_of(srv):
    return urlsplit(srv.base_url).port


def assert_bindable(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
        probe.listen(1)


def get(base_url, path):
    conn = http.client.HTTPConnection(urlsplit(base_url).netloc, timeout=1)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("existing", [None, "SEND_MODE=live\n"], ids=["new", "was-live"])
def test_01_bootstrap_forces_test_mode(tmp_path, existing):
    root = tmp_path / "bootstrap"
    if existing is not None:
        root.mkdir()
        (root / ".env").write_text(existing)
    runtime.bootstrap_workspace(root)
    values = [line.split("=", 1)[1].strip().strip("\"'")
              for line in (root / ".env").read_text().splitlines()
              if line.split("=", 1)[0].strip() == "SEND_MODE"]
    assert values and values[-1] == "test"


def test_02_free_port_is_bindable():
    port = runtime.pick_free_port()
    assert isinstance(port, int) and 0 < port <= 65535
    assert_bindable(port)


class RecordingSocket:
    def __init__(self, addresses, selected, *args, **kwargs):
        self.socket = socket.socket(*args, **kwargs)
        self.addresses = addresses
        self.selected = selected

    def bind(self, address):
        self.addresses.append(address)
        self.socket.bind(address)
        self.selected.append(self.socket.getsockname()[1])

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.socket.close()


def test_03_probe_binds_only_ipv4_loopback():
    addresses, selected = [], []
    runtime.pick_free_port(socket_factory=lambda *a, **kw: RecordingSocket(
        addresses, selected, *a, **kw))
    assert addresses and all(address[0] == "127.0.0.1" for address in addresses)


def test_04_ports_are_dynamic_and_not_incremented():
    addresses, selected, ports = [], [], []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        for _ in range(20):
            before = len(selected)
            port = runtime.pick_free_port(socket_factory=lambda *a, **kw: RecordingSocket(
                addresses, selected, *a, **kw))
            assert len(selected) > before
            # Compare to the kernel's choice, avoiding random adjacent-port collisions.
            assert port == selected[-1]
            assert port != busy.getsockname()[1]
            ports.append(port)
    assert len(set(ports)) >= 2


def test_05_lock_created_private_even_with_zero_umask(tmp_path):
    path = tmp_path / "private.lock"
    lock = runtime.SingleInstanceLock(path)
    previous = os.umask(0)
    try:
        assert lock.acquire()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(previous)
        lock.release()


def test_06_lock_rejects_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"do not change")
    target.chmod(0o644)
    before = target.stat()
    path = tmp_path / "symlink.lock"
    path.symlink_to(target)
    lock = runtime.SingleInstanceLock(path)
    try:
        assert denied(lock)
        assert path.is_symlink()
        assert target.read_bytes() == b"do not change"
        after = target.stat()
        assert (after.st_mode, after.st_mtime_ns) == (before.st_mode, before.st_mtime_ns)
    finally:
        lock.release()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_07_lock_rejects_nonregular_files_cleanly(tmp_path, kind):
    path = tmp_path / "nonregular.lock"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import runtime
lock = runtime.SingleInstanceLock(sys.argv[2])
try:
    try:
        result = lock.acquire()
    except (OSError, RuntimeError, ValueError):
        result = False
    assert not result, 'nonregular lock was acquired'
finally:
    lock.release()
"""
    proc = subprocess.run([sys.executable, "-c", code, str(APP_DIR), str(path)],
                          capture_output=True, text=True, timeout=3,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                               "PYTHONPATH": str(REPO)})
    assert proc.returncode == 0, proc.stderr


def test_08_contended_acquire_does_not_leak_descriptors(tmp_path):
    path = tmp_path / "contended.lock"
    holder = runtime.SingleInstanceLock(path)
    contenders = [runtime.SingleInstanceLock(path) for _ in range(20)]
    assert holder.acquire()
    try:
        before = open_fds()
        for lock in contenders:
            assert denied(lock)
        assert open_fds() == before
    finally:
        for lock in contenders:
            lock.release()
        holder.release()


def test_09_existing_lock_permissions_are_tightened(tmp_path):
    path = tmp_path / "loose.lock"
    path.write_text("old lock")
    path.chmod(0o644)
    lock = runtime.SingleInstanceLock(path)
    try:
        assert lock.acquire()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        lock.release()


def test_10_holder_acquire_is_idempotent(tmp_path):
    path = tmp_path / "idempotent.lock"
    holder, other = runtime.SingleInstanceLock(path), runtime.SingleInstanceLock(path)
    try:
        assert holder.acquire()
        assert holder.acquire() is True
        assert denied(other)
    finally:
        other.release()
        holder.release()


def test_11_release_unlocks_and_closes_descriptor(tmp_path):
    path = tmp_path / "release.lock"
    first, second = runtime.SingleInstanceLock(path), runtime.SingleInstanceLock(path)
    before = open_fds()
    try:
        assert first.acquire()
        acquired = open_fds()
        assert len(acquired) > len(before)
        first.release()
        assert open_fds() == before
        for fd in acquired - before:
            with pytest.raises(OSError):
                os.fstat(fd)
        assert second.acquire()
    finally:
        first.release()
        second.release()


def test_12_context_manager_enforces_and_releases_lock(tmp_path):
    path = tmp_path / "context.lock"
    holder = runtime.SingleInstanceLock(path)
    assert holder.acquire()
    try:
        with pytest.raises((OSError, RuntimeError, ValueError)):
            with runtime.SingleInstanceLock(path):
                pytest.fail("contended context entered")
    finally:
        holder.release()
    other = runtime.SingleInstanceLock(path)
    try:
        with runtime.SingleInstanceLock(path):
            assert denied(other)
        assert other.acquire()
    finally:
        other.release()


def test_13_post_open_acquire_error_closes_descriptor(tmp_path, monkeypatch):
    original = fcntl.flock
    opened = []

    def fail_lock(fd, operation):
        if operation & fcntl.LOCK_EX:
            os.fstat(fd)  # Verify the injected error occurs after a real open.
            opened.append(fd)
            raise OSError(errno.EIO, "injected lock failure")
        return original(fd, operation)

    monkeypatch.setattr(fcntl, "flock", fail_lock)
    locks = [runtime.SingleInstanceLock(tmp_path / "error.lock") for _ in range(20)]
    before = open_fds()
    try:
        for lock in locks:
            assert denied(lock)
        assert len(opened) == 20
        assert open_fds() == before
    finally:
        for lock in locks:
            lock.release()


@contextmanager
def canned_exchange(app, monkeypatch, *, location="/", omit_attribute=None,
                    append_attribute="", missing_cookie=False):
    """Feed canned wire responses through the app's public test-client boundary.

    The real Flask client maintains the authenticated session and CSRF state.
    Only the exchange response comes from the tiny local HTTP server.
    """
    requests, wire_cookie = [], []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            initial_exchange = len(requests) == 1
            self.send_response(302 if initial_exchange else 200)
            if initial_exchange:
                self.send_header("Location", location)
                if not missing_cookie:
                    self.send_header("Set-Cookie", wire_cookie[0])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    original_client = app.test_client

    class WireClient:
        def __init__(self):
            self.client = original_client()

        def __enter__(self):
            self.client.__enter__()
            return self

        def __exit__(self, *args):
            return self.client.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.client, name)

        def get(self, path, *args, **kwargs):
            original_response = self.client.get(path, *args, **kwargs)
            if not wire_cookie:
                cookie = original_response.headers.get("Set-Cookie")
                assert original_response.status_code == 302 and cookie
                assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
                if omit_attribute is not None:
                    cookie = cookie.replace("; " + omit_attribute, "")
                if append_attribute:
                    cookie += "; " + append_attribute
                wire_cookie.append(cookie)
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=1)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                return Response(response.read(), status=response.status,
                                headers=response.getheaders())
            finally:
                conn.close()

    monkeypatch.setattr(app, "test_client", WireClient)
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1)


@pytest.fixture
def exchange_app(workspace):
    # The public server_factory seam supplies the real app without another listener.
    apps = []

    class Captured(Exception):
        pass

    def capture(host, port, app, **kwargs):
        apps.append(app)
        raise Captured()

    with pytest.raises(Captured):
        runtime.start_server(workspace.root, server_factory=capture)
    return apps[0]


@pytest.mark.parametrize("location", ["/destination-one", "/", "http://evil.example/"],
                         ids=["other-path", "root-path", "absolute-remote"])
def test_14_exchange_only_accepts_root_redirect(exchange_app, monkeypatch, location):
    # Spec correction: the session exchange must land on "/" and nowhere else.
    with canned_exchange(exchange_app, monkeypatch, location=location) as (base, requests):
        if location == "/":
            assert runtime.exchange_token(exchange_app, base) is not None
        else:
            with pytest.raises((ValueError, RuntimeError)):
                runtime.exchange_token(exchange_app, base)
        assert requests


def test_15_exchange_rejects_cookie_without_httponly(exchange_app, monkeypatch):
    with canned_exchange(exchange_app, monkeypatch, omit_attribute="HttpOnly") as (base, requests):
        with pytest.raises((ValueError, RuntimeError)):
            runtime.exchange_token(exchange_app, base)
        assert requests


@pytest.mark.parametrize("attribute", ["", "SameSite=Lax", "SameSite=None"],
                         ids=["missing", "lax", "none"])
def test_16_exchange_rejects_cookie_without_strict_samesite(exchange_app, monkeypatch, attribute):
    with canned_exchange(exchange_app, monkeypatch,
                         omit_attribute="SameSite=Strict",
                         append_attribute=attribute) as (base, requests):
        with pytest.raises((ValueError, RuntimeError)):
            runtime.exchange_token(exchange_app, base)
        assert requests


def test_17_exchange_rejects_missing_cookie(exchange_app, monkeypatch):
    with canned_exchange(exchange_app, monkeypatch, missing_cookie=True) as (base, requests):
        with pytest.raises((ValueError, RuntimeError)):
            runtime.exchange_token(exchange_app, base)
        assert requests


def test_exchange_control_accepts_protected_cookie(exchange_app, monkeypatch):
    with canned_exchange(exchange_app, monkeypatch) as (base, requests):
        cookie = runtime.exchange_token(exchange_app, base)
        assert cookie.name and cookie.value and cookie.csrf
        assert requests


def test_18_server_stop_is_idempotent(server):
    server.stop()
    server.stop()


def test_19_stopped_server_port_is_immediately_reusable(server):
    port = port_of(server)
    server.stop()
    assert_bindable(port)


def test_20_stopped_server_cannot_answer_http(server):
    assert get(server.base_url, "/healthz")[0] == 200
    server.stop()
    with pytest.raises((OSError, http.client.HTTPException)):
        get(server.base_url, "/healthz")


def test_21_stopped_server_thread_has_exited(server):
    assert server.thread.is_alive()
    server.stop()
    assert not server.thread.is_alive()


def test_22_stop_calls_scheduler_stop(server, monkeypatch):
    calls = []
    original = scheduler.stop_scheduler

    def stop():
        calls.append(True)
        return original()

    monkeypatch.setattr(scheduler, "stop_scheduler", stop)
    server.stop()
    assert calls


def test_23_server_thread_is_daemon(server):
    assert server.thread.daemon is True


def test_24_app_port_matches_bound_listener(server):
    assert server.app.config["PORT"] == port_of(server)
    assert server.app.config["PORT"] == server.server.server_port


def test_25_request_token_is_not_logged(server, capfd, caplog):
    token = "hardening-private-token-9428"
    caplog.set_level(logging.DEBUG)
    get(server.base_url, "/healthz?token=" + token)
    server.stop()  # Join before checking asynchronous request logs.
    assert token not in capfd.readouterr().err
    assert token not in caplog.text


def test_26_thread_start_failure_closes_listener(workspace, monkeypatch):
    bound = []

    def factory(*args, **kwargs):
        listener = make_server(*args, **kwargs)
        bound.append((listener, listener.server_port))
        return listener

    def fail_start(self):
        raise RuntimeError("injected thread start failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        with pytest.raises(RuntimeError, match="injected thread start failure"):
            runtime.start_server(workspace.root, server_factory=factory)
        assert bound
        for listener, port in bound:
            assert_bindable(port)
    finally:
        for listener, port in bound:
            listener.server_close()


def self_test_failed(root):
    try:
        result = runtime.self_test(root)
    except (Exception, SystemExit):
        return True
    return result is not None and result != 0


def intercept_self_test(monkeypatch, mutate):
    """Modify actual WSGI responses, leaving real HTTP and runtime shutdown intact."""
    original = runtime.start_server
    servers, touched = [], []

    def start(*args, **kwargs):
        srv = original(*args, **kwargs)
        servers.append(srv)
        wrapped = srv.app.wsgi_app

        def middleware(environ, start_response):
            replacement = mutate(environ)
            if replacement is not None:
                touched.append(environ["PATH_INFO"])
                status, body = replacement
                start_response(status, [("Content-Type", "text/plain"),
                                        ("Content-Length", str(len(body)))])
                return [body]
            return wrapped(environ, start_response)

        srv.app.wsgi_app = middleware
        return srv

    monkeypatch.setattr(runtime, "start_server", start)
    return servers, touched


@pytest.mark.parametrize("status", ["200 OK", "401 Unauthorized", "404 Not Found"])
def test_27_self_test_requires_exactly_403_for_cookieless_page(workspace, monkeypatch, status):
    servers, touched = intercept_self_test(monkeypatch, lambda env:
        (status, b"blocked incorrectly")
        if env["PATH_INFO"] in ("/", "/crm", "/settings", "/replies")
        and not env.get("HTTP_COOKIE") and not env.get("QUERY_STRING") else None)
    try:
        assert self_test_failed(workspace.root)
        assert touched, "self_test never checked the cookieless protected page"
    finally:
        for srv in servers:
            srv.stop()


@pytest.mark.parametrize("body", [b'{"ok": false}', b'{"wrong": true}'],
                         ids=["not-ok", "missing-ok"])
def test_28_self_test_rejects_wrong_health_body(workspace, monkeypatch, body):
    servers, touched = intercept_self_test(monkeypatch, lambda env:
        ("200 OK", body) if env["PATH_INFO"] == "/healthz" else None)
    try:
        assert self_test_failed(workspace.root)
        assert touched
    finally:
        for srv in servers:
            srv.stop()


@pytest.mark.parametrize("page", ["static", "replies"])
def test_29_self_test_requires_assets_and_replies(workspace, monkeypatch, page):
    def mutate(env):
        path = env["PATH_INFO"]
        if (page == "static" and path.startswith("/static/")) or (
                page == "replies" and path.rstrip("/") == "/replies"):
            return "500 Internal Server Error", b"injected page failure"

    servers, touched = intercept_self_test(monkeypatch, mutate)
    try:
        assert self_test_failed(workspace.root)
        assert touched, f"self_test did not request {page}"
    finally:
        for srv in servers:
            srv.stop()


@pytest.mark.parametrize("fail", [False, True], ids=["pass", "fail"])
def test_30_self_test_always_stops_its_server(workspace, monkeypatch, fail):
    servers, touched = intercept_self_test(monkeypatch, lambda env:
        ("500 Internal Server Error", b"health failed")
        if fail and env["PATH_INFO"] == "/healthz" else None)
    try:
        failed = self_test_failed(workspace.root)
        assert servers, "self_test did not start a server"
        for srv in servers:
            assert_bindable(port_of(srv))
        assert failed is fail
    finally:
        for srv in servers:
            srv.stop()


def test_31_self_test_respects_single_instance_lock(workspace, monkeypatch):
    lock = runtime.SingleInstanceLock(workspace.root / "data" / ".app.lock")
    attempts = []

    def forbidden_start(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("must not start while another holder owns the lock")

    monkeypatch.setattr(runtime, "start_server", forbidden_start)
    assert lock.acquire()
    try:
        assert self_test_failed(workspace.root)
        assert not attempts, "self_test started a server despite the held instance lock"
    finally:
        lock.release()


def changed(field, value):
    parsed = json.loads(good_client())
    parsed["installed"][field] = value
    return json.dumps(parsed).encode()


def missing(field):
    parsed = json.loads(good_client())
    del parsed["installed"][field]
    return json.dumps(parsed).encode()


def test_32_client_size_boundary():
    raw = good_client()
    padded = raw + b" " * (gmail_setup.MAX_CLIENT_BYTES - len(raw))
    assert len(padded) == gmail_setup.MAX_CLIENT_BYTES
    assert gmail_setup.validate_oauth_client(padded) == json.loads(raw)
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(padded + b" ")


@pytest.mark.parametrize("raw", [b"", None, 123, [], {}],
                         ids=["empty-bytes", "none", "integer", "list", "dict"])
def test_33_client_rejects_empty_and_nonbytes(raw):
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(raw)


def test_34_deep_json_raises_valueerror():
    # Parser recursion must surface as the standard ValueError, never RecursionError.
    depth = min(20000, gmail_setup.MAX_CLIENT_BYTES // 2 - 1)
    raw = b"[" * depth + b"]" * depth
    assert len(raw) <= gmail_setup.MAX_CLIENT_BYTES
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(raw)


def test_35_client_rejects_extra_top_level_key():
    parsed = json.loads(good_client())
    parsed["web"] = {}
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(json.dumps(parsed).encode())


@pytest.mark.parametrize("where,value", [
    ("top", []), ("top", "installed"), ("top", 123),
    ("installed", []), ("installed", "client"), ("installed", None),
], ids=["top-list", "top-string", "top-number", "installed-list", "installed-string", "installed-null"])
def test_36_client_requires_objects(where, value):
    parsed = json.loads(good_client())
    if where == "top":
        parsed = value
    else:
        parsed["installed"] = value
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(json.dumps(parsed).encode())


@pytest.mark.parametrize("field", ["client_id", "client_secret"])
@pytest.mark.parametrize("case,value", [("missing", None), ("empty", ""),
                                         ("whitespace", " \t\n"), ("number", 123)],
                         ids=["missing", "empty", "whitespace", "number"])
def test_37_client_requires_nonempty_string_credentials(field, case, value):
    raw = missing(field) if case == "missing" else changed(field, value)
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(raw)


@pytest.mark.parametrize("field", ["auth_uri", "token_uri"])
def test_38_client_rejects_each_wrong_oauth_endpoint(field):
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(changed(field, "https://evil.example.com/oauth"))


def test_39_client_rejects_wrong_certificate_endpoint():
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(changed(
            "auth_provider_x509_cert_url", "https://evil.example.com/certs"))


@pytest.mark.parametrize("present", [False, True], ids=["absent", "correct"])
def test_39_client_accepts_optional_correct_certificate_endpoint(present):
    raw = changed("auth_provider_x509_cert_url", "https://www.googleapis.com/oauth2/v1/certs") \
        if present else good_client()
    assert gmail_setup.validate_oauth_client(raw) == json.loads(raw)


@pytest.mark.parametrize("uris", [
    [], "http://localhost", ["http://localhost", 123],
    ["http://localhost", "http://localhost:abc"], ["https://localhost"],
    ["http://evil.example.com"], ["http://user@localhost:1"],
    ["http://user:pw@localhost:1"], ["http://localhost:1?x=1"],
    ["http://localhost:1#frag"],
], ids=["empty", "non-list", "non-string-entry", "malformed-port-with-valid-entry",
        "https", "non-loopback", "username", "password", "query", "fragment"])
def test_40_client_rejects_invalid_redirect(uris):
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(changed("redirect_uris", uris))


@pytest.mark.parametrize("uri", ["http://localhost", "http://127.0.0.1:8080", "http://[::1]:8080"],
                         ids=["localhost", "ipv4", "ipv6"])
def test_40_client_accepts_loopback_redirect(uri):
    raw = changed("redirect_uris", [uri])
    assert gmail_setup.validate_oauth_client(raw) == json.loads(raw)


def test_41_client_rejects_unknown_installed_key():
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(changed("unknown_field", "unexpected"))


def test_41_client_accepts_project_id():
    raw = changed("project_id", "another-project")
    assert gmail_setup.validate_oauth_client(raw)["installed"]["project_id"] == "another-project"


def test_42_valid_client_returns_independent_parsed_dict():
    raw = good_client()
    first = gmail_setup.validate_oauth_client(raw)
    assert first == json.loads(raw)
    assert first["installed"]["client_id"] == CLIENT_ID
    first["installed"]["client_id"] = "mutated"
    first["installed"]["redirect_uris"].append("http://evil.example.com")
    assert gmail_setup.validate_oauth_client(raw) == json.loads(raw)


def test_43_replacement_client_removes_stale_gmail_token(workspace):
    gmail_setup.store_oauth_client(good_client(), workspace)
    token = workspace.root / "data" / ".gmail_token.json"
    token.write_text('{"refresh_token": "stale-token"}')
    replacement = changed("client_secret", "new-client-secret")
    gmail_setup.store_oauth_client(replacement, workspace)
    assert not token.exists()


def test_43_first_client_needs_no_existing_token(workspace):
    token = workspace.root / "data" / ".gmail_token.json"
    assert not token.exists()
    gmail_setup.store_oauth_client(good_client(), workspace)
    assert (workspace.root / "data" / ".gmail_credentials.json").is_file()


def test_44_stored_credentials_are_private_validated_json(workspace):
    raw = good_client()
    gmail_setup.store_oauth_client(raw, workspace)
    path = workspace.root / "data" / ".gmail_credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_bytes()) == gmail_setup.validate_oauth_client(raw)
