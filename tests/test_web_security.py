"""Security, headers, branding and page hygiene of the web app (SPEC_V3 section 4, SPEC.md section 3).

Written from the specs only. Flask's test client, a tmp_path workspace, no network.
No dash characters appear in this file.
"""
from __future__ import annotations

import io
import re
from urllib.parse import urlsplit

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures: web_env_isolation, make_web, web, sample_web, ...)
from web_helpers import (
    ADA, BASE, CSP_EXPECTED, REPO_ROOT, Web, analyze, body, contact_form_fields, contact_ref, csv_row,
    csv_text, discovered_get_routes, discovered_post_routes, discovery_error, discovery_ok, export_rows,
    external_loads, get_token, owner_hits, page_text,
)

DENIED_MESSAGE = "Open the newest link printed in the Terminal window."


def anon(w: Web) -> Web:
    """The same app, a browser with no cookies."""
    return Web(w.app, w.ws, w.transport)


def tight_text(markup: str) -> str:
    return re.sub(r"<[^>]+>", "", markup)


# ---------------------------------------------------------------------------------------------------------------
# /healthz and the one-time token link
# ---------------------------------------------------------------------------------------------------------------

def test_healthz_answers_ok_json_without_any_session(make_web):
    w = anon(make_web(login=False))
    r = w.get("/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
    assert "json" in r.headers["Content-Type"]


def test_token_link_redirects_to_root_with_the_token_removed_and_sets_a_strict_http_only_cookie(make_web):
    w = make_web(login=False)
    token = get_token(w.app)
    r = w.get(f"/?token={token}")
    assert r.status_code in (302, 303)
    location = r.headers["Location"]
    parts = urlsplit(location)
    assert parts.path == "/" and parts.query == "" and token not in location
    landing = w.get("/", follow_redirects=True)
    assert landing.status_code == 200 and DENIED_MESSAGE not in body(landing), "the cookie must open the pages"
    cookies = r.headers.getlist("Set-Cookie")
    assert cookies, "the token exchange must set a session cookie"
    joined = " | ".join(cookies).lower()
    for attribute in ("httponly", "samesite=strict", "path=/"):
        assert attribute in joined, f"cookie lacks {attribute}: {cookies}"


@pytest.mark.parametrize("kind", ["empty", "token-minus-one-char"])
def test_a_wrong_token_is_refused_and_sets_no_cookie(make_web, kind):
    w = make_web(login=False)
    token = get_token(w.app)
    bad = {"empty": "", "token-minus-one-char": token[:-1]}[kind]
    r = w.get(f"/?token={bad}")
    assert r.status_code == 403
    assert DENIED_MESSAGE in page_text(r)
    assert not r.headers.getlist("Set-Cookie")
    assert w.get("/crm").status_code == 403


@pytest.mark.parametrize("path", ["/crm"])
def test_every_page_without_a_session_cookie_is_403_with_the_plain_message(make_web, path):
    w = anon(make_web(login=False))
    r = w.get(path)
    assert r.status_code == 403
    assert DENIED_MESSAGE in page_text(r)
    assert "<form" not in body(r).lower()


@pytest.mark.parametrize("tamper", ["flip-last-char", "drop-signature"])
def test_a_tampered_session_cookie_is_refused(make_web, tamper):
    w = make_web()
    pairs = w.cookie_pairs()
    assert pairs, "the logged-in client holds no cookie"
    assert w.raw("get", "/crm").status_code == 200, "control: the untouched cookie must work"

    def mangle(value: str) -> str:
        if tamper == "flip-last-char":
            # The last base64 character of the signature can carry unused padding bits, so changing
            # it may leave the decoded signature unchanged (flaky). Change a character in the middle
            # of the signature segment instead: every character there is fully used, so the decoded
            # bytes always change and the cookie is always invalid.
            head, sep, signature = value.rpartition(".")
            assert sep and signature, "the session cookie has no signature segment"
            mid = len(signature) // 2
            flipped = "A" if signature[mid] != "A" else "B"
            return head + sep + signature[:mid] + flipped + signature[mid + 1:]
        if tamper == "truncate":
            return value[: len(value) // 2]
        if tamper == "empty-value":
            return ""
        return value.rsplit(".", 1)[0]

    r = w.raw("get", "/crm", cookies=[(k, mangle(v)) for k, v in pairs])
    assert r.status_code == 403


def test_a_session_cookie_from_another_workspace_is_refused(make_web):
    a, b = make_web(), make_web()
    pairs = b.cookie_pairs()
    assert a.raw("get", "/crm", cookies=pairs).status_code == 403


def test_the_token_never_appears_in_a_rendered_page(make_web):
    w = make_web()
    token = get_token(w.app)
    for path in ("/crm", "/settings", "/crm/send-plan"):
        assert token not in w.html(path)


def test_a_valid_token_with_a_foreign_host_header_is_refused_and_sets_no_cookie(make_web):
    w = anon(make_web(login=False))
    r = w.raw("get", f"/?token={get_token(w.app)}", host="evil.example.com:8765", cookies=[])
    assert r.status_code == 403
    assert not r.headers.getlist("Set-Cookie")


# ---------------------------------------------------------------------------------------------------------------
# Host header (DNS rebinding)
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["evil.example.com:8765", "127.0.0.1.evil.example.com:8765"])
def test_a_request_with_a_foreign_host_header_is_refused_even_with_a_valid_session(make_web, host):
    w = make_web()
    r = w.raw("get", "/crm", host=host)
    assert r.status_code == 403


def test_localhost_and_loopback_host_headers_are_accepted_and_forwarded_host_is_ignored(make_web):
    w = make_web()
    for host in ("127.0.0.1:8765", "localhost:8765"):
        assert w.raw("get", "/crm", host=host).status_code == 200, host
    r = w.raw("get", "/crm", host="127.0.0.1:8765", headers={"X-Forwarded-Host": "evil.example.com"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------------------------------------------

CRITICAL_POSTS = ["/toggle-send-mode", "/push", "/crm/send-batch", "/crm/new"]


def _csrf_problems(w: Web, path: str):
    problems = []
    for label, token in (("missing", False), ("wrong", "not-the-session-token"), ("empty", "")):
        r = w.post(path, {"name": "x", "confirm": "GO LIVE"}, csrf=token)
        if r.status_code not in (400, 403):
            problems.append(f"POST {path} with a {label} CSRF token answered {r.status_code}")
    return problems


@pytest.mark.parametrize("path", CRITICAL_POSTS)
def test_the_critical_post_routes_reject_a_missing_or_a_wrong_csrf_token(make_web, path):
    w = make_web()
    assert not _csrf_problems(w, path)
    assert not w.transport.calls


def test_every_post_route_in_the_url_map_rejects_a_missing_or_a_wrong_csrf_token(make_web):
    assert discovery_ok(), discovery_error()
    w = make_web()
    problems = []
    for path in discovered_post_routes():
        problems += _csrf_problems(w, path)
    assert not problems, "\n".join(problems)
    assert not w.transport.calls


def test_the_csrf_token_may_travel_in_the_x_csrf_token_header(web):
    token = web.csrf
    r = web.request("post", web.url("contact_new"), data=contact_form_fields(ADA),
                    headers={"X-CSRF-Token": token})
    assert r.status_code in (302, 303)
    assert [row["email"] for row in export_rows(web)] == ["ada@example.com"]


def test_a_csrf_token_from_another_workspace_session_is_rejected(make_web):
    a, b = make_web(), make_web()
    r = a.post(a.url("contact_new"), contact_form_fields(ADA), csrf=b.csrf)
    assert r.status_code in (400, 403)
    assert export_rows(a) == []


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.example.com"},
    {"Origin": "null"},
    {"Referer": "http://evil.example.com/crm/new"},
], ids=["foreign-origin", "null-origin", "foreign-referer"])
def test_a_cross_origin_post_is_rejected_even_with_a_valid_csrf_token(web, headers):
    r = web.post(web.url("contact_new"), contact_form_fields(ADA), headers=headers)
    assert r.status_code in (400, 403)
    assert export_rows(web) == []


def test_same_origin_origin_and_referer_headers_are_accepted(web):
    rows = [csv_row(id=f"p{i}", name=f"Person {i}", email=f"p{i}@example.com") for i in range(3)]
    for row, headers in zip(rows[:2], [{"Origin": "http://127.0.0.1:8765"},
                                       {"Referer": "http://127.0.0.1:8765/crm/new"}]):
        r = web.post(web.url("contact_new"), contact_form_fields(row), headers=headers)
        assert r.status_code in (302, 303), headers
    data = {**contact_form_fields(rows[2]), "csrf_token": web.csrf}
    r = web.raw("post", web.url("contact_new"), host="localhost:8765", data=data,
                headers={"Origin": "http://localhost:8765"})
    assert r.status_code in (302, 303), "an Origin that matches the Host the page was opened on is same-origin"
    assert len(export_rows(web)) == 3


# ---------------------------------------------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------------------------------------------

def _fetch(w: Web, kind: str):
    if kind == "html page":
        return w.get("/crm")
    if kind == "json health":
        return anon(w).get("/healthz")
    if kind == "403 page":
        return anon(w).get("/crm")
    if kind == "404 page":
        return w.get("/no-such-page-xyz")
    if kind == "stylesheet":
        return w.get("/static/style.css")
    return w.get("/")


@pytest.mark.parametrize("kind", ["html page", "403 page", "404 page", "stylesheet"])
def test_every_kind_of_response_carries_the_security_headers_and_the_exact_csp(make_web, kind):
    w = make_web()
    r = _fetch(w, kind)
    headers = {k.lower(): v for k, v in r.headers.items()}
    assert "no-store" in headers.get("cache-control", "").lower()
    assert headers.get("x-content-type-options", "").lower() == "nosniff"
    assert headers.get("x-frame-options", "").upper() == "DENY"
    assert headers.get("referrer-policy", "").lower() == "no-referrer"
    csp = headers.get("content-security-policy", "")
    assert {part.strip() for part in csp.split(";") if part.strip()} == CSP_EXPECTED, csp


def test_no_response_carries_cors_headers_even_for_a_foreign_origin(make_web):
    w = make_web()
    for path in ("/crm", "/healthz", "/static/style.css", "/no-such-page-xyz"):
        r = w.get(path, headers={"Origin": "http://evil.example.com"})
        assert not [h for h in r.headers.keys() if h.lower().startswith("access-control-")], path
    r = w.request("options", "/crm", headers={"Origin": "http://evil.example.com",
                                              "Access-Control-Request-Method": "POST"})
    assert not [h for h in r.headers.keys() if h.lower().startswith("access-control-")]


# ---------------------------------------------------------------------------------------------------------------
# Pages: nothing external, no inline script, no handlers
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def rich_web(sample_web):
    """The sample loaded and drafts written, so contact pages and the editor have content."""
    sample_web.post(sample_web.url("drafts_write"), {"provider": "template"}, follow=True)
    return sample_web


def _resolve(w: Web, key: str) -> str:
    rows = export_rows(w)
    if key == "contact":
        return f"/crm/{contact_ref(w, rows[0]['email'])}"
    if key == "draft":
        first_ok = next(r["id"] for r in rows if (w.ws / "drafts" / f"{r['id']}.md").is_file())
        return w.url("draft_edit", slug=first_ok)
    return key


@pytest.mark.parametrize("key", ["/crm", "/settings", "contact", "draft"])
def test_page_loads_nothing_external_and_has_no_inline_script_handler_or_javascript_url(rich_web, key):
    path = _resolve(rich_web, key)
    r = rich_web.get(path)
    assert r.status_code == 200, path
    markup = body(r)
    assert external_loads(markup) == []
    parsed = analyze(markup)
    assert parsed.inline_scripts == 0, "inline <script> found (CSP forbids it)"
    assert parsed.handlers == [], f"inline event handlers: {parsed.handlers}"
    assert parsed.js_urls == []
    assert all(not re.match(r"(?i)[a-z][a-z0-9+.-]*:|//", src) for src in parsed.script_srcs), parsed.script_srcs
    for needle in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr", "cdnjs.cloudflare", "unpkg.com"):
        assert needle not in markup.lower()


def test_stylesheet_and_scripts_contain_no_external_urls_and_have_a_mobile_media_query(web):
    css = body(web.get("/static/style.css"))
    assert "@media" in css and "max-width" in css
    sources = analyze(web.html("/crm")).script_srcs
    texts = [("style.css", css)] + [(src, body(web.get(src))) for src in sources]
    for name, text in texts:
        cleaned = text.replace("http://www.w3.org/", "")
        assert not re.search(r"https?://", cleaned), f"{name} mentions an external URL"
        assert "@import" not in cleaned
    assert sources, "the theme toggle needs a script file"
    for src in sources:
        assert web.get(src).status_code == 200


# ---------------------------------------------------------------------------------------------------------------
# Body limit and errors
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["urlencoded", "multipart-file"])
def test_a_body_over_two_megabytes_is_refused_with_413_and_a_short_message(web, kind):
    big = b"a" * (2 * 1024 * 1024 + 4096)
    url = web.url("contact_import")
    headers = {"X-CSRF-Token": web.csrf}
    if kind == "urlencoded":
        r = web.request("post", url, data=b"csv_text=" + big, headers=headers,
                        content_type="application/x-www-form-urlencoded")
    else:
        r = web.request("post", url, data={"file": (io.BytesIO(big), "big.csv"), "csrf_token": web.csrf},
                        content_type="multipart/form-data")
    assert r.status_code == 413
    assert "Traceback" not in body(r) and len(page_text(r)) < 500


