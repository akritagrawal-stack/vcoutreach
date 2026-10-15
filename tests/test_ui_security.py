"""Security of the local web app: token, Host, CORS, content type, body size, headers, traversal, symlinks.

Written from the UI spec only. Every test talks to a real server subprocess over loopback HTTP.
The helpers come from tests/test_ui_server.py.
"""
from __future__ import annotations

import json
import re
import socket
import time
from pathlib import Path

import pytest

from test_ui_server import (  # noqa: F401  (fixtures are imported on purpose)
    EM_DASH,
    OK_IDS,
    SECRET,
    SENDER_OK,
    UiServer,
    contact_row,
    drafted,
    load_samples,
    make_server,
    paths,
    run_template_draft,
    server,
    start_ui,
    tree,
)

pytestmark = pytest.mark.local_http

TOKEN_HEADER = "X-Vcoutreach-Token"
MB2 = 2 * 1024 * 1024

# (method, path, JSON body that would be valid for that route when authorized)
API_ROUTES = [
    ("GET", "/api/state", None),
    ("GET", "/api/sender", None),
    ("PUT", "/api/sender", SENDER_OK),
    ("GET", "/api/contacts", None),
    ("PUT", "/api/contacts", [contact_row()]),
    ("POST", "/api/contacts/import", {"csv_text": "id,name,email\nx,X,x@example.com\n"}),
    ("POST", "/api/samples", {}),
    ("POST", "/api/draft", {"provider": "template"}),
    ("GET", "/api/drafts", None),
    ("GET", "/api/drafts/ines-marlowe", None),
    ("PUT", "/api/drafts/ines-marlowe", {"subject": "s", "body": "b"}),
    ("POST", "/api/lint", {"subject": "s", "body": "b"}),
    ("GET", "/api/send-plan", None),
    ("PUT", "/api/keys", {"OPENAI_API_KEY": SECRET}),
    ("GET", "/api/does-not-exist", None),
]
ROUTE_IDS = ["%s %s" % (m, p) for m, p, _ in API_ROUTES]
BODY_ROUTES = [(m, p, b) for m, p, b in API_ROUTES if m in ("POST", "PUT")]
BODY_IDS = ["%s %s" % (m, p) for m, p, _ in BODY_ROUTES]


def call(s: UiServer, method: str, path: str, body, **kw):
    if body is None:
        return s.request(method, path, **kw)
    return s.request(method, path, body=body, **kw)


def assert_json_error(r, *, repo_leak: bool = True) -> None:
    assert (r.header("content-type") or "").lower().startswith("application/json"), r.headers
    payload = r.json()
    assert isinstance(payload, dict) and isinstance(payload.get("error"), str), payload
    msg = payload["error"]
    assert isinstance(msg, str) and msg.strip()
    assert "\n" not in msg, "the error must be one line"
    low = r.text
    assert "Traceback" not in low and 'File "' not in low
    if repo_leak:
        repo = str(Path(__file__).resolve().parent.parent)
        assert repo not in low
        assert "vcoutreach/ui.py" not in low and "ui.py" not in low


# ===== token ===============================================================================================================

@pytest.mark.parametrize("method,path,body", API_ROUTES, ids=ROUTE_IDS)
def test_missing_token_is_403(drafted, method, path, body):
    r = call(drafted, method, path, body, token=None)
    assert r.status == 403, (r.status, r.text)
    assert_json_error(r)


@pytest.mark.parametrize("method,path,body", API_ROUTES, ids=ROUTE_IDS)
def test_wrong_token_is_403(drafted, method, path, body):
    r = call(drafted, method, path, body, token="not-the-token")
    assert r.status == 403, (r.status, r.text)


@pytest.mark.parametrize("variant", ["upper", "lower", "prefix", "suffix", "empty", "double"])
def test_near_miss_tokens_are_403(server, variant):
    t = server.token
    bad = {
        "upper": t.upper() if t.upper() != t else t + "x",
        "lower": t.lower() if t.lower() != t else t + "x",
        "prefix": t[:-1],
        "suffix": t + "0",
        "empty": "",
        "double": t + t,
    }[variant]
    r = server.get("/api/state", token=None, headers={TOKEN_HEADER: bad})
    assert r.status == 403
    # control: the real token still works afterwards
    assert server.get("/api/state").status == 200


