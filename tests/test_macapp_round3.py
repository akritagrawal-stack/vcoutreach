"""Precisely discriminating round-three contracts using public behavior only."""

import json
import os
import socketserver
from unittest.mock import Mock

import pytest
from flask import has_request_context
from werkzeug.datastructures import FileStorage

from test_macapp_round2 import (
    BASE,
    GmailHarness,
    consent,
    gmail_setup,
    good_client,
    runtime,
    workspace,
)
from vcoutreach.web.gmail import scheduler

pytestmark = pytest.mark.local_http
ROUTES = ("disconnect", "connect", "client")


def client_with_redirect(uri):
    client = json.loads(good_client())
    # Every credential and endpoint stays valid; change only redirect_uris.
    client["installed"]["redirect_uris"] = [uri]
    return json.dumps(client).encode()


@pytest.mark.parametrize("uri", [
    "http://example.com",
    "http://example.com:8080",
    "http://localhost.example.com:8080",
    "http://127.0.0.2:8080",
    "http://0.0.0.0:8080",
    "http://192.168.1.5:8080",
    "http://[::2]:8080",
])
def test_01_non_loopback_redirect_rejected(uri):
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(client_with_redirect(uri))


@pytest.mark.parametrize("uri", [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://[::1]:8080",
])
def test_01_real_loopback_redirect_accepted(uri):
    raw = client_with_redirect(uri)
    assert gmail_setup.validate_oauth_client(raw) == json.loads(raw)


def test_02_password_without_username_rejected():
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(
            client_with_redirect("http://:secret@localhost:8080"))


def test_02_username_without_password_rejected():
    with pytest.raises(ValueError):
        gmail_setup.validate_oauth_client(
            client_with_redirect("http://user@localhost:8080"))


@pytest.fixture
def gmail(workspace):
    return GmailHarness(workspace)


@pytest.fixture
def stored_gmail(gmail):
    gmail.setup.import_client(good_client())
    gmail.token.write_text('{"refresh_token":"round-three-keep"}')
    return gmail


def mutate(gmail, route, headers):
    if route == "client":
        return gmail.upload(good_client("replacement-secret"), headers=headers)
    return gmail.post(route, headers=headers)


def assert_rejected_without_change(gmail, consent, route, headers):
    before = gmail.snapshot()
    response = mutate(gmail, route, headers)
    # Check preservation even when a broken guard returns the wrong status.
    assert gmail.snapshot() == before, "rejected-origin request changed Gmail state"
    assert consent.threads == [] and consent.calls == []
    assert response.status_code == 400


@pytest.mark.parametrize("route", ROUTES)
def test_03_same_scheme_different_host_rejected(stored_gmail, consent, route):
    assert_rejected_without_change(
        stored_gmail, consent, route,
        {"Origin": "http://evil.example.com:7654"})


@pytest.mark.parametrize("route", ROUTES)
def test_03_same_host_different_scheme_rejected(stored_gmail, consent, route):
    assert_rejected_without_change(
        stored_gmail, consent, route,
        {"Origin": "https://127.0.0.1:7654"})


@pytest.mark.parametrize("route", ROUTES)
def test_03_same_scheme_and_host_different_port_rejected(stored_gmail, consent, route):
    assert_rejected_without_change(
        stored_gmail, consent, route,
        {"Origin": "http://127.0.0.1:7655"})


@pytest.mark.parametrize("route", ROUTES)
def test_03_exact_origin_accepted(stored_gmail, consent, route):
    response = mutate(stored_gmail, route, {"Origin": BASE})
    assert response.status_code == (202 if route == "connect" else 200)
    if route == "connect":
        assert consent.entered.wait(2), "accepted connect did not start OAuth"


@pytest.mark.parametrize("route", ROUTES)
def test_03_referer_same_scheme_different_host_rejected(stored_gmail, consent, route):
    # No Origin: this request must exercise the Referer fallback itself.
    assert_rejected_without_change(
        stored_gmail, consent, route,
        {"Referer": "http://evil.example.com:7654/settings"})