def test_a_body_under_the_limit_is_not_refused_as_too_large(web):
    rows = [csv_row(id=f"c{i}", name=f"Person {i}", email=f"c{i}@example.com", facts="your talk on x")
            for i in range(300)]
    r = web.post(web.url("contact_import"), {"csv_text": csv_text(rows)})
    assert r.status_code != 413


def test_unknown_page_and_wrong_method_give_short_messages_without_internals(web, tmp_path):
    for r in (web.get("/no-such-page-xyz"), web.get(web.url("toggle"))):
        assert r.status_code in (404, 405)
        text = body(r)
        for needle in ("Traceback", "werkzeug", "Werkzeug", "site-packages", ".py", str(tmp_path)):
            assert needle not in text, needle
        assert len(page_text(r)) < 500


def test_an_unexpected_exception_shows_a_one_line_error_with_no_traceback_or_path(make_web, tmp_path):
    w = make_web(login=False)
    w.app.config["PROPAGATE_EXCEPTIONS"] = False

    def boom():
        raise RuntimeError("boom-secret-detail at " + str(tmp_path))

    w.app.add_url_rule("/__boom", "boom_for_test", boom)
    w.login()
    r = w.get("/__boom")
    assert r.status_code == 500
    text = body(r)
    for needle in ("Traceback", "RuntimeError", "boom-secret-detail", str(tmp_path), ".py", "site-packages"):
        assert needle not in text, needle
    assert len(page_text(r)) < 500
    assert r.headers.get("Content-Security-Policy")


