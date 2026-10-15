"""The local web app (`vcoutreach ui`), driven like a person would: a real server in a subprocess.

Written from the UI spec and the public CLI contract only. The tests start
`python -m vcoutreach ui --workspace <tmp> --no-browser --port <free>`, parse the printed link
and talk to the server over plain HTTP with the token header.

This module also holds the helpers that tests/test_ui_security.py imports.
No dash characters appear here: EM_DASH is built from its code point.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

pytestmark = pytest.mark.local_http

EM_DASH = chr(0x2014)
REPO = Path(__file__).resolve().parent.parent
TOKEN_HEADER = "X-Vcoutreach-Token"
LINK_RE = re.compile(r"Open this link: http://127\.0\.0\.1:(\d+)/#token=(\S+)")
SAMPLE_IDS = {"ines-marlowe", "tobias-wren", "priya-ostrander", "odalys-brandt", "rhea-castellane"}
OK_IDS = SAMPLE_IDS - {"rhea-castellane"}
SECRET = "sk-test-SUPERSECRET-0123456789abcdef"

VALIDATION = (400, 422)

CSV_HEADER = [
    "id", "name", "email", "org", "role", "facts", "source_urls",
    "lane", "thread_subject", "in_reply_to", "references",
]


# ----- plumbing ---------------------------------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Resp:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class UiServer:
    def __init__(self, proc: subprocess.Popen, workspace: Path, home: Path, cwd: Path) -> None:
        self.proc = proc
        self.workspace = workspace
        self.home = home
        self.cwd = cwd
        self.port = 0
        self.token = ""
        self.out_lines: List[str] = []
        self.err_lines: List[str] = []
        self._linked = threading.Event()
        self._threads = [
            threading.Thread(target=self._pump, args=(proc.stdout, self.out_lines, True), daemon=True),
            threading.Thread(target=self._pump, args=(proc.stderr, self.err_lines, False), daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _pump(self, stream, sink: List[str], watch: bool) -> None:
        for line in iter(stream.readline, ""):
            sink.append(line)
            if watch:
                m = LINK_RE.search(line)
                if m and not self._linked.is_set():
                    self.port = int(m.group(1))
                    self.token = m.group(2)
                    self._linked.set()

    def wait_for_link(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._linked.wait(0.05):
                return
            if self.proc.poll() is not None:
                time.sleep(0.2)
                break
        raise AssertionError(
            "server never printed the link; exit=%r stdout=%r stderr=%r"
            % (self.proc.poll(), "".join(self.out_lines), "".join(self.err_lines))
        )

    @property
    def stdout(self) -> str:
        return "".join(self.out_lines)

    @property
    def stderr(self) -> str:
        return "".join(self.err_lines)

    def stop(self, sig: int = signal.SIGINT, timeout: float = 10.0) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        for t in self._threads:
            t.join(2)
        return self.proc.returncode

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass

    # ----- http ---------------------------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw: Optional[bytes] = None,
        content_type: Any = "application/json",
        headers: Optional[Dict[str, str]] = None,
        token: Any = True,
        host: Any = True,
        timeout: float = 20.0,
    ) -> Resp:
        """token True sends the real token, a string sends that string, None/False sends none.
        host True sends 127.0.0.1:PORT, a string sends that, False omits the Host header."""
        if raw is None and body is not None:
            raw = json.dumps(body).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is True:
                conn.putheader("Host", "127.0.0.1:%d" % self.port)
            elif host:
                conn.putheader("Host", str(host))
            if token is True:
                conn.putheader(TOKEN_HEADER, self.token)
            elif token:
                conn.putheader(TOKEN_HEADER, str(token))
            if raw is not None or method in ("POST", "PUT"):
                if content_type:
                    conn.putheader("Content-Type", content_type)
                conn.putheader("Content-Length", str(len(raw or b"")))
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.endheaders()
            try:
                if raw:
                    conn.send(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the server may answer 413 without reading the whole body
            resp = conn.getresponse()
            data = resp.read()
            return Resp(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        finally:
            conn.close()

    def get(self, path: str, **kw: Any) -> Resp:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Resp:
        return self.request("POST", path, body={} if body is None else body, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> Resp:
        return self.request("PUT", path, body={} if body is None else body, **kw)

    def alive(self) -> bool:
        try:
            return self.get("/api/health", token=False, timeout=5).status == 200
        except OSError:
            return False


def start_ui(
    workspace: Path,
    *,
    home: Path,
    cwd: Path,
    port: Optional[int] = None,
    extra_env: Optional[Dict[str, str]] = None,
    wait: bool = True,
) -> UiServer:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(REPO),
        "NO_PROXY": "127.0.0.1,localhost",
        "LC_ALL": "C.UTF-8",
    }
    env.update(extra_env or {})
    cmd = [sys.executable, "-m", "vcoutreach", "ui", "--workspace", str(workspace), "--no-browser",
           "--port", str(port if port is not None else free_port())]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd),
        stdin=subprocess.DEVNULL,
    )
    server = UiServer(proc, workspace, home, cwd)
    if wait:
        try:
            server.wait_for_link()
        except BaseException:
            server.kill()
            raise
    return server


@pytest.fixture
def paths(tmp_path: Path) -> Dict[str, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return {"ws": ws, "outside": outside, "home": tmp_path / "home", "cwd": tmp_path / "work", "tmp": tmp_path}


@pytest.fixture
def make_server(paths):
    started: List[UiServer] = []

    def make(extra_env: Optional[Dict[str, str]] = None, workspace: Optional[Path] = None) -> UiServer:
        s = start_ui(workspace or paths["ws"], home=paths["home"], cwd=paths["cwd"], extra_env=extra_env)
        started.append(s)
        return s

    yield make
    for s in started:
        s.kill()


@pytest.fixture
def server(make_server) -> UiServer:
    return make_server()


# ----- shape helpers (the spec fixes names loosely, so these stay tolerant) -----------------------------------------

def as_list(payload: Any, *keys: str) -> list:
    if isinstance(payload, list):
        return payload
    assert isinstance(payload, dict), payload
    for key in keys + ("items", "rows", "results"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise AssertionError("no list found in %r" % (payload,))


def issue_codes(issues: Iterable[Any]) -> List[str]:
    out = []
    for issue in issues or []:
        out.append(issue.get("code") if isinstance(issue, dict) else str(issue))
    return out


def count(payload: Any, name: str) -> int:
    value = payload[name]
    return len(value) if isinstance(value, (list, dict)) else int(value)


def present(files_present: Any, name: str) -> bool:
    if isinstance(files_present, dict):
        return bool(files_present.get(name))
    return name in files_present


def draft_map(server: UiServer) -> Dict[str, dict]:
    r = server.get("/api/drafts")
    assert r.status == 200, r.text
    return {d["id"]: d for d in as_list(r.json(), "drafts")}


def load_samples(server: UiServer) -> None:
    r = server.post("/api/samples")
    assert r.ok, (r.status, r.text)


def run_template_draft(server: UiServer, **extra: Any) -> dict:
    body = {"provider": "template"}
    body.update(extra)
    r = server.post("/api/draft", body)
    assert r.status == 200, (r.status, r.text)
    return r.json()


def draft_file(ws: Path, draft_id: str) -> Path:
    for p in (ws / "drafts" / (draft_id + ".md"), ws / "drafts" / "blocked" / (draft_id + ".md")):
        if p.is_file():
            return p
    raise AssertionError("no draft file for %s" % draft_id)


def tree(root: Path) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = b"->" + os.readlink(p).encode()
        elif p.is_file():
            out[rel] = p.read_bytes()
        else:
            out[rel] = b"<dir>"
    return out


@pytest.fixture
def drafted(server, paths):
    load_samples(server)
    run_template_draft(server)
    return server


# ===== startup and shutdown ==============================================================================================

def test_prints_the_link_and_the_stop_hint(server):
    assert LINK_RE.search(server.stdout), server.stdout
    deadline = time.time() + 5
    while "Press Ctrl+C to stop." not in server.stdout and time.time() < deadline:
        time.sleep(0.05)
    assert "Press Ctrl+C to stop." in server.stdout
    assert server.stdout.index("Open this link") < server.stdout.index("Press Ctrl+C")


def test_token_is_long_and_differs_between_sessions(make_server):
    a, b = make_server(), make_server()
    assert len(a.token) >= 16 and len(b.token) >= 16
    assert a.token != b.token
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", a.token), "token must be safe to put in a URL fragment"


def test_requested_port_is_used_exactly(paths):
    port = free_port()
    s = start_ui(paths["ws"], home=paths["home"], cwd=paths["cwd"], port=port)
    try:
        assert s.port == port
        assert s.get("/api/health", token=False).status == 200
    finally:
        s.kill()


def test_ctrl_c_exits_zero_and_quietly(paths):
    s = start_ui(paths["ws"], home=paths["home"], cwd=paths["cwd"])
    try:
        code = s.stop(signal.SIGINT)
    finally:
        s.kill()
    assert code == 0
    assert s.stderr.strip() == "", s.stderr
    assert "Traceback" not in s.stdout


def test_busy_requested_port_is_a_one_line_error(paths):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        s = start_ui(paths["ws"], home=paths["home"], cwd=paths["cwd"], port=port, wait=False)
        try:
            s.proc.wait(20)
        finally:
            s.kill()
        for t in s._threads:
            t.join(2)
        assert s.proc.returncode not in (0, None)
        combined = s.stderr + s.stdout
        assert "Traceback" not in combined
        lines = [ln for ln in s.stderr.splitlines() if ln.strip()]
        assert len(lines) == 1, s.stderr
        assert str(port) in lines[0]
        assert "Open this link" not in s.stdout


@pytest.mark.parametrize("bad", ["70000", "-5", "abc"])
def test_nonsense_port_is_a_clean_failure(paths, bad):
    env = {"PATH": os.environ["PATH"], "HOME": str(paths["home"]), "PYTHONPATH": str(REPO)}
    res = subprocess.run(
        [sys.executable, "-m", "vcoutreach", "ui", "--workspace", str(paths["ws"]), "--no-browser", "--port", bad],
        capture_output=True, text=True, env=env, timeout=20, stdin=subprocess.DEVNULL,
    )
    assert res.returncode != 0
    assert "Traceback" not in res.stderr + res.stdout


def test_default_port_falls_forward_when_8765_is_busy(paths):
    blocker = socket.socket()
    try:
        blocker.bind(("127.0.0.1", 8765))
        blocker.listen(1)
    except OSError:
        blocker.close()
        pytest.skip("port 8765 is in use on this machine")
    try:
        env = {"PATH": os.environ["PATH"], "HOME": str(paths["home"]), "PYTHONPATH": str(REPO),
               "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "vcoutreach", "ui", "--workspace", str(paths["ws"]), "--no-browser"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(paths["cwd"]),
            stdin=subprocess.DEVNULL,
        )
        s = UiServer(proc, paths["ws"], paths["home"], paths["cwd"])
        try:
            s.wait_for_link()
            assert 8766 <= s.port <= 8785
            assert s.get("/api/health", token=False).ok
        finally:
            s.kill()
    finally:
        blocker.close()


def test_binds_loopback_only(server):
    if not any((Path(p) / "lsof").exists() for p in os.environ.get("PATH", "").split(os.pathsep)):
        pytest.skip("lsof not available")
    res = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(server.proc.pid), "-iTCP", "-sTCP:LISTEN"],
        capture_output=True, text=True, timeout=20,
    )
    listening = [ln for ln in res.stdout.splitlines() if "LISTEN" in ln]
    assert listening, res.stdout + res.stderr
    for line in listening:
        assert "127.0.0.1:%d" % server.port in line, line
        assert "*:" not in line and "0.0.0.0" not in line and "[::]" not in line, line


def test_second_loopback_address_is_not_served(server):
    code = (
        "import socket,sys\n"
        "s=socket.socket(); s.settimeout(3)\n"
        "try:\n"
        "    s.connect(('127.0.0.2', %d))\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n" % server.port
    )
    res = subprocess.run([sys.executable, "-c", code], timeout=15)
    assert res.returncode == 0, "server accepts connections on 127.0.0.2, so it is not bound to 127.0.0.1 only"


# ===== health, page, state ===============================================================================================

def test_health_needs_no_token(server):
    r = server.get("/api/health", token=False)
    assert r.status == 200
    assert r.json() == {"ok": True}


def test_root_page_is_html_with_the_five_steps(server):
    r = server.get("/", token=False)
    assert r.status == 200
    assert (r.header("content-type") or "").lower().startswith("text/html")
    for label in ("Sender", "Contacts", "Draft", "Review", "Send plan"):
        assert label in r.text, label


def test_page_has_no_external_urls_in_src_or_href(server):
    html = server.get("/", token=False).text
    assert "<" in html
    for m in re.finditer(r"""(?:src|href|action|data|poster)\s*=\s*["']?\s*([^"'\s>]+)""", html, re.I):
        target = m.group(1)
        assert not re.match(r"(?:https?:)?//", target, re.I), "external reference: %s" % target
    assert not re.search(r"url\(\s*[\"']?(?:https?:)?//", html, re.I)
    assert not re.search(r"@import\s+[\"']?(?:https?:)?//", html, re.I)


def test_page_never_embeds_the_session_token(server):
    assert server.token not in server.get("/", token=False).text


def test_state_on_an_empty_workspace(server, paths):
    r = server.get("/api/state")
    assert r.status == 200
    st = r.json()
    assert Path(st["workspace"]).resolve() == paths["ws"].resolve()
    for name in ("contacts.csv", "sender.json"):
        assert not present(st["files_present"], name)
    prov = st["provider"]
    assert prov["auto_choice"] == "template"
    assert "configured" in prov and "keys" in prov
    assert isinstance(st["counts"], dict)
    assert all(v == 0 for v in st["counts"].values() if isinstance(v, int))


def test_state_after_samples_and_draft_counts_them(drafted):
    st = drafted.get("/api/state").json()
    assert present(st["files_present"], "contacts.csv")
    assert present(st["files_present"], "sender.json")
    values = list(st["counts"].values())
    assert 5 in values, st["counts"]
    assert 4 in values and 1 in values, st["counts"]


# ===== samples ===========================================================================================================

def test_samples_copy_the_packaged_examples_into_the_workspace(server, paths):
    from vcoutreach.contacts import load_contacts
    from vcoutreach.sender import load_sender

    load_samples(server)
    ws = paths["ws"]
    assert {c.id for c in load_contacts(ws / "contacts.csv")} == SAMPLE_IDS
    assert load_sender(ws / "sender.json").name == "Sam Rivera"


def test_samples_never_overwrite_existing_files(server, paths):
    ws = paths["ws"]
    (ws / "contacts.csv").write_text("id,name,email\nmine,Mine,mine@example.com\n", encoding="utf-8")
    before = (ws / "contacts.csv").read_bytes()
    load_samples(server)
    assert (ws / "contacts.csv").read_bytes() == before
    assert (ws / "sender.json").is_file(), "the missing sample file is still created"


def test_samples_twice_is_harmless(server, paths):
    load_samples(server)
    snap = tree(paths["ws"])
    load_samples(server)
    assert tree(paths["ws"]) == snap


# ===== sender ============================================================================================================

SENDER_OK = {
    "name": "Zoë Müller",
    "title": "Founder",
    "org": "Acme Labs",
    "website": "https://example.com/acme",
    "signature": "Zoë Müller\nFounder, Acme Labs",
    "product_lines": ["Acme keeps lists tidy."],
    "traction_lines": ["About 40 small teams use it each week."],
    "ask_line": "Would a short walkthrough help?",
    "approved_claims": {"weekly_teams": "40 small teams use Acme each week"},
    "forbidden_phrases": ["game changer"],
}


def test_sender_round_trip_writes_a_file_the_cli_accepts(server, paths):
    from vcoutreach.sender import load_sender

    r = server.put("/api/sender", SENDER_OK)
    assert r.ok, r.text
    s = load_sender(paths["ws"] / "sender.json")
    assert s.name == SENDER_OK["name"]
    assert list(s.product_lines) == SENDER_OK["product_lines"]
    assert dict(s.approved_claims) == SENDER_OK["approved_claims"]
    got = server.get("/api/sender")
    assert got.status == 200
    body = got.json()
    body = body.get("sender", body) if isinstance(body, dict) else body
    assert body["name"] == SENDER_OK["name"]
    assert body["product_lines"] == SENDER_OK["product_lines"]


def test_sender_get_on_empty_workspace_is_not_a_server_error(server):
    r = server.get("/api/sender")
    assert r.status in (200, 404)
    assert isinstance(r.json(), dict)


def test_sender_subject_template_is_kept(server, paths):
    from vcoutreach.sender import load_sender

    body = dict(SENDER_OK, subject_template="A note for {first_name} at {org}")
    assert server.put("/api/sender", body).ok
    assert load_sender(paths["ws"] / "sender.json").subject_template == "A note for {first_name} at {org}"


@pytest.mark.parametrize(
    "mutation,field",
    [
        ({"name": ""}, "name"),
        ({"product_lines": "just a string"}, "product_lines"),
        ({"ask_line": "Let us talk " + EM_DASH + " soon"}, "ask_line"),
        ({"subject_template": "Hello {bogus}"}, "subject_template"),
        ({"approved_claims": ["not", "an", "object"]}, "approved_claims"),
        ({"forbidden_phrases": [1, 2]}, "forbidden_phrases"),
    ],
)
def test_sender_validation_errors_name_the_field(server, paths, mutation, field):
    body = dict(SENDER_OK)
    body.update(mutation)
    r = server.put("/api/sender", body)
    assert r.status in VALIDATION, (r.status, r.text)
    err = r.json()["error"]
    assert field in err, err
    assert "\n" not in err
    assert not (paths["ws"] / "sender.json").exists()


def test_sender_missing_name_key_is_rejected(server):
    body = dict(SENDER_OK)
    del body["name"]
    r = server.put("/api/sender", body)
    assert r.status in VALIDATION and "name" in r.json()["error"]


def test_sender_that_is_not_an_object_is_rejected(server):
    r = server.request("PUT", "/api/sender", raw=b'["a", "b"]')
    assert r.status in VALIDATION
    assert isinstance(r.json()["error"], str)


def test_invalid_sender_does_not_clobber_the_saved_one(server, paths):
    assert server.put("/api/sender", SENDER_OK).ok
    before = (paths["ws"] / "sender.json").read_bytes()
    assert server.put("/api/sender", dict(SENDER_OK, name="")).status in VALIDATION
    assert (paths["ws"] / "sender.json").read_bytes() == before


# ===== contacts ==========================================================================================================

def contact_row(**kw: str) -> Dict[str, str]:
    row = {k: "" for k in CSV_HEADER}
    row.update({"id": "ada-l", "name": "Ada Lovelace", "email": "ada@example.com", "org": "Analytical Co",
                "role": "Founder", "facts": "your 2024 essay on engines", "lane": "first_touch"})
    row.update(kw)
    return row


def test_contacts_put_writes_the_standard_csv_the_cli_can_load(server, paths):
    from vcoutreach.contacts import load_contacts

    rows = [contact_row(), contact_row(id="bo-c", name="Bo Chen", email="bo@example.org")]
    r = server.put("/api/contacts", rows)
    assert r.ok, r.text
    text = (paths["ws"] / "contacts.csv").read_text(encoding="utf-8")
    header = text.splitlines()[0].lstrip(chr(0xFEFF)).split(",")
    assert header[: len(CSV_HEADER)] == CSV_HEADER
    assert set(header[len(CSV_HEADER):]) <= {"first_name"}
    loaded = load_contacts(paths["ws"] / "contacts.csv")
    assert [c.id for c in loaded] == ["ada-l", "bo-c"]
    got = as_list(server.get("/api/contacts").json(), "contacts")
    assert [row["id"] for row in got] == ["ada-l", "bo-c"]


def test_contacts_round_trip_after_samples_is_lossless(server, paths):
    from vcoutreach.contacts import load_contacts

    load_samples(server)
    before = load_contacts(paths["ws"] / "contacts.csv")
    rows = as_list(server.get("/api/contacts").json(), "contacts")
    assert len(rows) == 5
    assert server.put("/api/contacts", rows).ok
    assert load_contacts(paths["ws"] / "contacts.csv") == before


def test_contacts_with_commas_quotes_and_unicode_survive(server, paths):
    from vcoutreach.contacts import load_contacts

    fact = 'your essay, "On Engines" for Café owners'
    r = server.put("/api/contacts", [contact_row(facts=fact + " | a second fact", org="Smith, Jones & Co")])
    assert r.ok, r.text
    c = load_contacts(paths["ws"] / "contacts.csv")[0]
    assert c.facts == (fact, "a second fact")
    assert c.org == "Smith, Jones & Co"


@pytest.mark.parametrize(
    "row,needle",
    [
        (contact_row(email="not-an-email"), "email"),
        (contact_row(email="Jane <jane@example.com>"), "email"),
        (contact_row(name=""), "name"),
        (contact_row(lane="sideways"), "lane"),
        (contact_row(lane="follow_up"), "in_reply_to"),
        (contact_row(id="bad id!"), "id"),
    ],
)
def test_contacts_invalid_rows_are_rejected_with_field_text(server, paths, row, needle):
    r = server.put("/api/contacts", [row])
    assert r.status in VALIDATION, (r.status, r.text)
    assert needle in r.json()["error"]
    assert not (paths["ws"] / "contacts.csv").exists()


def test_contacts_duplicate_email_is_rejected(server):
    rows = [contact_row(), contact_row(id="other")]
    r = server.put("/api/contacts", rows)
    assert r.status in VALIDATION and "duplicate" in r.json()["error"].lower()


def test_contacts_get_with_no_file_is_empty_not_an_error(server):
    r = server.get("/api/contacts")
    assert r.status in (200, 404)
    assert isinstance(r.json(), (list, dict))


def test_contacts_import_parses_and_writes_nothing(server, paths):
    text = "id,name,email\nada-l,Ada Lovelace,ada@example.com\nbo-c,Bo Chen,bo@example.org\n"
    r = server.post("/api/contacts/import", {"csv_text": text})
    assert r.status == 200, r.text
    payload = r.json()
    assert "ada-l" in json.dumps(payload) and "bo-c" in json.dumps(payload)
    assert not payload.get("errors")
    assert not (paths["ws"] / "contacts.csv").exists()


def test_contacts_import_reports_errors_and_writes_nothing(server, paths):
    text = "id,name,email\nada-l,Ada Lovelace,not-an-email\n"
    r = server.post("/api/contacts/import", {"csv_text": text})
    assert r.status in (200, 400, 422)
    assert "email" in r.text
    assert not (paths["ws"] / "contacts.csv").exists()
    if r.status == 200:
        assert r.json().get("errors")


def test_contacts_import_does_not_touch_an_existing_file(server, paths):
    (paths["ws"] / "contacts.csv").write_text("id,name,email\nmine,Mine,mine@example.com\n")
    before = (paths["ws"] / "contacts.csv").read_bytes()
    server.post("/api/contacts/import", {"csv_text": "id,name,email\nx,X,x@example.com\n"})
    assert (paths["ws"] / "contacts.csv").read_bytes() == before


@pytest.mark.parametrize("body", [{}, {"csv_text": 5}, {"csv_text": None}, {"csv_text": ["a"]}])
def test_contacts_import_needs_a_string(server, body):
    r = server.post("/api/contacts/import", body)
    assert r.status in VALIDATION and isinstance(r.json()["error"], str)


# ===== draft pipeline ====================================================================================================

def test_template_draft_of_the_sample_gives_four_ok_one_blocked(server, paths):
    load_samples(server)
    result = run_template_draft(server)
    assert count(result, "drafted") == 4
    assert count(result, "blocked") == 1
    assert "rhea-castellane" in json.dumps(result)
    ws = paths["ws"]
    assert {p.stem for p in (ws / "drafts").glob("*.md")} == OK_IDS
    assert (ws / "drafts" / "blocked" / "rhea-castellane.md").is_file()
    assert (ws / "drafts" / "index.jsonl").is_file()


def test_ui_draft_is_byte_identical_to_the_cli(server, paths):
    load_samples(server)
    run_template_draft(server)
    out = paths["tmp"] / "cli_out"
    env = {"PATH": os.environ["PATH"], "HOME": str(paths["home"]), "PYTHONPATH": str(REPO)}
    res = subprocess.run(
        [sys.executable, "-m", "vcoutreach", "draft", "--contacts", str(paths["ws"] / "contacts.csv"),
         "--sender", str(paths["ws"] / "sender.json"), "--offline", "--out", str(out)],
        capture_output=True, text=True, env=env, cwd=str(paths["cwd"]), timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert tree(out) == tree(paths["ws"] / "drafts")


def test_only_ids_limits_the_run(server, paths):
    load_samples(server)
    result = run_template_draft(server, only_ids=["tobias-wren"])
    assert count(result, "drafted") == 1 and count(result, "blocked") == 0
    assert (paths["ws"] / "drafts" / "tobias-wren.md").is_file()
    assert not (paths["ws"] / "drafts" / "ines-marlowe.md").exists()


def test_draft_without_inputs_is_a_clear_client_error(server):
    r = server.post("/api/draft", {"provider": "template"})
    assert 400 <= r.status < 500
    err = r.json()["error"]
    assert "contacts" in err.lower() or "sender" in err.lower()
    assert "Traceback" not in r.text


def test_draft_with_unknown_provider_is_rejected(server):
    load_samples(server)
    r = server.post("/api/draft", {"provider": "skynet"})
    assert r.status in VALIDATION
    assert "template" in r.json()["error"].lower(), "the error should list the valid providers"


def test_draft_with_a_keyed_provider_but_no_key_fails_cleanly_offline(server):
    load_samples(server)
    r = server.post("/api/draft", {"provider": "publik"})
    assert 400 <= r.status < 600 and r.status != 500, (r.status, r.text)
    assert "Traceback" not in r.text
    assert isinstance(r.json()["error"], str)
    assert server.alive()


# ===== drafts list and detail ============================================================================================

def test_drafts_list_has_the_documented_fields(drafted):
    drafts = draft_map(drafted)
    assert set(drafts) == SAMPLE_IDS
    d = drafts["ines-marlowe"]
    assert d["to"] == "ines.marlowe@example.com"
    assert d["status"] == "ok"
    assert isinstance(d["issues"], list)
    assert isinstance(d["word_count"], int) and d["word_count"] > 20
    assert d["subject"].strip()
    b = drafts["rhea-castellane"]
    assert b["status"] == "blocked"
    assert "insufficient_facts" in issue_codes(b["issues"])


def test_drafts_list_survives_stray_files(drafted, paths):
    (paths["ws"] / "drafts" / "notes.txt").write_text("hello", encoding="utf-8")
    (paths["ws"] / "drafts" / "scratch").mkdir()
    r = drafted.get("/api/drafts")
    assert r.status == 200, r.text
    ids = {d["id"] for d in as_list(r.json(), "drafts")}
    assert OK_IDS <= ids


def test_a_stray_markdown_file_in_drafts_never_causes_a_crash(drafted, paths):
    (paths["ws"] / "drafts" / "README.md").write_text("# not a draft\n", encoding="utf-8")
    r = drafted.get("/api/drafts")
    assert r.status == 200 or 400 <= r.status < 500, r.text
    assert "Traceback" not in r.text
    if r.status == 200:
        assert OK_IDS <= {d["id"] for d in as_list(r.json(), "drafts")}
    else:
        assert isinstance(r.json()["error"], str)
    assert drafted.alive()


def test_drafts_list_is_empty_before_drafting(server):
    r = server.get("/api/drafts")
    assert r.status == 200
    assert as_list(r.json(), "drafts") == []


def test_draft_detail_matches_the_file_on_disk(drafted, paths):
    from vcoutreach.drafts_io import read_draft

    r = drafted.get("/api/drafts/ines-marlowe")
    assert r.status == 200, r.text
    d = r.json()
    d = d.get("draft", d)
    on_disk = read_draft(paths["ws"] / "drafts" / "ines-marlowe.md")
    assert d["body"] == on_disk.body
    assert d["subject"] == on_disk.subject
    assert d["to"] == on_disk.to


def test_blocked_drafts_can_be_opened(drafted):
    r = drafted.get("/api/drafts/rhea-castellane")
    assert r.status == 200
    assert "blocked" in r.text


def test_unknown_draft_id_is_404_json(drafted):
    r = drafted.get("/api/drafts/nobody-here")
    assert r.status == 404
    assert isinstance(r.json()["error"], str)


def test_put_draft_with_em_dash_returns_the_lint_issue(drafted):
    d = drafted.get("/api/drafts/ines-marlowe").json()
    d = d.get("draft", d)
    new_body = d["body"].replace(" ", " " + EM_DASH + " ", 1)
    r = drafted.put("/api/drafts/ines-marlowe", {"subject": d["subject"], "body": new_body})
    assert r.status == 200, r.text
    assert "em_dash" in issue_codes(r.json()["issues"])


def test_put_draft_persists_the_edit_and_a_clean_edit_has_no_em_dash_issue(drafted, paths):
    from vcoutreach.drafts_io import read_draft

    d = drafted.get("/api/drafts/tobias-wren").json()
    d = d.get("draft", d)
    new_body = d["body"].rstrip("\n") + "\nP.S. Thank you for reading."
    r = drafted.put("/api/drafts/tobias-wren", {"subject": d["subject"], "body": new_body})
    assert r.status == 200, r.text
    assert "em_dash" not in issue_codes(r.json()["issues"])
    saved = read_draft(draft_file(paths["ws"], "tobias-wren"))
    assert saved.body == new_body
    assert saved.to == "tobias.wren@example.org"
    again = drafted.get("/api/drafts/tobias-wren").json()
    assert again.get("draft", again)["body"] == new_body


def test_saving_one_draft_leaves_every_other_file_alone(drafted, paths):
    ws = paths["ws"]
    before = tree(ws)
    d = drafted.get("/api/drafts/priya-ostrander").json()
    d = d.get("draft", d)
    assert drafted.put("/api/drafts/priya-ostrander", {"subject": d["subject"], "body": d["body"] + "\nPS"}).ok
    after = tree(ws)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    allowed = {"drafts/priya-ostrander.md", "drafts/blocked/priya-ostrander.md", "drafts/index.jsonl"}
    assert changed <= allowed, changed


def test_put_unknown_draft_does_not_create_it(drafted, paths):
    before = tree(paths["ws"])
    r = drafted.put("/api/drafts/ghost-writer", {"subject": "s", "body": "b"})
    assert r.status in (400, 404)
    assert tree(paths["ws"]) == before


def test_lint_endpoint_flags_an_em_dash_and_stores_nothing(server, paths):
    before = tree(paths["ws"])
    r = server.post("/api/lint", {"subject": "Hello", "body": "Hi there " + EM_DASH + " friend.\n\nSam"})
    assert r.status == 200, r.text
    assert "em_dash" in issue_codes(r.json()["issues"])
    assert tree(paths["ws"]) == before


# ===== send plan (plan only) =============================================================================================

def test_send_plan_before_drafting_is_not_a_server_error(server):
    r = server.get("/api/send-plan")
    assert r.status < 500
    assert isinstance(r.json(), dict)


def test_send_plan_lists_ok_drafts_counts_blocked_and_says_nothing_is_sent(drafted):
    r = drafted.get("/api/send-plan")
    assert r.status == 200, r.text
    plan = r.json()
    assert len(plan["planned"]) + len(plan["skipped"]) == 4
    assert count(plan, "blocked") == 1
    assert "nothing" in str(plan["notice"]).lower()
    assert "sent" in str(plan["notice"]).lower()


def test_send_plan_matches_the_cli_plan(drafted, paths):
    env = {"PATH": os.environ["PATH"], "HOME": str(paths["home"]), "PYTHONPATH": str(REPO)}
    res = subprocess.run(
        [sys.executable, "-m", "vcoutreach", "send", "--drafts", str(paths["ws"] / "drafts"),
         "--sender", str(paths["ws"] / "sender.json")],
        capture_output=True, text=True, env=env, cwd=str(paths["cwd"]), timeout=60,
    )
    assert res.returncode == 0, res.stderr
    cli_planned = {ln.split("\t")[1] for ln in res.stdout.splitlines() if ln.startswith("plan\t")}
    cli_skipped = {ln.split()[1].rstrip(":") for ln in res.stdout.splitlines() if ln.startswith("skipped ")}
    plan = drafted.get("/api/send-plan").json()
    planned_text = json.dumps(plan["planned"])
    for cid in cli_planned:
        assert cid in planned_text, cid
    for cid in cli_skipped:
        assert cid not in planned_text, cid
    assert len(plan["planned"]) == len(cli_planned)


def test_send_plan_reroutes_to_the_test_recipient(make_server, paths):
    s = make_server({"SEND_MODE": "test", "TEST_RECIPIENT_EMAIL": "owner@example.com"})
    load_samples(s)
    run_template_draft(s)
    text = json.dumps(s.get("/api/send-plan").json()["planned"])
    assert "owner@example.com" in text
    assert "[TEST to ines.marlowe@example.com]" in text


def test_send_plan_never_writes_a_journal_or_sends(drafted, paths):
    drafted.get("/api/send-plan")
    journal = paths["ws"] / ".vcoutreach" / "journal.jsonl"
    assert not journal.exists() or journal.read_text().strip() == ""
    cwd_journal = paths["cwd"] / ".vcoutreach" / "journal.jsonl"
    assert not cwd_journal.exists() or cwd_journal.read_text().strip() == ""


# ===== keys ==============================================================================================================

def test_keys_are_written_to_dotenv_with_mode_0600(server, paths):
    from vcoutreach.config import parse_dotenv

    r = server.put("/api/keys", {"PUBLIK_API_KEY": SECRET})
    assert r.ok, r.text
    env_path = paths["ws"] / ".env"
    assert (env_path.stat().st_mode & 0o777) == 0o600
    assert parse_dotenv(env_path.read_text())["PUBLIK_API_KEY"] == SECRET


def test_key_value_is_never_echoed_anywhere(server, paths):
    r = server.put("/api/keys", {"PUBLIK_API_KEY": SECRET})
    assert SECRET not in r.text
    load_samples(server)
    run_template_draft(server)
    for path in ("/api/state", "/api/sender", "/api/contacts", "/api/drafts", "/api/send-plan",
                 "/api/drafts/ines-marlowe", "/api/health", "/"):
        got = server.get(path, token=False if path in ("/", "/api/health") else True)
        assert SECRET not in got.text, path
        assert SECRET not in "".join("%s: %s" % kv for kv in got.headers.items()), path


def test_state_reports_the_key_as_configured_without_its_value(server):
    server.put("/api/keys", {"PUBLIK_API_KEY": SECRET})
    st = server.get("/api/state").json()
    assert st["provider"]["auto_choice"] == "publik"
    assert SECRET not in json.dumps(st)
    assert SECRET[:8] not in json.dumps(st)


def test_keys_preserve_other_lines_and_comments(server, paths):
    env_path = paths["ws"] / ".env"
    env_path.write_text("# my notes\nSEND_MODE=test\n\nTEST_RECIPIENT_EMAIL=me@example.com\n", encoding="utf-8")
    assert server.put("/api/keys", {"OPENAI_API_KEY": SECRET}).ok
    lines = env_path.read_text().splitlines()
    for keep in ("# my notes", "SEND_MODE=test", "TEST_RECIPIENT_EMAIL=me@example.com"):
        assert keep in lines, lines
    assert lines.index("# my notes") < lines.index("SEND_MODE=test")
    assert (env_path.stat().st_mode & 0o777) == 0o600


def test_setting_a_key_twice_replaces_it(server, paths):
    from vcoutreach.config import parse_dotenv

    assert server.put("/api/keys", {"ANTHROPIC_API_KEY": "first-value-AAAA"}).ok
    assert server.put("/api/keys", {"ANTHROPIC_API_KEY": "second-value-BBBB"}).ok
    text = (paths["ws"] / ".env").read_text()
    assert text.count("ANTHROPIC_API_KEY") == 1
    assert "first-value-AAAA" not in text
    assert parse_dotenv(text)["ANTHROPIC_API_KEY"] == "second-value-BBBB"


@pytest.mark.parametrize("name", ["PUBLIK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                  "OPENAI_BASE_URL", "OPENAI_MODEL", "ANTHROPIC_MODEL"])
def test_each_allowed_setting_is_accepted(server, paths, name):
    from vcoutreach.config import parse_dotenv

    value = "https://example.com/v1" if name.endswith("URL") else "value-" + name.lower()
    r = server.put("/api/keys", {name: value})
    assert r.ok, r.text
    assert parse_dotenv((paths["ws"] / ".env").read_text())[name] == value


@pytest.mark.parametrize("name", ["SEND_MODE", "TEST_RECIPIENT_EMAIL", "GMAIL_TOKEN_PATH", "PATH", "LD_PRELOAD",
                                  "PUBLIK_API_BASE_URL", "publik_api_key", "PUBLIK_API_KEY ", "", "A=B"])
def test_other_names_are_rejected_and_not_written(server, paths, name):
    r = server.put("/api/keys", {name: "x"})
    assert r.status == 400, (name, r.status, r.text)
    env_path = paths["ws"] / ".env"
    assert not env_path.exists() or "x" not in env_path.read_text().replace("export", "")


def test_a_bad_name_in_a_mixed_request_never_lands_in_dotenv(server, paths):
    server.put("/api/keys", {"PUBLIK_API_KEY": "good-key-1234", "SEND_MODE": "live"})
    env_path = paths["ws"] / ".env"
    assert not env_path.exists() or "SEND_MODE" not in env_path.read_text()


@pytest.mark.parametrize("value", [5, None, ["a"], {"x": 1}, True])
def test_non_string_key_values_are_rejected(server, paths, value):
    r = server.put("/api/keys", {"OPENAI_API_KEY": value})
    assert r.status == 400
    assert not (paths["ws"] / ".env").exists()


@pytest.mark.parametrize(
    "value",
    ["abc\nSEND_MODE=live", "abc\r\nSEND_MODE=live", 'ab"cd', "ab'cd", 'x"\nSEND_MODE=live\n"', "ab # not a comment",
     "ab$(touch /tmp/pwned)", "back\\slash", "tab\there"],
)
def test_awkward_values_never_inject_extra_env_lines(server, paths, value):
    from vcoutreach.config import parse_dotenv

    r = server.put("/api/keys", {"OPENAI_API_KEY": value})
    env_path = paths["ws"] / ".env"
    if r.status not in (200, 201, 204):
        assert r.status in VALIDATION, "a rejected key value must be a client error, got %s" % r.status
        assert not env_path.exists() or "SEND_MODE" not in env_path.read_text()
        return
    parsed = parse_dotenv(env_path.read_text())
    assert set(parsed) == {"OPENAI_API_KEY"}, parsed
    assert parsed["OPENAI_API_KEY"] == value
    assert len([ln for ln in env_path.read_text().splitlines() if ln.strip()]) == 1


def test_symlinked_dotenv_is_never_written_through(server, paths):
    target = paths["outside"] / "victim.env"
    target.write_text("KEEP=me\n", encoding="utf-8")
    (paths["ws"] / ".env").symlink_to(target)
    server.put("/api/keys", {"OPENAI_API_KEY": SECRET})
    assert target.read_text() == "KEEP=me\n"
    assert SECRET not in target.read_text()


def test_keys_and_bodies_never_reach_the_server_logs(make_server, paths):
    s = make_server()
    marker = "MARKER-sender-name-7f3a9c"
    s.put("/api/keys", {"PUBLIK_API_KEY": SECRET})
    s.put("/api/sender", dict(SENDER_OK, name=marker))
    s.post("/api/contacts/import", {"csv_text": "id,name,email\nlogme-1234,Log Me,logme@example.com\n"})
    s.put("/api/keys", {"SEND_MODE": SECRET + "-bad"})
    s.get("/api/state")
    s.stop()
    logs = s.stdout + s.stderr
    assert SECRET not in logs
    assert marker not in logs
    assert "logme@example.com" not in logs
    assert s.token not in s.stderr
    assert "Traceback" not in logs


# ===== no send route =====================================================================================================

@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/send", {}),
        ("PUT", "/api/send", {}),
        ("POST", "/api/send", {"execute": True, "live": True, "confirm_live": "CONFIRM_LIVE_SEND"}),
        ("POST", "/api/send/execute", {"execute": True}),
        ("POST", "/api/execute", {"execute": True}),
        ("POST", "/api/send-plan", {"execute": True}),
        ("PUT", "/api/send-plan", {"execute": True, "live": True, "confirm_live": "CONFIRM_LIVE_SEND"}),
        ("POST", "/api/send-plan/execute", {}),
        ("GET", "/api/send-plan?execute=1&live=1&confirm_live=CONFIRM_LIVE_SEND", None),
        ("GET", "/api/send", None),
    ],
)
def test_there_is_no_way_to_execute_a_send(make_server, paths, method, path, body):
    s = make_server({"SEND_MODE": "test", "TEST_RECIPIENT_EMAIL": "owner@example.com"})
    load_samples(s)
    run_template_draft(s)
    r = s.request(method, path, body=body) if body is not None else s.request(method, path)
    if r.ok:
        text = r.text.lower()
        data = r.json()
        assert not data.get("sent"), data
        assert "message_id" not in text
        assert "nothing" in text or "planned" in text, "a 2xx here must be the plain plan"
    journal = paths["ws"] / ".vcoutreach" / "journal.jsonl"
    assert not journal.exists() or journal.read_text().strip() == ""
    assert s.alive()


# ===== nothing leaks outside the workspace ===============================================================================

def test_a_full_session_writes_only_inside_the_workspace(server, paths):
    home_before, cwd_before, outside_before = tree(paths["home"]), tree(paths["cwd"]), tree(paths["outside"])
    load_samples(server)
    run_template_draft(server)
    server.put("/api/keys", {"PUBLIK_API_KEY": SECRET})
    server.put("/api/sender", SENDER_OK)
    server.get("/api/send-plan")
    server.put("/api/drafts/ines-marlowe", {"subject": "s", "body": "b"})
    assert tree(paths["home"]) == home_before
    assert tree(paths["cwd"]) == cwd_before
    assert tree(paths["outside"]) == outside_before


def test_the_workspace_flag_not_the_cwd_decides_where_files_go(make_server, paths):
    other = paths["tmp"] / "elsewhere"
    other.mkdir()
    s = make_server(workspace=other)
    load_samples(s)
    assert (other / "contacts.csv").is_file()
    assert not (paths["ws"] / "contacts.csv").exists()
    assert not (paths["cwd"] / "contacts.csv").exists()
