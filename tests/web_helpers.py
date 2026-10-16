"""Shared helpers for the web app tests (tests/test_web_*.py).

Written from SPEC_V3.md, SPEC_V2.md sections 2 and 9, SPEC.md sections 3, 7 and 10, the old
route list and the public engine. Nothing here reads the web implementation.

Everything that imports the web package does it lazily, inside functions, so these test modules
collect cleanly even before ``vcoutreach.web`` exists.

ASSUMPTIONS that live in exactly one place each (see SPEC_QUESTIONS.md):
* ``get_token``: the one-time token is ``app.config["ACCESS_TOKEN"]`` (named by CONTRACT.md), else OUTREACH_TOKEN.
* ``ROUTE_CANDIDATES``: the URLs of routes the specs name only loosely.
* ``TYPED_FIELDS``: the form field that carries a typed confirmation phrase.
* ``Web.csrf``: the CSRF token is read from the hidden ``csrf_token`` input of a rendered page.
* ``mark_verified``: the kept legacy flow refuses LIVE sends to unverified addresses; the status is set in the
  workspace database (CONTRACT.md: ``data/outreach.db``, table ``partners``) because no provider runs in tests.
* ``send_batch``/``approved_draft_ids``: the batch form posts the selected draft ids (CONTRACT.md: ``/send-batch``
  counts "the selected approved drafts"); they are read from the Approved rows of the ``/partners`` table.

No dash characters appear in this file: use EM_DASH / EN_DASH (built from code points).
"""
from __future__ import annotations

import csv
import html as html_lib
import http.client
import io
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pytest

# In-process imports of the web package must not drop __pycache__ into the source tree.
sys.dont_write_bytecode = True

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

REPO_ROOT = Path(__file__).resolve().parent.parent

PORT = 8765
HOST = f"127.0.0.1:{PORT}"
BASE = f"http://{HOST}"

TEST_INBOX = "inbox@testmail.example.org"
SENDER_EMAIL = "sender@example.com"

CSV_COLUMNS = [
    "id", "name", "email", "org", "role", "facts", "source_urls",
    "lane", "thread_subject", "in_reply_to", "references",
]


def _j(*pieces: str) -> str:
    """Build a literal from fragments so scans of this public file never meet their own subject."""
    return "".join(pieces)


# Owner strings that must never show up in any page or file (SPEC_V3 section 3). Lower case.
OWNER_NEEDLES = (
    _j("tam", "u.edu"),
    _j("be", "square"),
    _j("/users/", "akrit"),
    _j("application support/", "publik"),
    _j("akrit", "agrawal"),
    _j("vc-outreach-dev-", "secret"),
    _j("com.", "publikhq"),
)

CSP_EXPECTED = {
    "default-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "script-src 'self'",
    "img-src 'self' data:",
    "base-uri 'none'",
    "form-action 'self'",
}

STAGES = ["prospect", "contacted", "warm", "meeting", "passed", "hold"]
REPLY_LABELS = ["interested", "needs_info", "pass", "out_of_office", "bounce", "unlabeled"]

# Environment variables any part of the app might read. All removed before every web test.
WEB_ENV_NAMES = [
    "SEND_MODE", "TEST_RECIPIENT_EMAIL", "DAILY_SEND_CAP", "SENDER_EMAIL", "WORKDIR",
    "VCOUTREACH_WORKDIR", "OUTREACH_HOME", "OUTREACH_SCHEDULER", "OUTREACH_TOKEN",
    "FLASK_SECRET_KEY", "FLASK_ENV", "FLASK_DEBUG",
    "GMAIL_TOKEN_PATH", "GMAIL_CLIENT_SECRETS_PATH",
    "PUBLIK_API_KEY", "PUBLIK_API_BASE_URL", "PUBLIK_MODEL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "ENROW_API_KEY", "APOLLO_API_KEY",
    "PITCHBOOK_ENABLED", "TRACKING_WORKER_URL", "TRACKING_API_TOKEN",
    "CLOUDFLARE_WORKER_URL", "CLOUDFLARE_API_TOKEN", "CF_WORKER_URL",
    "VCOUTREACH_DEBUG",
]