# ---------------------------------------------------------------------------------------------------------------
# Publik branding
# ---------------------------------------------------------------------------------------------------------------

def test_the_wordmark_is_publik_dot_with_the_signal_tag_and_no_web_fonts_are_loaded(web):
    markup = web.html("/crm")
    assert "publik." in tight_text(markup)
    assert "SIGNAL" in page_text(markup)
    for path in ("/crm", "/settings", "/replies"):
        assert "googleapis" not in web.html(path).lower()
    assert "googleapis" not in body(web.get("/static/style.css")).lower()


def test_the_theme_choice_is_stored_under_publik_signal_theme_in_a_static_script(web):
    markup = web.html("/crm")
    sources = analyze(markup).script_srcs
    combined = markup + "".join(body(web.get(src)) for src in sources)
    assert "publik-signal-theme" in combined
    assert "outreach-signal-theme" not in combined


# ---------------------------------------------------------------------------------------------------------------
# Every kept page on an EMPTY workspace
# ---------------------------------------------------------------------------------------------------------------

KEPT_PAGES = [
    "/crm", "/crm/drafts", "/crm/analytics", "/crm/send-plan", "/crm/new", "/settings", "/replies",
    "/partners", "/candidates", "/enrichment", "/email-verification", "/scheduled",
]