def test_token_in_the_query_string_or_a_cookie_does_not_count(server):
    assert server.get("/api/state?token=" + server.token, token=None).status == 403
    assert server.get("/api/state", token=None, headers={"Cookie": "token=" + server.token}).status == 403
    assert server.get("/api/state", token=None, headers={"Authorization": "Bearer " + server.token}).status == 403
    assert server.get("/api/state", token=None, headers={"X-Token": server.token}).status == 403


def test_unauthenticated_writes_change_nothing(server, paths):
    ws = paths["ws"]
    before = tree(ws)
    for method, path, body in BODY_ROUTES:
        r = call(server, method, path, body, token=None)
        assert r.status == 403, (method, path, r.status)
    assert tree(ws) == before
    assert not (ws / ".env").exists()


def test_every_other_session_has_a_different_token_and_the_old_one_is_useless(make_server):
    a, b = make_server(), make_server()
    assert a.get("/api/state", token=b.token).status == 403
    assert b.get("/api/state", token=a.token).status == 403


def test_health_stays_open_but_reveals_nothing(server):
    r = server.get("/api/health", token=None)
    assert r.status == 200
    assert r.json() == {"ok": True}
    assert server.token not in r.text


# ===== Host header (DNS rebinding) ========================================================================================

@pytest.mark.parametrize("host_fmt", ["127.0.0.1:{p}", "localhost:{p}"])
def test_loopback_hosts_are_accepted(server, host_fmt):
    r = server.get("/api/state", host=host_fmt.format(p=server.port))
    assert r.status == 200


@pytest.mark.parametrize(
    "host_fmt",
    [
        "evil.example.com",
        "evil.example.com:{p}",
        "127.0.0.1.evil.example.com:{p}",
        "localhost.evil.example.com:{p}",
        "evil.example.com:{p}@127.0.0.1",
        "127.0.0.1:1",
        "localhost:1",
        "127.0.0.1",
        "localhost",
        "0.0.0.0:{p}",
        "[::1]:{p}",
        "127.0.0.2:{p}",
        "192.168.1.5:{p}",
        "127.0.0.1:{p}.evil.example.com",
        "127.0.0.1:{p}, evil.example.com",
        "",
    ],
)
def test_foreign_hosts_are_403_even_with_a_valid_token(server, host_fmt):
    host = host_fmt.format(p=server.port)
    r = server.get("/api/state", host=host)
    assert r.status == 403, (host, r.status, r.text)
    assert_json_error(r)


def test_missing_host_header_is_refused(server):
    r = server.get("/api/state", host=False)
    assert r.status in (400, 403)


@pytest.mark.parametrize("method,path,body", BODY_ROUTES, ids=BODY_IDS)
def test_foreign_host_cannot_write_even_with_the_token(server, paths, method, path, body):
    before = tree(paths["ws"])
    r = call(server, method, path, body, host="evil.example.com:%d" % server.port)
    assert r.status == 403
    assert tree(paths["ws"]) == before


def test_duplicate_host_headers_cannot_smuggle_a_foreign_host(server):
    s = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        s.sendall(
            ("GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nHost: evil.example.com\r\n%s: %s\r\n"
             "Connection: close\r\n\r\n" % (server.port, TOKEN_HEADER, server.token)).encode()
        )
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    status = int(data.split(b" ", 2)[1])
    assert status in (400, 403), data[:200]


# ===== CORS ===============================================================================================================

def _no_cors(r):
    bad = [k for k in r.headers if k.startswith("access-control-")]
    assert not bad, bad


def test_no_cors_headers_on_any_kind_of_response(drafted):
    origin = {"Origin": "https://evil.example.com"}
    responses = [
        drafted.get("/", token=None, headers=origin),
        drafted.get("/api/health", token=None, headers=origin),
        drafted.get("/api/state", headers=origin),
        drafted.get("/api/state", token=None, headers=origin),
        drafted.get("/api/nope", headers=origin),
        drafted.post("/api/lint", {"subject": "s", "body": "b"}, headers=origin),
    ]
    for r in responses:
        _no_cors(r)