# Where each route is, when the specs name it loosely. {name} marks a path variable.
# Lookups go through the live url_map, so a candidate that does not exist is simply skipped.
ROUTE_CANDIDATES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "contacts_list": ("GET", ("/crm",)),
    "contact_new": ("POST", ("/crm/new", "/contacts/new")),
    "contact_import": ("POST", ("/crm/import", "/contacts/import")),
    "contact_export": ("GET", ("/crm/export.csv", "/contacts/export.csv")),
    "contact_profile": ("POST", ("/crm/{ref}/profile", "/contacts/{ref}/update")),
    "contact_delete": ("POST", ("/crm/{ref}/delete", "/contacts/{ref}/delete")),
    "contact_activity": ("POST", ("/crm/{ref}/activity", "/contacts/{ref}/activity")),
    "task_complete": ("POST", ("/crm/tasks/{id}/complete", "/tasks/{id}/complete")),
    "drafts_list": ("GET", ("/crm/drafts", "/drafts")),
    "drafts_write": ("POST", ("/crm/drafts/write", "/drafts/write")),
    "draft_edit": ("GET", ("/crm/drafts/{slug}", "/drafts/{slug}")),
    "draft_save": ("POST", ("/crm/drafts/{slug}", "/drafts/{slug}")),
    "draft_reviewed": ("POST", ("/crm/drafts/{slug}/reviewed", "/crm/drafts/{slug}/mark-reviewed",
                                "/drafts/{slug}/reviewed", "/crm/drafts/{slug}")),
    "send_test": ("POST", ("/crm/drafts/{slug}/send-test", "/partners/{id}/send-test")),
    "send_live": ("POST", ("/crm/drafts/{slug}/send", "/partners/{id}/send")),
    "approve": ("POST", ("/partners/{id}/approve",)),
    "send_batch": ("POST", ("/crm/send-batch", "/send-batch")),
    "push": ("POST", ("/push",)),
    "schedule": ("POST", ("/schedule-send",)),
    "toggle": ("POST", ("/toggle-send-mode",)),
    "send_plan": ("GET", ("/crm/send-plan", "/send-plan")),
    "recipient": ("POST", ("/crm/send-plan", "/crm/send-plan/recipient", "/settings/test-recipient",
                           "/settings/keys")),
    "analytics": ("GET", ("/crm/analytics", "/analytics")),
    "replies": ("GET", ("/replies",)),
    "reply_add": ("POST", ("/replies/add",)),
    "reply_label": ("POST", ("/replies/{id}/update-label", "/replies/{id}/label")),
    "reply_reviewed": ("POST", ("/replies/{id}/mark-reviewed", "/replies/{id}/reviewed")),
    "settings": ("GET", ("/settings",)),
    "sender_save": ("POST", ("/settings/sender",)),
    "keys_save": ("POST", ("/settings/keys",)),
    "sample_load": ("POST", ("/sample/load",)),
}

# The form field a typed confirmation phrase travels in is not pinned down by the specs.
# Every plausible name is sent; the app reads the one it knows and ignores the rest.
TYPED_FIELDS = (
    "confirm", "confirmation", "typed_confirm", "typed", "phrase", "confirm_text",
    "confirm_phrase", "live_confirm", "typed_confirmation", "go_live", "confirm_slug",
)


def typed(phrase: str) -> Dict[str, str]:
    return {name: phrase for name in TYPED_FIELDS}


# ---------------------------------------------------------------------------------------------------------------
# Environment isolation (import this fixture into each module; see the star import there)
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def web_env_isolation(monkeypatch: pytest.MonkeyPatch):
    for name in WEB_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    yield


# ---------------------------------------------------------------------------------------------------------------
# App creation, login, requests
# ---------------------------------------------------------------------------------------------------------------

def create_web_app(workspace: Path, send_transport: Any = None):
    from vcoutreach.web import create_app

    return create_app(workspace, testing=True, send_transport=send_transport)


TOKEN_CONFIG_KEYS = ("ACCESS_TOKEN", "OUTREACH_TOKEN", "BOOTSTRAP_TOKEN")


def get_token(app) -> str:
    """ASSUMPTION: the one-time link token is exposed in app.config (ACCESS_TOKEN per CONTRACT.md)."""
    for key in TOKEN_CONFIG_KEYS:
        token = app.config.get(key)
        if token:
            return str(token)
    pytest.fail(f"create_app(testing=True) must expose the one-time token in app.config under one of {TOKEN_CONFIG_KEYS}")
    return ""


def body(response) -> str:
    return response.get_data(as_text=True)


def page_text(markup: Any) -> str:
    """Visible text of a page: scripts and styles dropped, tags dropped, entities decoded."""
    if not isinstance(markup, str):
        markup = body(markup)
    stripped = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", markup)
    stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
    return " ".join(html_lib.unescape(stripped).split())


_CSRF_PATTERNS = (
    re.compile(r"""<input\b[^>]*\bname=["']csrf_token["'][^>]*\bvalue=["']([^"']+)["']""", re.I),
    re.compile(r"""<input\b[^>]*\bvalue=["']([^"']+)["'][^>]*\bname=["']csrf_token["']""", re.I),
    re.compile(r"""<meta\b[^>]*\bname=["']csrf-token["'][^>]*\bcontent=["']([^"']+)["']""", re.I),
)


def extract_csrf(markup: str) -> Optional[str]:
    for pattern in _CSRF_PATTERNS:
        match = pattern.search(markup)
        if match:
            return html_lib.unescape(match.group(1))
    return None


