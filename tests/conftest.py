"""Shared fixtures for the vcoutreach test suite.

Written from SPEC.md only (sections 1 to 17). Nothing here reads the implementation.

Layers:
* autouse isolation: scrubbed environment, throwaway HOME and cwd, socket guard
* ``kit``: builders (contacts, senders, CSV files, drafts), fakes (HTTP transport, mail
  transport, model provider) and a subprocess CLI runner
* ``gateway``: a real stdlib HTTP server on 127.0.0.1 for the tests marked ``local_http``

No em dash or en dash appears in this file. Use ``EM_DASH`` / ``EN_DASH`` escapes instead.
"""
from __future__ import annotations

import csv
import hashlib
import http.server
import importlib
import io
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pytest

EM_DASH = "\u2014"
EN_DASH = "\u2013"

CSV_HEADER = [
    "id", "name", "email", "org", "role", "facts", "source_urls",
    "lane", "thread_subject", "in_reply_to", "references",
]

# Environment variables the tool may read. All are removed before every test.
SCRUBBED_ENV = [
    "SEND_MODE", "TEST_RECIPIENT_EMAIL", "DAILY_SEND_CAP", "SENDER_EMAIL", "WORKDIR",
    "GMAIL_TOKEN_PATH", "GMAIL_CLIENT_SECRETS_PATH",
    "PUBLIK_API_KEY", "PUBLIK_API_BASE_URL", "PUBLIK_MODEL",
    "PUBLIK_TIMEOUT_SECONDS", "PUBLIK_MAX_RETRIES",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
    "VCOUTREACH_DEBUG",
]
PROXY_ENV = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy",
]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "local_http: test may open sockets to 127.0.0.1 only (fake gateway)"
    )
    config.addinivalue_line(
        "markers", "live: opt-in test that talks to the real Publik API (costs real money)"
    )


# ---------------------------------------------------------------------------
# Autouse isolation
# ---------------------------------------------------------------------------

class NetworkBlocked(BaseException):
    """Raised when code tries to open a forbidden socket.

    Derives from BaseException on purpose: ``except Exception`` or ``except OSError`` in
    the code under test cannot swallow it, so a hidden network attempt always fails a test.
    """


class NetworkGuard:
    def __init__(self) -> None:
        self.attempts: list[tuple[str, Any]] = []