@pytest.mark.parametrize("path", ["/api/state", "/api/health", "/api/sender", "/api/keys", "/"])
def test_preflight_is_not_granted(server, path):
    r = server.request(
        "OPTIONS", path, token=None, content_type=None,
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "PUT",
                 "Access-Control-Request-Headers": "content-type,x-vcoutreach-token"},
    )
    _no_cors(r)
    assert r.status != 200 or r.header("allow") is None or "*" not in (r.header("allow") or "")
    assert r.status in (200, 204, 403, 404, 405, 501)


def test_a_cross_origin_browser_request_cannot_read_data(server):
    """The browser only lets a page read a response when Access-Control-Allow-Origin allows it."""
    r = server.get("/api/health", token=None, headers={"Origin": "https://evil.example.com"})
    assert r.header("access-control-allow-origin") is None


# ===== Content-Type =======================================================================================================

BAD_TYPES = ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
             "application/xml", "text/json", "application/jsonp", None]


@pytest.mark.parametrize("ctype", BAD_TYPES, ids=[str(c) for c in BAD_TYPES])
@pytest.mark.parametrize("method,path,body", BODY_ROUTES, ids=BODY_IDS)
def test_non_json_content_type_is_415_and_changes_nothing(drafted, paths, method, path, body, ctype):
    before = tree(paths["ws"])
    r = drafted.request(method, path, raw=json.dumps(body).encode(), content_type=ctype)
    assert r.status == 415, (ctype, r.status, r.text)
    assert_json_error(r)
    assert tree(paths["ws"]) == before


@pytest.mark.parametrize("ctype", ["application/json", "application/json; charset=utf-8", "Application/JSON"])
def test_json_content_type_forms_are_accepted(server, ctype):
    r = server.request("POST", "/api/lint", raw=b'{"subject": "s", "body": "b"}', content_type=ctype)
    assert r.status == 200, (ctype, r.status, r.text)


# ===== body size ==========================================================================================================

def _big_valid_json(n: int) -> bytes:
    return json.dumps({"csv_text": "a" * n}).encode()


@pytest.mark.parametrize("method,path", [("PUT", "/api/sender"), ("POST", "/api/contacts/import"),
                                         ("PUT", "/api/keys"), ("PUT", "/api/drafts/ines-marlowe"),
                                         ("POST", "/api/lint"), ("POST", "/api/draft")])
def test_body_over_2_mb_is_413_and_stores_nothing(drafted, paths, method, path):
    before = tree(paths["ws"])
    body = _big_valid_json(MB2 + 10)
    r = drafted.request(method, path, raw=body)
    assert r.status == 413, (r.status, r.text[:200])
    assert_json_error(r)
    assert tree(paths["ws"]) == before
    assert drafted.alive()


def test_a_declared_huge_length_is_413_without_reading_it(server):
    s = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        s.sendall(
            ("PUT /api/sender HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%s: %s\r\nContent-Type: application/json\r\n"
             "Content-Length: 9999999999\r\nConnection: close\r\n\r\n{" % (server.port, TOKEN_HEADER, server.token)).encode()
        )
        s.settimeout(10)
        data = s.recv(65536)
    finally:
        s.close()
    assert data.startswith(b"HTTP/1."), data[:100]
    assert int(data.split(b" ", 2)[1]) == 413
    assert server.alive()


def test_a_large_but_legal_body_is_not_413(server):
    r = server.post("/api/contacts/import", {"csv_text": "id,name,email\n" + ("#" * 1_500_000)})
    assert r.status != 413 and r.status < 500
    assert server.alive()


@pytest.mark.parametrize("length", ["abc", "-5", "1e3"])
def test_bad_content_length_is_a_clean_client_error(server, length):
    s = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        s.sendall(
            ("PUT /api/sender HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%s: %s\r\nContent-Type: application/json\r\n"
             "Content-Length: %s\r\nConnection: close\r\n\r\n{}" % (server.port, TOKEN_HEADER, server.token, length)).encode()
        )
        data = s.recv(65536)
    finally:
        s.close()
    assert data.startswith(b"HTTP/1.")
    assert 400 <= int(data.split(b" ", 2)[1]) < 500
    assert b"Traceback" not in data
    assert server.alive()