class UploadReadSpy:
    def __init__(self, stream, calls):
        self.stream = stream
        self.calls = calls

    def read(self, *args, **kwargs):
        size = kwargs.get("size", args[0] if args else None)
        call = {"size": size, "bytes": 0}
        self.calls.append(call)
        result = self.stream.read(*args, **kwargs)
        call["bytes"] = len(result)
        return result

    def __getattr__(self, name):
        return getattr(self.stream, name)


def test_04_50_mib_upload_rejected_with_bounded_stream_read(
        stored_gmail, tmp_path, monkeypatch):
    gmail = stored_gmail
    path = tmp_path / "50-mib-client.json"
    with path.open("wb") as handle:
        handle.write(good_client())
        handle.truncate(50 * 1024 * 1024)
    assert path.stat().st_size == 50 * 1024 * 1024
    calls = []
    wrapped = []

    original_init = FileStorage.__init__

    def observe_upload_stream(upload, *args, **kwargs):
        original_init(upload, *args, **kwargs)
        # Ignore client-side multipart construction; observe the parsed upload.
        if has_request_context() and upload.name == "oauth_client":
            upload.stream = UploadReadSpy(upload.stream, calls)
            wrapped.append(upload)

    monkeypatch.setattr(FileStorage, "__init__", observe_upload_stream)

    before = gmail.snapshot()
    with path.open("rb") as handle:
        response = gmail.post(
            "client", data={"oauth_client": (handle, "huge-client.json")})

    # An early HTTP 413 can reject the body before FileStorage exists.
    assert calls or response.status_code == 413, "upload read spy was not exercised"
    assert all(isinstance(call["size"], int)
               and 0 <= call["size"] <= gmail_setup.MAX_CLIENT_BYTES + 1
               for call in calls), f"unbounded or oversized upload read: {calls}"
    assert sum(call["bytes"] for call in calls) <= gmail_setup.MAX_CLIENT_BYTES + 1
    assert wrapped or response.status_code == 413
    assert response.status_code in (400, 413)
    assert gmail.snapshot() == before


@pytest.fixture
def stopped_twice(workspace, monkeypatch):
    class PipeServer(socketserver.BaseServer):
        """Real stdlib serve_forever/shutdown without a restricted socket bind."""

        server_port = 7654

        def __init__(self, host, port, app, **kwargs):
            super().__init__((host, port), None)
            self.read_fd, self.write_fd = os.pipe()

        def fileno(self):
            return self.read_fd

        def server_close(self):
            os.close(self.read_fd)
            os.close(self.write_fd)

    server = runtime.start_server(workspace.root, server_factory=PipeServer)
    scheduler_stop = Mock(wraps=scheduler.stop_scheduler)
    shutdown = Mock(wraps=server.server.shutdown)
    monkeypatch.setattr(scheduler, "stop_scheduler", scheduler_stop)
    monkeypatch.setattr(server.server, "shutdown", shutdown)
    try:
        server.stop()
        server.stop()
        yield scheduler_stop, shutdown
    finally:
        server.stop()


def test_05_double_stop_calls_scheduler_stop_once(stopped_twice):
    scheduler_stop, _ = stopped_twice
    assert scheduler_stop.call_count == 1


def test_05_double_stop_calls_server_shutdown_once(stopped_twice):
    _, shutdown = stopped_twice
    assert shutdown.call_count == 1


@pytest.fixture
def bare_gmail_client(workspace, consent):
    """Register only Gmail routes, with no application security middleware."""
    from flask import Flask
    from vcoutreach.web.gmail.setup import register_setup_routes

    app = Flask(__name__)
    app.config.update(TESTING=True, PORT=80, WORKSPACE=workspace.root)
    app.extensions["workspace"] = workspace
    register_setup_routes(app, lambda: {})
    app.extensions["gmail_setup"].import_client(good_client())
    return app.test_client()