@pytest.fixture
def network_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> NetworkGuard:
    guard = NetworkGuard()
    if request.node.get_closest_marker("live") is not None:
        return guard  # opt-in live test: real network allowed
    allow_local = request.node.get_closest_marker("local_http") is not None
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def loopback(address: Any) -> bool:
        return isinstance(address, tuple) and len(address) >= 2 and address[0] == "127.0.0.1"

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple):
            if allow_local and loopback(address):
                return real_connect(self, address)
            guard.attempts.append(("connect", address[:2]))
            raise NetworkBlocked(f"outbound connect blocked by test suite: {address[:2]!r}")
        return real_connect(self, address)  # AF_UNIX and friends are not network

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple):
            if allow_local and loopback(address):
                return real_connect_ex(self, address)
            guard.attempts.append(("connect_ex", address[:2]))
            raise NetworkBlocked(f"outbound connect_ex blocked by test suite: {address[:2]!r}")
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if allow_local and host in ("127.0.0.1", b"127.0.0.1"):
            return real_getaddrinfo(host, *args, **kwargs)
        guard.attempts.append(("getaddrinfo", host))
        raise NetworkBlocked(f"DNS lookup blocked by test suite: {host!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    return guard


@pytest.fixture(autouse=True)
def _network_guard_autouse(network_guard: NetworkGuard) -> None:
    return None


@pytest.fixture(autouse=True)
def _clean_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    if request.node.get_closest_marker("live") is None:
        for name in PROXY_ENV:
            monkeypatch.delenv(name, raising=False)
        # A developer proxy must never swallow requests meant for the fake gateway.
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture(autouse=True)
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh cwd and HOME for every test, so nothing touches the real home or the repo."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(work)
    return work


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        assert self.body is not None, "request had no body"
        return json.loads(self.body.decode("utf-8"))


class FakeTransport:
    """Implements the spec's Transport protocol.

    The script is a queue of HttpResponse objects or TransportFailure instances (or a
    callable taking the RecordedRequest and returning one of those). Every request is
    recorded. A request with nothing scripted raises AssertionError so over-calling fails.
    """

    def __init__(self, *script: Any) -> None:
        self.script: deque[Any] = deque(script)
        self.requests: list[RecordedRequest] = []

    def push(self, *items: Any) -> "FakeTransport":
        self.script.extend(items)
        return self

    def request(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None, timeout: float) -> Any:
        rec = RecordedRequest(
            method=method, url=url,
            headers={str(k).lower(): str(v) for k, v in headers.items()},
            body=body, timeout=timeout,
        )
        self.requests.append(rec)
        if not self.script:
            raise AssertionError(
                f"FakeTransport: request #{len(self.requests)} to {url} had no scripted response"
            )
        item = self.script.popleft()
        if callable(item) and not isinstance(item, BaseException):
            item = item(rec)
        if isinstance(item, BaseException):
            raise item
        return item


class Sleeper:
    """Injectable ``sleep`` that records instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


def make_rng(*values: float) -> Callable[[], float]:
    """Injectable ``rng``: yields the values in order, repeating the last one forever."""
    seq = list(values) or [0.0]
    state = {"i": 0}

    def rng() -> float:
        v = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return v

    rng.calls = state  # type: ignore[attr-defined]
    return rng


class FakeMailTransport:
    """Implements the spec's MailTransport protocol and records every message."""

    def __init__(self, fail_on: Iterable[int] = (), fail_all: bool = False) -> None:
        self.sent: list[Any] = []
        self.attempts = 0
        self.fail_on = set(fail_on)
        self.fail_all = fail_all

    def send(self, message: Any) -> Any:
        from vcoutreach.send.gate import SendReceipt
        from vcoutreach.types import OutreachError

        self.attempts += 1
        if self.fail_all or self.attempts in self.fail_on:
            raise OutreachError("simulated mail transport failure")
        self.sent.append(message)
        return SendReceipt(message_id=f"<fake-{len(self.sent)}@mail.test>")


class StubProvider:
    """A model provider scripted from the test (hostile or otherwise)."""

    def __init__(self, behavior: Callable[[Any], Any], name: str = "stub") -> None:
        self.name = name
        self.behavior = behavior
        self.calls: list[Any] = []

    def personalize(self, request: Any) -> Any:
        self.calls.append(request)
        return self.behavior(request)


# ---------------------------------------------------------------------------
# Local gateway (real HTTP server on 127.0.0.1)
# ---------------------------------------------------------------------------

@dataclass
class CapturedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class _GatewayServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:  # skip getfqdn(): no reverse DNS in tests
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


class _GatewayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    def _serve(self) -> None:
        gw: LocalGateway = self.server.gateway  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        gw._capture(CapturedRequest(
            self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body,
        ))
        spec = gw._next_spec()
        if spec["delay"]:
            gw.release.wait(spec["delay"])
        payload: bytes = spec["body"]
        try:
            self.send_response(spec["status"])
            sent = set()
            for key, value in spec["headers"].items():
                self.send_header(key, value)
                sent.add(key.lower())
            if "content-length" not in sent:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except OSError:
            pass  # client gave up (timeout test)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _serve


class LocalGateway:
    """Fake Publik-style gateway: scripted statuses, headers, bodies, delays; captures requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._script: deque[dict[str, Any]] = deque()
        self.requests: list[CapturedRequest] = []
        self.exhausted = 0
        self.release = threading.Event()
        self.stopped = False
        self.server = _GatewayServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.gateway = self  # type: ignore[attr-defined]
        self.port: int = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
        )
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        return f"{self.origin}/api/v1"

    def reply(self, status: int = 200, *, json_body: Any = None, body: bytes | str = b"",
              headers: Mapping[str, str] | None = None, delay: float = 0.0) -> "LocalGateway":
        hdrs = dict(headers or {})
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "application/json"
        else:
            payload = body.encode("utf-8") if isinstance(body, str) else body
        with self._lock:
            self._script.append({"status": status, "headers": hdrs, "body": payload, "delay": delay})
        return self

    def reply_chat(self, text: str, headers: Mapping[str, str] | None = None) -> "LocalGateway":
        return self.reply(200, headers=headers, json_body={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        })

    def _capture(self, req: CapturedRequest) -> None:
        with self._lock:
            self.requests.append(req)

    def _next_spec(self) -> dict[str, Any]:
        with self._lock:
            if self._script:
                return self._script.popleft()
            self.exhausted += 1
        return {"status": 500, "headers": {"Content-Type": "application/json"},
                "body": b'{"error":{"message":"gateway script exhausted"}}', "delay": 0.0}

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def gateway(request: pytest.FixtureRequest):
    if request.node.get_closest_marker("local_http") is None:
        pytest.fail("tests that use the gateway fixture must be marked @pytest.mark.local_http")
    gw = LocalGateway()
    try:
        yield gw
    finally:
        gw.stop()


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

@dataclass
class CliResult:
    code: int
    out: str
    err: str
    net_attempts: str

    @property
    def combined(self) -> str:
        return self.out + self.err


_SITECUSTOMIZE = '''\
import os, socket
_marker = os.environ.get("VCOUTREACH_TEST_NET_MARKER")

def _note(kind, detail):
    if _marker:
        try:
            with open(_marker, "a") as fh:
                fh.write("%s %r\\n" % (kind, detail))
        except OSError:
            pass

_orig_connect = socket.socket.connect

def _connect(self, address):
    if isinstance(address, tuple):
        _note("connect", address[:2])
        raise ConnectionRefusedError("network blocked by the vcoutreach test suite")
    return _orig_connect(self, address)

def _connect_ex(self, address):
    _note("connect_ex", address)
    return 111

def _gai(host, *a, **k):
    _note("getaddrinfo", host)
    raise OSError("network blocked by the vcoutreach test suite")

socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.getaddrinfo = _gai

if os.environ.get("VCOUTREACH_TEST_BLOCK_GOOGLE"):
    # Fake "the optional Google libraries are not installed": import raises ImportError and
    # importlib.util.find_spec reports None, exactly as for a package that is really absent.
    import sys
    for _name in ("google", "googleapiclient", "google_auth_oauthlib", "google_auth_httplib2", "httplib2", "oauthlib"):
        sys.modules[_name] = None
'''


# ---------------------------------------------------------------------------
# Kit
# ---------------------------------------------------------------------------

class Kit:
    EM = EM_DASH
    EN = EN_DASH
    FAKE_KEY = "fk_test_0123456789abcdefZQ4W"
    HOOK = "I came across your essay on how small teams ship weekly, and I found it interesting."
    CONFIRM = "CONFIRM_LIVE_SEND"
    CSV_HEADER = CSV_HEADER

    def __init__(self, workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workdir = workdir
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    # ----- builders -------------------------------------------------------

    def sender_dict(self, **over: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "name": "Sam Rivera",
            "title": "Founder",
            "org": "Lumenlist",
            "website": "https://lumenlist.example.com",
            "signature": "Sam Rivera\nFounder, Lumenlist\nhttps://lumenlist.example.com",
            "product_lines": [
                "We build a tool that turns a spreadsheet of contacts into plain drafts a founder can actually send."
            ],
            "traction_lines": ["Last month we onboarded a handful of design partners."],
            "ask_line": "If a short call makes sense, here is my calendar link: https://cal.example.com/sam",
            "approved_claims": {"design_partners": "a handful of design partners"},
            "forbidden_phrases": ["synergy", "game changer"],
        }
        base.update(over)
        return base

    def sender(self, **over: Any):
        from dataclasses import fields

        from vcoutreach.types import SenderProfile

        data = self.sender_dict(**over)
        allowed = {f.name for f in fields(SenderProfile)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in allowed:
                continue
            if key in ("product_lines", "traction_lines", "forbidden_phrases"):
                value = tuple(value)
            kwargs[key] = value
        return SenderProfile(**kwargs)

    def write_sender(self, path: Path | str | None = None, **over: Any) -> Path:
        path = Path(path) if path else self.workdir / "sender.json"
        path.write_text(json.dumps(self.sender_dict(**over), indent=2), encoding="utf-8")
        return path

    def contact(self, **over: Any):
        from vcoutreach.types import Contact, Lane

        data: dict[str, Any] = {
            "id": "ada",
            "name": "Ada Lovelace",
            "email": "ada@northwind.example.com",
            "org": "Northwind Labs",
            "role": "Partner",
            "facts": ("your essay on how small teams ship weekly",
                      "your role leading Northwind Labs, a seed stage fund"),
            "source_urls": ("https://northwind.example.com/essay",),
            "lane": Lane.FIRST_TOUCH,
        }
        data.update(over)
        for key in ("facts", "source_urls"):
            data[key] = tuple(data[key])
        return Contact(**data)

    def follow_up_contact(self, **over: Any):
        from vcoutreach.types import Lane

        data = {
            "id": "grace",
            "name": "Grace Hopper",
            "email": "grace@compiler.example.org",
            "org": "Compiler Co",
            "facts": ("your talk on making software readable",),
            "lane": Lane.FOLLOW_UP,
            "thread_subject": "a note for Grace",
            "in_reply_to": "<orig-001@lumenlist.example.com>",
            "references": "<orig-001@lumenlist.example.com>",
        }
        data.update(over)
        return self.contact(**data)

    def request(self, contact: Any = None, sender: Any = None, instructions: str = ""):
        from vcoutreach.types import PersonalizationRequest

        return PersonalizationRequest(
            contact=contact or self.contact(), sender=sender or self.sender(),
            instructions=instructions,
        )

    def clean_body(self, *, first_name: str = "Ada", hook: str | None = None,
                   sender: dict[str, Any] | None = None) -> str:
        """A lint-clean first touch body assembled by hand (independent of templates.py)."""
        s = sender or self.sender_dict()
        paragraphs = [
            f"Hi {first_name},",
            hook if hook is not None else self.HOOK,
            " ".join(p for p in (s["product_lines"][0], s["traction_lines"][0]) if p),
            s["ask_line"],
            s["signature"],
        ]
        return "\n\n".join(paragraphs)

    def draft(self, *, contact_id: str = "alice", to: str = "alice@example.com",
              subject: str | None = None, body: str | None = None, status: str = "ok",
              lane: Any = None, in_reply_to: str = "", references: str = "",
              hook: Any = None, issues: Sequence[Any] = ()):
        from vcoutreach.types import Draft, Lane

        first = contact_id.capitalize()
        return Draft(
            contact_id=contact_id, to=to,
            subject=subject if subject is not None else f"a note for {first}",
            body=body if body is not None else self.clean_body(first_name=first),
            status=status, hook=hook, issues=tuple(issues),
            lane=lane if lane is not None else Lane.FIRST_TOUCH,
            in_reply_to=in_reply_to, references=references,
        )

    def follow_up_draft(self, **over: Any):
        from vcoutreach.types import Lane

        data = {
            "contact_id": "grace", "to": "grace@example.org", "subject": "a note for Grace",
            "body": self.clean_body(first_name="Grace"), "lane": Lane.FOLLOW_UP,
            "in_reply_to": "<orig-001@lumenlist.example.com>",
            "references": "<orig-000@lumenlist.example.com> <orig-001@lumenlist.example.com>",
        }
        data.update(over)
        return self.draft(**data)

    # ----- settings -------------------------------------------------------

    def settings(self, **env: Any):
        """Build Settings through load_settings from env style kwargs (None drops the key)."""
        from vcoutreach.config import load_settings

        values = {k: str(v) for k, v in env.items() if v is not None}
        return load_settings(env=values, dotenv_path=self.tmp_path / "no-such-file.env")

    def publik_settings(self, **env: Any):
        base: dict[str, Any] = {"PUBLIK_API_KEY": self.FAKE_KEY}
        base.update(env)
        return self.settings(**base)

    def send_settings(self, mode: str = "test", **env: Any):
        base: dict[str, Any] = {
            "SEND_MODE": mode,
            "TEST_RECIPIENT_EMAIL": "me@mail.test",
            "SENDER_EMAIL": "sam@lumenlist.example.com",
        }
        base.update(env)
        return self.settings(**base)

    # ----- CSV ------------------------------------------------------------

    def csv_row(self, **kw: Any) -> dict[str, str]:
        row = {col: "" for col in CSV_HEADER}
        row.update({k: v for k, v in kw.items()})
        return row

    def csv_text(self, rows: Sequence[Mapping[str, Any] | Sequence[Any]],
                 header: Sequence[str] | None = None, lineterminator: str = "\n") -> str:
        header = list(header) if header is not None else list(CSV_HEADER)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator=lineterminator)
        writer.writerow(header)
        for row in rows:
            if isinstance(row, Mapping):
                writer.writerow([row.get(col, "") for col in header])
            else:
                writer.writerow(list(row))
        return buf.getvalue()

    def write_csv(self, rows: Sequence[Mapping[str, Any] | Sequence[Any]], *,
                  name: str = "contacts.csv", header: Sequence[str] | None = None,
                  bom: bool = False, crlf: bool = False) -> Path:
        text = self.csv_text(rows, header=header, lineterminator="\r\n" if crlf else "\n")
        path = self.workdir / name
        path.write_bytes((("\ufeff" if bom else "") + text).encode("utf-8"))
        return path

    def write_raw(self, name: str, data: bytes | str) -> Path:
        path = self.workdir / name
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        return path

    def assert_names_rows(self, message: str, rows: Sequence[int], total: int,
                          path: Path | None = None) -> None:
        """Assert an error message names the given data rows.

        ``rows`` are 1-based data rows (the first data row is 1). SPEC 19.6: the header is
        row 1 of the file, so data row k must be reported as row k + 1. Every wanted number
        must appear, and any number written right after the word "row" must be one of them.
        ``total`` (the number of data rows) is kept for readability at the call sites.
        """
        text = message
        if path is not None:
            text = text.replace(str(path), "").replace(path.name, "")
        wanted = {r + 1 for r in rows}
        found = {int(n) for n in re.findall(r"\d+", text)}
        named = {int(n) for n in re.findall(r"\brows?\s+#?(\d+)", text, re.IGNORECASE)}
        if not wanted <= found or (named and not named <= wanted):
            raise AssertionError(
                f"error message should name file rows {sorted(wanted)} (header is row 1, {total} data rows), "
                f"got {message!r}"
            )

    # ----- HTTP fakes -----------------------------------------------------

    def response(self, status: int = 200, *, headers: Mapping[str, str] | None = None,
                 body: bytes | str = b"", json_body: Any = None):
        from vcoutreach.providers.http import HttpResponse

        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("content-type", "application/json")
        else:
            payload = body.encode("utf-8") if isinstance(body, str) else body
        return HttpResponse(status=status, headers=hdrs, body=payload)

    def chat_ok(self, text: Any, *, headers: Mapping[str, str] | None = None):
        return self.response(200, headers=headers, json_body={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
        })

    def anthropic_ok(self, text: str, *, headers: Mapping[str, str] | None = None):
        return self.response(200, headers=headers, json_body={
            "id": "msg_test", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        })

    def error_response(self, status: int, message: str = "something went wrong", *,
                       headers: Mapping[str, str] | None = None, **error_fields: Any):
        err = {"message": message}
        err.update(error_fields)
        return self.response(status, headers=headers, json_body={"error": err})

    def failure(self, message: str = "simulated connection failure"):
        from vcoutreach.providers.http import TransportFailure

        return TransportFailure(message)

    def fake_transport(self, *script: Any) -> FakeTransport:
        return FakeTransport(*script)

    def sleeper(self) -> Sleeper:
        return Sleeper()

    def rng(self, *values: float) -> Callable[[], float]:
        return make_rng(*values)

    def stub_provider(self, behavior: Callable[[Any], Any], name: str = "stub") -> StubProvider:
        return StubProvider(behavior, name)

    def hook(self, text: str, provider: str = "stub", model: str = "none"):
        from vcoutreach.types import Hook

        return Hook(text=text, provider=provider, model=model)

    def mail_transport(self, **kw: Any) -> FakeMailTransport:
        return FakeMailTransport(**kw)

    # ----- drafts on disk (independent parsing, not using drafts_io) --------

    def md_text(self, *, to: str, subject: str, body: str, status: str = "ok",
                lane: str = "first_touch", provider: str = "template",
                in_reply_to: str = "", references: str = "", issues: str = "") -> str:
        def line(key: str, value: str) -> str:
            return f"{key}: {value}" if value else f"{key}:"

        header = "\n".join([
            line("to", to), line("subject", subject), line("status", status),
            line("lane", lane), line("provider", provider),
            line("in_reply_to", in_reply_to), line("references", references),
            line("issues", issues),
        ])
        return header + "\n\n" + body

    def parse_md(self, path: Path) -> tuple[list[str], dict[str, str], str]:
        """Return (raw header lines, header dict, body) from a draft markdown file."""
        text = path.read_text(encoding="utf-8")
        head, sep, body = text.partition("\n\n")
        assert sep, f"{path} has no blank line between header and body"
        lines = head.split("\n")
        headers: dict[str, str] = {}
        for ln in lines:
            key, _, value = ln.partition(":")
            headers[key.strip()] = value.strip()
        return lines, headers, body

    def read_index(self, out_dir: Path) -> list[dict[str, Any]]:
        path = Path(out_dir) / "index.jsonl"
        assert path.exists(), f"missing {path}"
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def md_files(self, directory: Path) -> list[Path]:
        return sorted(Path(directory).glob("*.md"))

    def paragraphs(self, body: str) -> list[str]:
        return body.strip("\n").split("\n\n")

    # ----- journal / send -------------------------------------------------

    @property
    def journal_path(self) -> Path:
        return self.workdir / ".vcoutreach" / "journal.jsonl"

    def journal(self, path: Path | None = None):
        from vcoutreach.journal import Journal

        return Journal(path or self.journal_path)

    def journal_lines(self, path: Path | None = None) -> list[dict[str, Any]]:
        p = path or self.journal_path
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    @staticmethod
    def expected_key(recipient: str, lane: str) -> str:
        return hashlib.sha256(f"{recipient.lower()}|{lane}".encode("utf-8")).hexdigest()

    # ----- message helpers -------------------------------------------------

    def header(self, msg: Any, name: str) -> str | None:
        value = msg[name]
        if value is None:
            return None
        return str(make_header(decode_header(str(value))))

    def message_text(self, msg: Any) -> str:
        payload = msg.get_payload(decode=True)
        assert payload is not None, "message has no single-part payload"
        return payload.decode(msg.get_content_charset() or "utf-8")

    def recipients(self, msg: Any) -> list[str]:
        raw: list[str] = []
        for name in ("To", "Cc", "Bcc"):
            raw.extend(str(v) for v in (msg.get_all(name) or []))
        return [addr.lower() for _, addr in getaddresses(raw) if addr]

    # ----- subprocess ------------------------------------------------------

    @property
    def import_root(self) -> Path:
        mod = importlib.import_module("vcoutreach")
        return Path(mod.__file__).resolve().parent.parent

    def cli_env(self, extra: Mapping[str, str] | None = None, *, net_guard: bool = True) -> dict[str, str]:
        guard_dir = self.tmp_path / "netguard"
        guard_dir.mkdir(exist_ok=True)
        (guard_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
        path_entries = [str(self.import_root)]
        if net_guard:
            path_entries.insert(0, str(guard_dir))
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.tmp_path / "home"),
            "PYTHONPATH": os.pathsep.join(path_entries),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "VCOUTREACH_TEST_NET_MARKER": str(self.tmp_path / "net_attempts.log"),
        }
        env.update(extra or {})
        return env

    def net_attempts(self) -> str:
        marker = self.tmp_path / "net_attempts.log"
        return marker.read_text(encoding="utf-8") if marker.exists() else ""

    def cli(self, *args: str, cwd: Path | None = None, env: Mapping[str, str] | None = None,
            timeout: float = 90, net_guard: bool = True, no_google: bool = False) -> CliResult:
        """Run ``python -m vcoutreach``. ``no_google`` makes the optional Google libraries look uninstalled."""
        extra = dict(env or {})
        if no_google:
            assert net_guard, "no_google rides on the sitecustomize guard, which net_guard=False removes"
            extra["VCOUTREACH_TEST_BLOCK_GOOGLE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "vcoutreach", *args],
            cwd=str(cwd or self.workdir), env=self.cli_env(extra, net_guard=net_guard),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
        return CliResult(proc.returncode, proc.stdout, proc.stderr, self.net_attempts())

    GOOGLE_MODULES = ("google", "googleapiclient", "google_auth_oauthlib", "google_auth_httplib2", "httplib2", "oauthlib")

    def hide_google(self) -> None:
        """In this process, make the optional Google libraries look uninstalled (import raises ImportError)."""
        for name in self.GOOGLE_MODULES:
            self.monkeypatch.setitem(sys.modules, name, None)

    def run_python(self, code: str, *args: str, env: Mapping[str, str] | None = None,
                   timeout: float = 90) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", code, *args], cwd=str(self.workdir),
            env=self.cli_env(env, net_guard=False), capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
        )

    def quickstart(self, *, extra_env: Mapping[str, str] | None = None) -> CliResult:
        """init + draft --offline in the work dir, exactly as the README quickstart."""
        init = self.cli("init", env=extra_env)
        assert init.code == 0, init.combined
        return self.cli(
            "draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json",
            "--offline", env=extra_env,
        )


@pytest.fixture
def kit(workdir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Kit:
    return Kit(workdir, tmp_path, monkeypatch)