# ===== malformed JSON is a clean 400 ======================================================================================

BAD_BODIES = [
    b"",
    b"not json",
    b"{",
    b'{"name": ',
    b"\xff\xfe\x00bad utf8",
    b"[" * 200000,
    b'{"a":' * 50000,
    b"\x00\x00\x00",
    b'{"name": "x"} trailing',
    b"NaN",
]


@pytest.mark.parametrize("raw", BAD_BODIES, ids=["empty", "text", "brace", "cut", "bad-utf8", "deep-array", "deep-object",
                                                "nul", "trailing", "nan"])
@pytest.mark.parametrize("method,path", [("PUT", "/api/sender"), ("PUT", "/api/contacts"), ("PUT", "/api/keys"),
                                         ("POST", "/api/draft"), ("POST", "/api/contacts/import")])
def test_malformed_json_is_400_json_and_the_server_survives(server, paths, method, path, raw):
    r = server.request(method, path, raw=raw)
    assert r.status in (400, 422), (r.status, r.text[:200])
    assert_json_error(r)
    assert not (paths["ws"] / "sender.json").exists() and not (paths["ws"] / ".env").exists()
    assert server.alive()


# ===== response headers ===================================================================================================

def _responses(s: UiServer):
    return {
        "page": s.get("/", token=None),
        "health": s.get("/api/health", token=None),
        "state": s.get("/api/state"),
        "403": s.get("/api/state", token=None),
        "404": s.get("/api/no-such-route"),
        "400": s.request("PUT", "/api/sender", raw=b"{"),
        "415": s.request("POST", "/api/lint", raw=b"x", content_type="text/plain"),
        "404-page": s.get("/no-such-page", token=None),
    }


@pytest.mark.parametrize("which", ["page", "health", "state", "403", "404", "400", "415", "404-page"])
def test_hardening_headers_on_every_response(server, which):
    r = _responses(server)[which]
    assert (r.header("cache-control") or "").lower().replace(" ", "") .find("no-store") >= 0, r.headers
    assert (r.header("x-content-type-options") or "").lower() == "nosniff", r.headers
    csp = r.header("content-security-policy")
    assert csp, r.headers
    assert re.search(r"\bdefault-src\b", csp)
    assert not re.search(r"https?:", csp, re.I), csp
    assert "*" not in csp.replace("'unsafe-inline'", ""), csp
    assert "//" not in csp, csp


def test_csp_forbids_remote_scripts_and_framing_hosts(server):
    csp = server.get("/", token=None).header("content-security-policy") or ""
    for directive in re.split(r";\s*", csp):
        if directive.startswith(("script-src", "default-src", "connect-src", "style-src", "img-src", "font-src")):
            for src in directive.split()[1:]:
                assert src.startswith("'") or src in ("data:", "blob:"), "non-local source %r in %r" % (src, directive)


def test_errors_are_json_with_proper_statuses(server):
    cases = {
        server.get("/api/no-such-route"): 404,
        server.get("/api/state", token=None): 403,
        server.request("PUT", "/api/sender", raw=b"{"): (400, 422),
        server.request("POST", "/api/lint", raw=b"x", content_type="text/plain"): 415,
    }
    for r, want in cases.items():
        assert r.status in (want if isinstance(want, tuple) else (want,)), (r.status, r.text)
        assert_json_error(r)


@pytest.mark.parametrize("method", ["DELETE", "PATCH", "TRACE", "CONNECT", "BREW"])
def test_unsupported_methods_are_refused_without_a_traceback(server, method):
    r = server.request(method, "/api/state", content_type=None)
    assert r.status in (400, 403, 404, 405, 501)
    assert "Traceback" not in r.text
    assert server.token not in r.text
    assert server.alive()


def test_head_and_unknown_api_prefix_do_not_leak(server):
    r = server.request("HEAD", "/api/state", token=None, content_type=None)
    assert r.status in (403, 404, 405, 501)
    assert r.body == b""


# ===== path traversal on draft ids ========================================================================================

CANARY = "CANARY-OUTSIDE-9d41"