def bare_gmail_post(client, route, headers, *, upload_client=True):
    from io import BytesIO

    data = {}
    if route == "client" and upload_client:
        data["oauth_client"] = (BytesIO(good_client()), "client.json")
    return client.post(
        "/api/gmail/" + route, base_url="http://localhost",
        headers=headers, data=data)


def assert_bare_gmail_route_proceeded(response, route):
    if route == "client":
        # Missing file is a route-level JSON error, unlike abort(400) HTML.
        assert response.status_code == 400
        payload = response.get_json()
        assert payload is not None and "ok" in payload
        assert payload["ok"] is False
    else:
        assert response.status_code == (200 if route == "disconnect" else 202)


@pytest.mark.parametrize("route", ROUTES)
def test_06_a_bare_different_origin_host_rejected(bare_gmail_client, route):
    response = bare_gmail_post(
        bare_gmail_client, route, {"Origin": "http://evil.example.com"})
    assert response.status_code == 400


@pytest.mark.parametrize("route", ROUTES)
def test_06_b_bare_different_origin_scheme_rejected(bare_gmail_client, route):
    response = bare_gmail_post(
        bare_gmail_client, route, {"Origin": "https://localhost"})
    assert response.status_code == 400


@pytest.mark.parametrize("route", ROUTES)
def test_06_c_bare_different_origin_port_rejected(bare_gmail_client, route):
    response = bare_gmail_post(
        bare_gmail_client, route, {"Origin": "http://localhost:9999"})
    assert response.status_code == 400


@pytest.mark.parametrize("route", ROUTES)
def test_06_d_bare_exact_origin_accepted(bare_gmail_client, route):
    response = bare_gmail_post(
        bare_gmail_client, route, {"Origin": "http://localhost"},
        upload_client=False)
    assert_bare_gmail_route_proceeded(response, route)


@pytest.mark.parametrize("route", ROUTES)
def test_06_e_bare_missing_origin_and_referer_rejected(bare_gmail_client, route):
    response = bare_gmail_post(bare_gmail_client, route, {})
    assert response.status_code == 400


@pytest.mark.parametrize("route", ROUTES)
def test_06_f_bare_different_referer_host_rejected(bare_gmail_client, route):
    response = bare_gmail_post(
        bare_gmail_client, route,
        {"Referer": "http://evil.example.com/settings"})
    assert response.status_code == 400


@pytest.mark.parametrize("route", ROUTES)
def test_06_f_bare_same_origin_referer_accepted(bare_gmail_client, route):
    response = bare_gmail_post(
        bare_gmail_client, route, {"Referer": "http://localhost/settings"},
        upload_client=False)
    assert_bare_gmail_route_proceeded(response, route)


def test_06_g_bare_1_mib_upload_rejected_with_bounded_read(
        bare_gmail_client, monkeypatch):
    from io import BytesIO

    calls = []
    wrapped = []
    original_init = FileStorage.__init__

    def observe_upload_stream(upload, *args, **kwargs):
        original_init(upload, *args, **kwargs)
        if has_request_context() and upload.name == "oauth_client":
            upload.stream = UploadReadSpy(upload.stream, calls)
            wrapped.append(upload)

    monkeypatch.setattr(FileStorage, "__init__", observe_upload_stream)
    raw = good_client()
    raw += b" " * (1024 * 1024 - len(raw))
    assert len(raw) == 1024 * 1024
    response = bare_gmail_client.post(
        "/api/gmail/client", base_url="http://localhost",
        headers={"Origin": "http://localhost"},
        data={"oauth_client": (BytesIO(raw), "large-client.json")})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload is not None and payload["ok"] is False
    assert wrapped and calls, "parsed upload stream read spy was not exercised"
    assert all(isinstance(call["size"], int)
               and 0 < call["size"] <= 64 * 1024 + 1
               for call in calls), f"unbounded or oversized upload read: {calls}"