class Web:
    """A logged-in test client for one workspace."""

    def __init__(self, app, ws: Path, transport: "Optional[RecordingTransport]" = None):
        self.app = app
        self.ws = Path(ws)
        self.transport = transport
        self.client = app.test_client()
        self._csrf: Optional[str] = None
        self._refs: Dict[str, str] = {}

    # --- session ---------------------------------------------------------------------------------------------
    def login(self):
        response = self.client.get(f"/?token={get_token(self.app)}", base_url=BASE)
        assert response.status_code in (301, 302, 303, 307, 308), (
            f"token exchange should redirect, got {response.status_code}"
        )
        self._csrf = None
        return response

    @property
    def csrf(self) -> str:
        if self._csrf is None:
            for path in ("/settings", "/crm/new", "/crm", "/replies"):
                response = self.get(path)
                token = extract_csrf(body(response)) if response.status_code == 200 else None
                if token:
                    self._csrf = token
                    break
            if self._csrf is None:
                pytest.fail("no page carries a csrf_token hidden input (looked at /settings, /crm/new, /crm, /replies)")
        return self._csrf

    # --- raw requests ----------------------------------------------------------------------------------------
    def request(self, method: str, path: str, **kw):
        kw.setdefault("base_url", BASE)
        return getattr(self.client, method)(path, **kw)

    def get(self, path: str, **kw):
        return self.request("get", path, **kw)

    def post(self, path: str, data: Optional[Mapping[str, Any]] = None, *, csrf: Any = True,
             follow: bool = False, **kw):
        payload = dict(data or {})
        if csrf is True:
            payload["csrf_token"] = self.csrf
        elif isinstance(csrf, str):
            payload["csrf_token"] = csrf
        return self.request("post", path, data=payload, follow_redirects=follow, **kw)

    # --- requests with a hand-made Host or Cookie header ------------------------------------------------------
    def cookie_pairs(self) -> List[Tuple[str, str]]:
        """The (name, value) cookies this client currently holds (read from the client's jar)."""
        jar = getattr(self.client, "_cookies", None) or {}
        return [(c.key, c.value) for c in jar.values()]

    def raw(self, method: str, path: str, *, host: Optional[str] = None,
            cookies: Optional[Sequence[Tuple[str, str]]] = None, headers: Optional[Mapping[str, str]] = None,
            **kw):
        """A request that does not use the client's cookie jar: Host and Cookie are exactly what is passed."""
        pairs = self.cookie_pairs() if cookies is None else list(cookies)
        sent = dict(headers or {})
        if pairs:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in pairs)
        if host is not None:
            sent["Host"] = host
        client = self.app.test_client(use_cookies=False)
        return getattr(client, method)(path, base_url=BASE, headers=sent, **kw)

    # --- routes ----------------------------------------------------------------------------------------------
    def url(self, key: str, **values: Any) -> str:
        return route_url(self.app, key, **values)

    def has_route(self, key: str) -> bool:
        return find_route(self.app, key) is not None

    # --- text of pages ---------------------------------------------------------------------------------------
    def text(self, path: str, **kw) -> str:
        return page_text(body(self.get(path, **kw)))

    def html(self, path: str, **kw) -> str:
        return body(self.get(path, **kw))


# ---------------------------------------------------------------------------------------------------------------
# Route lookup against the live url_map
# ---------------------------------------------------------------------------------------------------------------

def _canon(rule: str) -> str:
    rule = re.sub(r"<(?:[^:>]+:)?[^>]+>", "<>", rule)
    return re.sub(r"\{[^}]+\}", "<>", rule)


def find_route(app, key: str) -> Optional[str]:
    method, candidates = ROUTE_CANDIDATES[key]
    rules = [(r.rule, r.methods or set()) for r in app.url_map.iter_rules()]
    for candidate in candidates:
        for rule, methods in rules:
            if _canon(rule) == _canon(candidate) and method in methods:
                return candidate
    return None


def route_url(app, key: str, **values: Any) -> str:
    candidate = find_route(app, key)
    if candidate is None:
        method, candidates = ROUTE_CANDIDATES[key]
        pytest.fail(f"no {method} route found among {list(candidates)} (route key {key!r})")
    return candidate.format(**{k: str(v) for k, v in values.items()}) if "{" in candidate else candidate


def fill_rule(rule: str) -> str:
    """Turn a url_map rule into a concrete URL with dummy values for path variables."""
    def sub(match: "re.Match[str]") -> str:
        converter = (match.group(1) or "").rstrip(":")
        return "1" if converter in ("int", "float") else "x"

    return re.sub(r"<(?:([^:>]+):)?[^>]+>", sub, rule)


def app_rules(app, method: Optional[str] = None) -> List[Tuple[str, str]]:
    out = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = rule.methods or set()
        if method is None or method in methods:
            out.append((rule.rule, rule.endpoint))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Collection-time discovery (tolerates a web package that does not exist yet)
# ---------------------------------------------------------------------------------------------------------------

STATIC_POST_ROUTES = [
    "/toggle-send-mode", "/push", "/send-batch", "/schedule-send", "/settings", "/settings/reset",
    "/settings/sender", "/settings/keys", "/sample/load", "/demo/sample-email",
    "/crm/new", "/crm/import", "/crm/drafts/write", "/crm/send-batch",
    "/crm/1/profile", "/crm/1/activity", "/crm/1/delete", "/crm/tasks/1/complete",
    "/crm/drafts/x", "/crm/drafts/x/send-test", "/crm/drafts/x/send",
    "/replies/add", "/replies/1/update-label", "/replies/1/mark-reviewed",
    "/poll-replies", "/partners/1/approve", "/partners/1/reject", "/partners/1/send",
]

STATIC_GET_ROUTES = [
    "/crm", "/crm/drafts", "/crm/analytics", "/crm/send-plan", "/crm/new", "/crm/export.csv",
    "/settings", "/replies", "/partners", "/candidates", "/enrichment", "/email-verification",
    "/scheduled", "/healthz",
]

_DISCOVERY: Dict[str, Any] = {}