TRAVERSAL_IDS = [
    "..", "../secret", "../../secret", "..%2fsecret", "..%2Fsecret", "%2e%2e/secret", "%2e%2e%2fsecret",
    "%2e%2e%2f%2e%2e%2fsecret", "..%5csecret", "..\\secret", "%2fetc%2fpasswd", "/etc/passwd", "%2Fetc%2Fpasswd",
    "....//secret", "..;/secret", "a/../../secret", "blocked/rhea-castellane", "blocked%2frhea-castellane",
    "ines-marlowe/..", "%00", "ines-marlowe%00", "ines-marlowe%00.md", "%2e", ".", "secret%2f..%2f..%2fsecret",
    "%252e%252e%252fsecret", "..%c0%afsecret", "C:%5cWindows", "~", "$HOME", "con", "%20", "a%0ab",
]


@pytest.fixture
def traversal_world(drafted, paths):
    ws, outside = paths["ws"], paths["outside"]
    (ws / "secret.md").write_text("to: x@example.com\nsubject: " + CANARY + "\nstatus: ok\n\n" + CANARY + " body\n")
    (ws / "secret").write_text(CANARY)
    (paths["tmp"] / "secret.md").write_text("to: x@example.com\nsubject: " + CANARY + "\nstatus: ok\n\n" + CANARY + "\n")
    (paths["tmp"] / "secret").write_text(CANARY)
    (outside / "secret.md").write_text(CANARY)
    (ws / ".env").write_text("OPENAI_API_KEY=" + CANARY + "\n")
    (ws / "drafts" / "secret.md").unlink(missing_ok=True)
    return drafted


@pytest.mark.parametrize("bad_id", TRAVERSAL_IDS)
def test_get_draft_rejects_traversal(traversal_world, bad_id):
    r = traversal_world.get("/api/drafts/" + bad_id)
    assert r.status in (400, 404), (bad_id, r.status, r.text[:200])
    assert CANARY not in r.text
    assert "root:" not in r.text
    assert traversal_world.alive()


@pytest.mark.parametrize("bad_id", TRAVERSAL_IDS)
def test_put_draft_rejects_traversal_and_writes_nothing(traversal_world, paths, bad_id):
    before_tmp = tree(paths["tmp"])
    r = traversal_world.put("/api/drafts/" + bad_id, {"subject": "PWNED", "body": "PWNED body"})
    assert r.status in (400, 404), (bad_id, r.status, r.text[:200])
    after_tmp = tree(paths["tmp"])
    changed = {k for k in set(before_tmp) | set(after_tmp) if before_tmp.get(k) != after_tmp.get(k)}
    assert not changed, changed
    assert b"PWNED" not in b"".join(after_tmp.values())


@pytest.mark.parametrize("bad_id", ["index.jsonl", "index", ".env", "sender.json", "contacts.csv", "ines-marlowe.md",
                                    "ines-marlowe.md.bak", "INES-MARLOWE"])
def test_get_draft_only_serves_real_draft_ids(traversal_world, bad_id):
    r = traversal_world.get("/api/drafts/" + bad_id)
    assert r.status in (400, 404), (bad_id, r.status, r.text[:200])
    assert CANARY not in r.text
    assert "OPENAI_API_KEY" not in r.text


def test_draft_id_with_a_query_string_is_still_checked(traversal_world):
    r = traversal_world.get("/api/drafts/..%2fsecret?x=1")
    assert r.status in (400, 404)
    assert CANARY not in r.text


def test_traversal_via_the_url_path_itself(traversal_world):
    for path in ("/api/drafts/../state", "/api/drafts/./ines-marlowe/../../../secret", "/api/drafts//ines-marlowe",
                 "/api/drafts/ines-marlowe/", "/api/drafts/ines-marlowe/extra", "//api/state"):
        r = traversal_world.get(path)
        assert CANARY not in r.text, path
        assert r.status in (200, 400, 403, 404, 405), (path, r.status)
        assert "Traceback" not in r.text


# ===== static file serving ================================================================================================