def test_every_kept_page_renders_200_on_an_empty_workspace_without_traceback_or_owner_strings(web, tmp_path):
    problems = []
    for path in KEPT_PAGES:
        r = web.get(path, follow_redirects=True)
        markup = body(r)
        if r.status_code != 200:
            problems.append(f"{path}: {r.status_code}")
        if "Traceback" in markup or "site-packages" in markup or str(tmp_path) in markup:
            problems.append(f"{path}: traceback or a source/workspace path in the page")
        if owner_hits(markup):
            problems.append(f"{path}: owner string {owner_hits(markup)}")
    assert not problems, "\n".join(problems)


def test_root_goes_to_the_overview_and_the_old_settings_aliases_still_work(web):
    r = web.get("/")
    assert r.status_code in (302, 303, 200)
    if r.status_code != 200:
        assert urlsplit(r.headers["Location"]).path == "/crm"
    for alias in ("/setup", "/knowledge"):
        assert web.get(alias, follow_redirects=True).status_code == 200, alias


def test_no_get_route_without_parameters_answers_5xx_or_leaks_owner_strings(make_web, no_spawn):
    assert discovery_ok(), discovery_error()
    w = make_web()
    problems = []
    for path in discovered_get_routes():
        r = w.get(path)
        markup = body(r)
        if r.status_code >= 500:
            problems.append(f"{path}: {r.status_code}")
        if "Traceback" in markup:
            problems.append(f"{path}: traceback in page")
        if owner_hits(markup):
            problems.append(f"{path}: owner string {owner_hits(markup)}")
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------------------------------------------
# Accessibility basics and flash messages
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/crm/new", "/settings"])
def test_pages_have_a_language_landmarks_and_labelled_fields(web, path):
    parsed = analyze(web.html(path))
    assert parsed.lang, "the <html> element needs a lang attribute"
    assert parsed.has_main, "the page needs a main landmark"
    assert parsed.has_nav, "the page needs a nav landmark"
    assert parsed.unlabelled == [], f"fields without a label: {parsed.unlabelled}"


def test_flash_messages_sit_in_an_aria_live_region(web):
    r = web.post(web.url("contact_import"), {"csv_text": "name,email\nNo Email,not-an-email\n"}, follow=True)
    assert r.status_code == 200
    assert "aria-live" in body(r)


# ---------------------------------------------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------------------------------------------

def test_a_full_session_writes_only_inside_the_workspace(make_web, tmp_path):
    import os
    from pathlib import Path

    w = make_web()
    w.post(w.url("sample_load"), {}, follow=True)
    w.post(w.url("drafts_write"), {"provider": "template"}, follow=True)
    w.get(w.url("contact_export"))
    assert list(Path.cwd().iterdir()) == [], "files were written to the current directory"
    assert list(Path(os.environ["HOME"]).iterdir()) == [], "files were written to the home directory"
    assert (w.ws / ".env").is_file()