def _discover() -> Dict[str, Any]:
    if _DISCOVERY:
        return _DISCOVERY
    saved = {name: os.environ.pop(name, None) for name in WEB_ENV_NAMES}
    workspace = tempfile.mkdtemp(prefix="vco-web-discover-")
    try:
        from vcoutreach.web import create_app

        app = create_app(Path(workspace), testing=True, send_transport=None)
        posts = sorted({fill_rule(rule) for rule, _ in app_rules(app, "POST")})
        gets = sorted({fill_rule(rule) for rule, _ep in app_rules(app, "GET")
                       if "<" not in rule and rule != "/"})
        _DISCOVERY.update(ok=True, post=posts, get=gets)
    except BaseException as exc:  # the app may not exist yet: fall back to the static lists
        _DISCOVERY.update(ok=False, post=sorted(STATIC_POST_ROUTES), get=sorted(STATIC_GET_ROUTES),
                          error=f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
    return _DISCOVERY


def discovered_post_routes() -> List[str]:
    return list(_discover()["post"])


def discovered_get_routes() -> List[str]:
    return list(_discover()["get"])


def discovery_ok() -> bool:
    return bool(_discover()["ok"])


def discovery_error() -> str:
    return str(_discover().get("error", ""))


# ---------------------------------------------------------------------------------------------------------------
# Transport that records what the app tries to send
# ---------------------------------------------------------------------------------------------------------------

class RecordedCall:
    def __init__(self, message: EmailMessage, recipient: Optional[str]):
        self.message = message
        self.recipient = recipient

    @property
    def to(self) -> str:
        return str(self.message["To"] or "")

    @property
    def subject(self) -> str:
        return str(self.message["Subject"] or "")

    @property
    def text(self) -> str:
        """The text/plain body (the app sends multipart/alternative: plain text first, then html)."""
        part = self.message.get_body(preferencelist=("plain",))
        if part is None:
            payload = self.message.get_payload(decode=True)
            return payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else ""
        return part.get_content()

    @property
    def recipients(self) -> List[str]:
        raw: List[str] = []
        for name in ("To", "Cc", "Bcc"):
            raw.extend(str(v) for v in (self.message.get_all(name) or []))
        return [addr.lower() for _, addr in getaddresses(raw) if addr]


class RecordingTransport:
    """The injected send_transport: records the built message and recipient, never touches a network."""

    def __init__(self) -> None:
        self.calls: List[RecordedCall] = []
        self.fail: Optional[BaseException] = None

    def __call__(self, *args: Any, **kwargs: Any):
        return self._send(args, kwargs)

    def send(self, message: Any, *args: Any, **kwargs: Any):
        return self._send((message,) + args, kwargs)

    def _send(self, args: Tuple[Any, ...], kwargs: Mapping[str, Any]):
        values = tuple(args) + tuple(kwargs.values())
        message = next((v for v in values if isinstance(v, EmailMessage)), None)
        recipient = next((v for v in values if isinstance(v, str)), None)
        if self.fail is not None:
            raise self.fail
        assert message is not None, "send_transport must receive the built EmailMessage"
        self.calls.append(RecordedCall(message, recipient))
        from vcoutreach.send.gate import SendReceipt

        return SendReceipt(message_id=f"<fake-{len(self.calls)}@mail.test>")

    def fail_with(self, text: str = "simulated transport failure") -> None:
        from vcoutreach.types import OutreachError

        self.fail = OutreachError(text)

    def heal(self) -> None:
        self.fail = None


# ---------------------------------------------------------------------------------------------------------------
# Workspace files and engine-side builders (independent of the web code)
# ---------------------------------------------------------------------------------------------------------------

def sender_dict(**over: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
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


def write_sender_json(ws: Path, **over: Any) -> Path:
    path = Path(ws) / "sender.json"
    path.write_text(json.dumps(sender_dict(**over), indent=2), encoding="utf-8")
    return path


def csv_row(**kw: str) -> Dict[str, str]:
    row = {col: "" for col in CSV_COLUMNS}
    row.update(kw)
    return row


def csv_text(rows: Sequence[Mapping[str, str]], header: Optional[Sequence[str]] = None) -> str:
    header = list(header or CSV_COLUMNS)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(col, "") for col in header])
    return buf.getvalue()


ADA = csv_row(id="ada", name="Ada Lovelace", email="ada@example.com", org="Northwind Labs", role="Partner",
              facts="your essay on how small teams ship weekly | your role leading Northwind Labs",
              source_urls="https://example.com/essay", lane="first_touch")
GRACE = csv_row(id="grace", name="Grace Hopper", email="grace@example.com", org="Compiler Co", role="Founder",
                facts="your talk on making software readable", source_urls="https://example.com/talk")
LINUS = csv_row(id="linus", name="Linus Pauling", email="linus@example.com", org="Helix Fund", role="Principal",
                facts="your newsletter on careful experiments", source_urls="https://example.com/news")
MINA = csv_row(id="mina", name="Mina Park", email="mina@example.com", org="Thin Co", role="Scout",
               facts="", source_urls="")
OREN = csv_row(id="oren", name="Oren Vale", email="oren@example.com", org="Vale Partners", role="Partner",
               facts="your note on weekly shipping", source_urls="https://example.com/note",
               lane="follow_up", thread_subject="a note for Oren", in_reply_to="<orig-001@example.com>",
               references="<orig-001@example.com>")
STANDARD_ROWS = [ADA, GRACE, LINUS]


def write_env(ws: Path, values: Mapping[str, str], extra_lines: Iterable[str] = ()) -> Path:
    path = Path(ws) / ".env"
    lines = [f"{k}={v}" for k, v in values.items()] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def read_env(ws: Path) -> Dict[str, str]:
    from vcoutreach.config import parse_dotenv

    path = Path(ws) / ".env"
    if not path.is_file():
        return {}
    return parse_dotenv(path.read_text(encoding="utf-8"))