@pytest.mark.parametrize(
    "path",
    ["/.env", "/contacts.csv", "/sender.json", "/drafts/ines-marlowe.md", "/drafts/index.jsonl", "/../../etc/passwd",
     "/%2e%2e/%2e%2e/etc/passwd", "/..%2f..%2fetc%2fpasswd", "/static/../../../etc/passwd", "/vcoutreach/ui.py",
     "/ui.py", "/index.html/../../ui.py", "/etc/passwd", "/ui/index.html", "/data/ui/index.html",
     "/%2e%2e%5c%2e%2e%5cetc%5cpasswd", "/" + "../" * 20 + "etc/passwd"],
)
def test_arbitrary_files_are_not_served(drafted, paths, path):
    (paths["ws"] / ".env").write_text("OPENAI_API_KEY=" + CANARY + "\n")
    r = drafted.get(path, token=None)
    assert CANARY not in r.text, path
    assert "root:" not in r.text
    assert "def serve" not in r.text and "import " not in r.text[:2000]
    assert "ines.marlowe@example.com" not in r.text
    assert r.status in (200, 400, 403, 404, 405), (path, r.status)
    if r.status == 200:
        assert (r.header("content-type") or "").startswith("text/html"), "only the app page may be served"


# ===== symlinks ===========================================================================================================

OUTSIDE_DRAFT = "to: leak@example.com\nsubject: " + CANARY + "\nstatus: ok\nlane: first_touch\nprovider: template\n\nHi,\n\n" + CANARY + "\n\nSam\n"


def test_symlinked_draft_file_is_not_read_or_written(drafted, paths):
    target = paths["outside"] / "real.md"
    target.write_text(OUTSIDE_DRAFT)
    (paths["ws"] / "drafts" / "evil.md").symlink_to(target)
    r = drafted.get("/api/drafts/evil")
    assert CANARY not in r.text
    assert r.status in (400, 403, 404)
    listing = drafted.get("/api/drafts")
    assert CANARY not in listing.text
    w = drafted.put("/api/drafts/evil", {"subject": "PWNED", "body": "PWNED body"})
    assert w.status in (400, 403, 404)
    assert target.read_text() == OUTSIDE_DRAFT


def test_symlinked_blocked_draft_is_not_followed(drafted, paths):
    target = paths["outside"] / "real_blocked.md"
    target.write_text(OUTSIDE_DRAFT.replace("status: ok", "status: blocked"))
    (paths["ws"] / "drafts" / "blocked" / "evil.md").symlink_to(target)
    assert CANARY not in drafted.get("/api/drafts/evil").text
    assert CANARY not in drafted.get("/api/drafts").text
    drafted.put("/api/drafts/evil", {"subject": "PWNED", "body": "PWNED body"})
    assert "PWNED" not in target.read_text()


def test_overwriting_a_real_draft_through_a_swapped_symlink_is_blocked(drafted, paths):
    """A draft that is a symlink to an outside file must not be rewritten via the symlink."""
    victim = paths["outside"] / "victim.md"
    victim.write_text(OUTSIDE_DRAFT)
    link = paths["ws"] / "drafts" / "tobias-wren.md"
    link.unlink()
    link.symlink_to(victim)
    drafted.put("/api/drafts/tobias-wren", {"subject": "PWNED", "body": "PWNED body"})
    assert victim.read_text() == OUTSIDE_DRAFT


def test_symlinked_drafts_directory_is_not_followed(drafted, paths):
    ws = paths["ws"]
    stash = paths["outside"] / "drafts_elsewhere"
    stash.mkdir()
    (stash / "leak.md").write_text(OUTSIDE_DRAFT)
    for p in (ws / "drafts").glob("*.md"):
        p.unlink()
    (ws / "drafts" / "blocked" / "rhea-castellane.md").unlink()
    (ws / "drafts" / "blocked").rmdir()
    import shutil

    shutil.rmtree(ws / "drafts")
    (ws / "drafts").symlink_to(stash, target_is_directory=True)
    listing = drafted.get("/api/drafts")
    assert CANARY not in listing.text
    assert CANARY not in drafted.get("/api/drafts/leak").text
    snap = tree(stash)
    drafted.put("/api/drafts/leak", {"subject": "PWNED", "body": "PWNED body"})
    run = drafted.post("/api/draft", {"provider": "template"})
    assert tree(stash) == snap, "nothing may be written through a drafts symlink that leaves the workspace"
    assert run.status < 600


def test_symlinked_sender_and_contacts_are_not_written_through(server, paths):
    victim_s = paths["outside"] / "victim_sender.json"
    victim_c = paths["outside"] / "victim_contacts.csv"
    victim_s.write_text('{"name": "' + CANARY + '"}')
    victim_c.write_text("id,name,email\nv," + CANARY + ",v@example.com\n")
    (paths["ws"] / "sender.json").symlink_to(victim_s)
    (paths["ws"] / "contacts.csv").symlink_to(victim_c)
    before = (victim_s.read_text(), victim_c.read_text())
    server.put("/api/sender", SENDER_OK)
    server.put("/api/contacts", [contact_row()])
    server.post("/api/samples")
    assert (victim_s.read_text(), victim_c.read_text()) == before


def test_symlinked_sender_and_contacts_contents_are_not_read(server, paths):
    secret_s = paths["outside"] / "private_sender.json"
    secret_c = paths["outside"] / "private_contacts.csv"
    secret_s.write_text('{"name": "' + CANARY + '"}')
    secret_c.write_text("id,name,email\nv," + CANARY + ",v@example.com\n")
    (paths["ws"] / "sender.json").symlink_to(secret_s)
    (paths["ws"] / "contacts.csv").symlink_to(secret_c)
    for path in ("/api/sender", "/api/contacts", "/api/state"):
        assert CANARY not in server.get(path).text, path


def test_a_dangling_or_looping_symlink_never_crashes_the_server(server, paths):
    ws = paths["ws"]
    (ws / "sender.json").symlink_to(ws / "nowhere.json")
    (ws / "contacts.csv").symlink_to(ws / "contacts.csv")
    for method, path, body in [("GET", "/api/sender", None), ("GET", "/api/contacts", None), ("GET", "/api/state", None),
                               ("POST", "/api/samples", {})]:
        r = call(server, method, path, body)
        assert r.status < 600 and "Traceback" not in r.text, (path, r.status)
    assert server.alive()


def test_workspace_that_is_itself_a_symlink_still_works(make_server, paths):
    real = paths["tmp"] / "real_ws"
    real.mkdir()
    link = paths["tmp"] / "link_ws"
    link.symlink_to(real, target_is_directory=True)
    s = make_server(workspace=link)
    assert s.post("/api/samples").ok
    assert (real / "contacts.csv").is_file()


# ===== robustness a real browser will exercise ============================================================================

def test_an_idle_half_open_connection_does_not_block_other_clients(server):
    """Browsers open speculative connections. One stalled socket must not freeze the app."""
    idle = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    try:
        idle.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1")  # never finished
        t0 = time.time()
        assert server.get("/api/health", token=None, timeout=5).status == 200
        assert time.time() - t0 < 5
    finally:
        idle.close()


def test_many_parallel_requests_all_succeed(server):
    import concurrent.futures as cf

    def hit(_):
        return server.get("/api/state").status

    with cf.ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(hit, range(40)))
    assert set(statuses) == {200}


def test_the_session_token_is_not_in_any_authorized_response(drafted):
    load_samples(drafted)
    for path in ("/api/state", "/api/sender", "/api/contacts", "/api/drafts", "/api/send-plan", "/api/drafts/ines-marlowe"):
        r = drafted.get(path)
        assert drafted.token not in r.text, path
        assert drafted.token not in "".join(r.headers.values()), path


def test_html_page_has_no_inline_event_handlers_pointing_offsite(server):
    html = server.get("/", token=None).text
    assert not re.search(r"""<script[^>]+src\s*=\s*["']?(?:https?:)?//""", html, re.I)
    assert not re.search(r"""<link[^>]+href\s*=\s*["']?(?:https?:)?//""", html, re.I)
    assert not re.search(r"""<iframe[^>]+src\s*=\s*["']?(?:https?:)?//""", html, re.I)
    assert not re.search(r"""<form[^>]+action\s*=\s*["']?(?:https?:)?//""", html, re.I)
    assert not re.search(r"""fetch\(\s*["'`]https?://""", html)
    assert "XMLHttpRequest" not in html or "http://" not in html