def file_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def engine_draft_all(ws: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """Write drafts into ws/drafts with the engine itself (template provider)."""
    from vcoutreach.contacts import load_contacts
    from vcoutreach.pipeline import draft_all
    from vcoutreach.providers import TemplateProvider
    from vcoutreach.sender import load_sender

    source = Path(ws).parent / f"seed-{Path(ws).name}.csv"
    source.write_text(csv_text(rows), encoding="utf-8")
    contacts = load_contacts(source)
    sender = load_sender(Path(ws) / "sender.json")
    draft_all(contacts, sender, TemplateProvider(), Path(ws) / "drafts")


def read_draft_file(ws: Path, slug: str):
    from vcoutreach.drafts_io import read_draft

    for folder in (Path(ws) / "drafts", Path(ws) / "drafts" / "blocked"):
        path = folder / f"{slug}.md"
        if path.is_file():
            return read_draft(path)
    return None


def journal_lines(ws: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for path in Path(ws).rglob("journal.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


def workspace_text_files(ws: Path) -> Dict[Path, str]:
    out: Dict[Path, str] = {}
    for path in Path(ws).rglob("*"):
        if path.is_file():
            try:
                data = path.read_bytes()
            except OSError:
                continue
            out[path] = data.decode("utf-8", errors="replace") if path.suffix not in (
                ".db", ".sqlite", ".sqlite3") else data.decode("latin-1")
    return out


# ---------------------------------------------------------------------------------------------------------------
# Higher level actions
# ---------------------------------------------------------------------------------------------------------------

def import_csv(web: Web, text: str, *, filename: Optional[str] = None, follow: bool = True):
    """POST a CSV to the import route (as pasted text, or as an uploaded file when a filename is given)."""
    url = web.url("contact_import")
    if filename is None:
        return web.post(url, {"csv_text": text}, follow=follow)
    data: Dict[str, Any] = {name: (io.BytesIO(text.encode("utf-8")), filename)
                            for name in ("file", "csv_file", "csv", "upload", "contacts")}
    data["csrf_token"] = web.csrf
    return web.request("post", url, data=data, content_type="multipart/form-data", follow_redirects=follow)


def contact_form_fields(row: Mapping[str, str]) -> Dict[str, str]:
    """A contact row as the New contact form would post it (engine column names, plus aliases)."""
    return {
        "slug": row.get("id", ""), "id": row.get("id", ""), "name": row.get("name", ""),
        "email": row.get("email", ""), "org": row.get("org", ""), "role": row.get("role", ""),
        "facts": row.get("facts", ""),
        "source_urls": row.get("source_urls", "").replace(" | ", "\n"),
        "lane": row.get("lane", "") or "first_touch",
        "thread_subject": row.get("thread_subject", ""), "in_reply_to": row.get("in_reply_to", ""),
        "refs": row.get("references", ""), "references": row.get("references", ""),
        "greeting_name": row.get("first_name", ""), "first_name": row.get("first_name", ""),
    }


def export_rows(web: Web) -> List[Dict[str, str]]:
    response = web.get(web.url("contact_export"))
    assert response.status_code == 200, f"export returned {response.status_code}"
    return list(csv.DictReader(io.StringIO(body(response))))


def seed_contacts(web: Web, rows: Sequence[Mapping[str, str]]) -> None:
    """Create contacts through the app (CSV import first, the New contact form as a fallback)."""
    import_csv(web, csv_text(rows))
    have = {r["email"] for r in export_rows(web)}
    for row in rows:
        if row["email"].lower() not in have:
            web.post(web.url("contact_new"), contact_form_fields(row), follow=True)
    have = {r["email"] for r in export_rows(web)}
    missing = [r["email"] for r in rows if r["email"].lower() not in have]
    assert not missing, f"could not create contacts through the app: {missing}"


_SKIP_REFS = ("drafts", "analytics", "new", "import", "export.csv", "send-plan", "send-batch", "tracking",
              "tasks", "refresh-inbox", "review-preview")


def contact_ref(web: Web, email: str) -> str:
    """The path segment of the contact's page (its slug, or an integer id), found through the app."""
    email = email.lower()
    if email in web._refs:
        return web._refs[email]
    rows = {r["email"]: r for r in export_rows(web)}
    slug = rows[email]["id"] if email in rows else None
    if slug:
        response = web.get(f"/crm/{slug}")
        if response.status_code == 200 and rows[email]["name"] in page_text(body(response)):
            web._refs[email] = slug
            return slug
    listing = web.html("/crm")
    candidates: List[str] = []
    for match in re.finditer(r'href="/crm/([^/"?#]+)"', listing):
        ref = match.group(1)
        if ref not in candidates and ref not in _SKIP_REFS:
            candidates.append(ref)
    for ref in candidates:
        response = web.get(f"/crm/{ref}")
        if response.status_code == 200 and email in body(response).lower():
            web._refs[email] = ref
            return ref
    pytest.fail(f"cannot find the page of contact {email}")
    return ""


def slug_of(web: Web, email: str) -> str:
    for row in export_rows(web):
        if row["email"] == email.lower():
            return row["id"]
    pytest.fail(f"{email} is not in the contact export")
    return ""


def set_stage(web: Web, email: str, stage: str):
    ref = contact_ref(web, email)
    return web.post(web.url("contact_profile", ref=ref), {"stage": stage}, follow=True)


def stage_of(web: Web, email: str) -> Optional[str]:
    ref = contact_ref(web, email)
    markup = web.html(f"/crm/{ref}")
    select = re.search(r'(?is)<select\b[^>]*name="stage"[^>]*>(.*?)</select>', markup)
    if select:
        for option in re.finditer(r'(?is)<option\b([^>]*)>', select.group(1)):
            attrs = option.group(1)
            if re.search(r"\bselected\b", attrs):
                value = re.search(r'value="([^"]*)"', attrs)
                if value:
                    return value.group(1)
    return None


def banner_mode(web: Web, path: str = "/crm/send-plan") -> Optional[str]:
    text = web.text(path).upper()
    positions = {m: text.find(m) for m in ("LIVE MODE", "TEST MODE") if m in text}
    if not positions:
        return None
    return min(positions, key=positions.get).split()[0].lower()


def go_live(web: Web, follow: bool = True):
    payload = typed("GO LIVE")
    payload.update({"mode": "live", "send_mode": "live", "target_mode": "live"})
    return web.post(web.url("toggle"), payload, follow=follow)


def go_test(web: Web, follow: bool = True):
    payload = {"mode": "test", "send_mode": "test", "target_mode": "test"}
    return web.post(web.url("toggle"), payload, follow=follow)


def partner_id(web: Web, slug: str) -> str:
    """The integer id of the contact with this slug (the old app addresses contacts by it)."""
    for row in export_rows(web):
        if row["id"] == slug:
            return contact_ref(web, row["email"])
    pytest.fail(f"no contact with slug {slug!r}")
    return ""


def send_url(web: Web, key: str, slug: str) -> str:
    candidate = find_route(web.app, key)
    values: Dict[str, Any] = {"slug": slug}
    if candidate is not None and "{id}" in candidate:
        values["id"] = partner_id(web, slug)
    return route_url(web.app, key, **values)


def approve_drafts(web: Web, slugs: Sequence[str]) -> None:
    """Approve drafts where the app has an approval step (the old review flow); a no-op otherwise."""
    if not web.has_route("approve"):
        return
    for slug in slugs:
        web.post(web.url("approve", id=partner_id(web, slug)), {}, follow=True)


def send_test(web: Web, slug: str, follow: bool = True):
    return web.post(send_url(web, "send_test", slug), {}, follow=follow)


def send_live(web: Web, slug: str, phrase: Optional[str], follow: bool = True):
    payload = typed(phrase) if phrase is not None else {}
    return web.post(send_url(web, "send_live", slug), payload, follow=follow)


def approved_draft_ids(web: Web) -> List[str]:
    """Ids of the drafts the contacts table offers for a batch and shows as Approved (the legacy /partners page)."""
    markup = web.html("/partners")
    ids: List[str] = []
    for chunk in re.split(r"(?i)<tr\b", markup)[1:]:
        if not re.search(r"(?i)badge-approved", chunk):
            continue
        found = re.search(r"""name=["']draft_ids["'][^>]*\bvalue=["'](\d+)["']""", chunk, re.S)
        if found:
            ids.append(found.group(1))
    return ids


def send_batch(web: Web, phrase: Optional[str], follow: bool = True, ids: Optional[Sequence[str]] = None):
    """POST the batch form: the selected (approved) draft ids, plus the typed phrase when one is given."""
    payload: Dict[str, Any] = typed(phrase) if phrase is not None else {}
    payload["draft_ids"] = list(approved_draft_ids(web) if ids is None else ids)
    return web.post(web.url("send_batch"), payload, follow=follow)


def mark_verified(web: Web, emails: Optional[Sequence[str]] = None) -> None:
    """Mark contact emails as verified, the state the kept Enrow/Apollo verification would record.

    LIVE sends of the kept legacy flow require a verified address (see SPEC_QUESTIONS.md Q7). There is no
    verification provider in the tests, so the status column of the contact table is set directly.
    """
    import sqlite3

    conn = sqlite3.connect(str(Path(web.ws) / "data" / "outreach.db"))
    try:
        if emails is None:
            conn.execute("UPDATE partners SET email_verification_status='valid'")
        else:
            for email in emails:
                conn.execute("UPDATE partners SET email_verification_status='valid' WHERE lower(email)=?",
                             (email.lower(),))
        conn.commit()
    finally:
        conn.close()


def sender_form(**over: Any) -> Dict[str, str]:
    """The Settings sender form as a person would fill it (one product line, one claim, ...)."""
    data = sender_dict(**over)
    return {
        "name": data["name"], "title": data["title"], "org": data["org"], "website": data["website"],
        "signature": data["signature"],
        "product_lines": "\n---\n".join(data["product_lines"]),
        "traction_lines": "\n".join(data["traction_lines"]),
        "ask_line": data["ask_line"],
        "first_touch_subject": data.get("first_touch_subject", ""),
        "approved_claims": json.dumps(data["approved_claims"]),
        "forbidden_phrases": "\n".join(data["forbidden_phrases"]),
    }


def post_keys(web: Web, values: Mapping[str, str], follow: bool = True):
    return web.post(web.url("keys_save"), dict(values), follow=follow)


# ---------------------------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def make_web(tmp_path: Path):
    """Factory: make_web(env=..., sender=True) -> logged-in Web on a fresh workspace."""
    counter = {"n": 0}

    def factory(env: Optional[Mapping[str, str]] = None, *, sender: bool = False,
                env_lines: Iterable[str] = (), with_transport: bool = True, login: bool = True,
                dirname: Optional[str] = None, **sender_over: Any) -> Web:
        counter["n"] += 1
        ws = tmp_path / (dirname or f"ws{counter['n']}")
        ws.mkdir(parents=True, exist_ok=True)
        if env is not None or env_lines:
            write_env(ws, env or {}, env_lines)
        if sender:
            write_sender_json(ws, **sender_over)
        transport = RecordingTransport() if with_transport else None
        app = create_web_app(ws, transport)
        instance = Web(app, ws, transport)
        if login:
            instance.login()
        return instance

    return factory


@pytest.fixture
def web(make_web) -> Web:
    """A logged-in app on an empty workspace."""
    return make_web()


@pytest.fixture
def send_web(make_web):
    """Factory: a workspace ready to send. Contacts imported, engine drafts written and approved, emails
    marked verified, test inbox set."""

    def factory(rows: Optional[Sequence[Mapping[str, str]]] = None, *, env: Optional[Mapping[str, str]] = None,
                with_recipient: bool = True, drafts: bool = True, verified: bool = True, **kw: Any) -> Web:
        values: Dict[str, str] = {"SENDER_EMAIL": SENDER_EMAIL}
        if with_recipient:
            values["TEST_RECIPIENT_EMAIL"] = TEST_INBOX
        values.update(env or {})
        chosen = list(STANDARD_ROWS if rows is None else rows)
        instance = make_web(values, sender=True, **kw)
        seed_contacts(instance, chosen)
        if drafts:
            instance.post(instance.url("drafts_write"), {"provider": "template"}, follow=True)
            approve_drafts(instance, [r["id"] for r in chosen
                                      if read_draft_file(instance.ws, r["id"]) is not None
                                      and read_draft_file(instance.ws, r["id"]).status == "ok"])
        if verified:
            mark_verified(instance, [r["email"] for r in chosen])
        return instance

    return factory


class _FakeProcess:
    pid = 424242
    returncode = 0
    stdout = None
    stderr = None
    stdin = None

    def __init__(self, args: Any = None, kwargs: Optional[Mapping[str, Any]] = None) -> None:
        self.args = args
        opts = dict(kwargs or {})
        self.text_mode = bool(opts.get("text") or opts.get("universal_newlines") or opts.get("encoding")
                              or opts.get("errors"))

    def poll(self) -> int:
        return 0

    def wait(self, timeout: Any = None) -> int:
        return 0

    def communicate(self, input: Any = None, timeout: Any = None) -> Tuple[Any, Any]:
        # A real child returns bytes unless the caller asked for text mode.
        return ("", "") if self.text_mode else (b"", b"")

    def kill(self) -> None:
        return None

    terminate = kill

    def send_signal(self, sig: Any) -> None:
        return None

    def __enter__(self) -> "_FakeProcess":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class SpawnRecorder:
    """Replaces every way the app could start a child process; records the attempts, runs nothing."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def popen(self, args: Any = None, *rest: Any, **kwargs: Any) -> _FakeProcess:
        self.calls.append({"args": args, "kwargs": kwargs, "rest": rest})
        return _FakeProcess(args, kwargs)

    def system(self, command: Any) -> int:
        self.calls.append({"args": command, "kwargs": {"shell": True}, "rest": ()})
        return 0


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> SpawnRecorder:
    recorder = SpawnRecorder()

    def popen_factory(*args: Any, **kwargs: Any) -> _FakeProcess:
        return recorder.popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen_factory)
    monkeypatch.setattr(os, "system", recorder.system)
    for name in ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, lambda *a, _n=name, **k: recorder.calls.append(
                {"args": (_n,) + tuple(a[:2]), "kwargs": k, "rest": ()}) or 0)
    return recorder


@pytest.fixture
def sample_web(make_web) -> Web:
    instance = make_web()
    instance.post(instance.url("sample_load"), {}, follow=True)
    return instance


# ---------------------------------------------------------------------------------------------------------------
# HTML analysis
# ---------------------------------------------------------------------------------------------------------------

class _Analyzer(HTMLParser):
    LOADERS = {"link": ("href",), "script": ("src",), "img": ("src", "srcset"), "iframe": ("src",),
               "source": ("src", "srcset"), "video": ("src", "poster"), "audio": ("src",),
               "embed": ("src",), "object": ("data",), "form": ("action",), "input": ("src", "formaction"),
               "button": ("formaction",), "frame": ("src",), "track": ("src",)}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.loaded: List[Tuple[str, str]] = []
        self.inline_scripts = 0
        self.script_srcs: List[str] = []
        self.handlers: List[str] = []
        self.js_urls: List[str] = []
        self.style_text: List[str] = []
        self.lang: Optional[str] = None
        self.has_main = False
        self.has_nav = False
        self.label_for: set = set()
        self.label_depth = 0
        self.fields: List[Dict[str, str]] = []
        self.in_style = False
        self.in_script = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "main" or a.get("role") == "main":
            self.has_main = True
        if tag == "nav" or a.get("role") == "navigation":
            self.has_nav = True
        for name, value in a.items():
            if name.startswith("on"):
                self.handlers.append(f"<{tag} {name}>")
            if name in ("href", "src", "action", "formaction", "data") and value.strip().lower().startswith("javascript:"):
                self.js_urls.append(f"<{tag} {name}={value[:40]}>")
        for attr in self.LOADERS.get(tag, ()):
            if a.get(attr):
                self.loaded.append((tag, a[attr]))
        if a.get("style"):
            self.style_text.append(a["style"])
        if tag == "style":
            self.in_style = True
        if tag == "script":
            if a.get("src"):
                self.script_srcs.append(a["src"])
            else:
                self.in_script = True
                self.inline_scripts += 1
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        if tag in ("input", "select", "textarea"):
            kind = a.get("type", "text").lower() if tag == "input" else tag
            if kind not in ("hidden", "submit", "button", "image", "reset"):
                self.fields.append({
                    "tag": tag, "id": a.get("id", ""), "name": a.get("name", ""),
                    "aria": a.get("aria-label", "") or a.get("aria-labelledby", ""),
                    "wrapped": "1" if self.label_depth > 0 else "",
                })

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.in_style = False
        if tag == "script":
            self.in_script = False
        if tag == "label" and self.label_depth:
            self.label_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self.style_text.append(data)

    @property
    def unlabelled(self) -> List[str]:
        bad = []
        for f in self.fields:
            if f["wrapped"] or f["aria"] or (f["id"] and f["id"] in self.label_for):
                continue
            bad.append(f"{f['tag']}[name={f['name']!r} id={f['id']!r}]")
        return bad


def analyze(markup: str) -> _Analyzer:
    parser = _Analyzer()
    parser.feed(markup)
    parser.close()
    return parser


def external_loads(markup: str) -> List[str]:
    """Absolute http(s) or protocol-relative URLs that a page would load (not plain links)."""
    parsed = analyze(markup)
    found = [f"<{tag}> {value}" for tag, value in parsed.loaded
             if re.match(r"(?i)(https?:)?//", value.strip())]
    for css in parsed.style_text:
        found.extend(f"css {m}" for m in re.findall(r"(?i)url\(\s*['\"]?(https?:[^)'\"]+|//[^)'\"]+)", css))
        found.extend(f"css @import {m}" for m in re.findall(r"(?i)@import\s+(?:url\()?['\"]?(https?:[^;'\")]+)", css))
    return found


def owner_hits(text: str) -> List[str]:
    low = text.lower()
    return [needle for needle in OWNER_NEEDLES if needle in low]


def decode_cookie_payload(value: str) -> str:
    """Best-effort readable form of a signed session cookie value (Flask signs, it does not encrypt)."""
    import base64
    import zlib

    first = value.strip('"').split(".")[0] if not value.startswith(".") else "." + value.split(".")[1]
    compressed = first.startswith(".")
    raw = first.lstrip(".")
    pad = "=" * (-len(raw) % 4)
    try:
        data = base64.urlsafe_b64decode(raw + pad)
        if compressed:
            data = zlib.decompress(data)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------------------------------------------
# Subprocess helpers (real CLI, real sockets)
# ---------------------------------------------------------------------------------------------------------------

def cli_env(home: Path, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    env.update(extra or {})
    return env


def run_cli(args: Sequence[str], cwd: Path, home: Path, extra_env: Optional[Mapping[str, str]] = None,
            timeout: float = 90) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-m", "vcoutreach", *args], cwd=str(cwd), env=cli_env(home, extra_env),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


LINK_RE = re.compile(r"Open this link: (http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_\-\.~%]+))")


class UiProcess:
    """`python -m vcoutreach ui ...` as a child process, with its output collected in a thread."""

    def __init__(self, workspace: Path, home: Path, cwd: Path, *, port: Optional[int] = None,
                 extra_args: Sequence[str] = (), extra_env: Optional[Mapping[str, str]] = None,
                 no_browser: bool = True):
        args = [sys.executable, "-m", "vcoutreach", "ui", "--workspace", str(workspace)]
        if no_browser:
            args.append("--no-browser")
        if port is not None:
            args += ["--port", str(port)]
        args += list(extra_args)
        self.proc = subprocess.Popen(
            args, cwd=str(cwd), env=cli_env(home, extra_env), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", bufsize=1,
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.seen: List[str] = []
        self.link: Optional[str] = None
        self.port: Optional[int] = None
        self.token: Optional[str] = None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put("\0EOF")

    def wait_for_link(self, timeout: float = 30.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None and self.lines.empty():
                    break
                continue
            if line == "\0EOF":
                break
            self.seen.append(line)
            match = LINK_RE.search(line)
            if match:
                self.link, self.port, self.token = match.group(1), int(match.group(2)), match.group(3)
                return self.link
        raise AssertionError("no 'Open this link:' line was printed. Output so far:\n" + "\n".join(self.seen))

    def drain(self) -> str:
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line != "\0EOF":
                self.seen.append(line)
        return "\n".join(self.seen)

    def stop(self, sig: int = signal.SIGINT, timeout: float = 20.0) -> Optional[int]:
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        time.sleep(0.1)
        self.drain()
        return self.proc.returncode

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def http_request(port: int, method: str, path: str, *, headers: Optional[Mapping[str, str]] = None,
                 payload: Optional[bytes] = None, timeout: float = 10.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=payload, headers=dict(headers or {}))
        response = conn.getresponse()
        data = response.read()
        return response.status, response.getheaders(), data.decode("utf-8", errors="replace")
    finally:
        conn.close()


def header_values(headers: Sequence[Tuple[str, str]], name: str) -> List[str]:
    return [v for k, v in headers if k.lower() == name.lower()]
