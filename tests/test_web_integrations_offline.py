"""The kept integrations (PitchBook, Enrow, Apollo, Gemini, Claude, Cloudflare tracking, Gmail), tested offline.

SPEC_V3 sections 2 to 5. No test here touches a network: urllib is replaced by a recorder, child processes
are replaced by a recorder, and heavy third party libraries are made unimportable in subprocesses.
No dash characters appear in this file.
"""
from __future__ import annotations

import email.message
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, REPO_ROOT, Web, analyze, body, cli_env, discovered_get_routes, fill_rule, page_text, read_draft_file,
    seed_contacts, write_env,
)

HOOK = "I came across your essay on how small teams ship weekly, and I found it interesting."

HEAVY_LIBRARIES = [
    "playwright", "curl_cffi", "anthropic", "openai", "google", "googleapiclient", "google_auth_oauthlib",
    "google_auth_httplib2", "httplib2", "oauthlib", "apscheduler", "requests", "httpx", "aiohttp", "bs4", "lxml",
    "pandas", "numpy", "selenium", "vertexai",
]

ALLOWED_HOST_SUFFIXES = (
    "apollo.io", "enrow.io", "enrow.com", "enrow.app", "googleapis.com", "google.com", "anthropic.com",
    "openai.com", "publikhq.com", "pitchbook.com", "brave.com",
)

FAMILIES = {
    "pitchbook": r"pitchbook",
    "email-verification and data providers": r"email-verification|data-provider",
    "gemini, claude and hook generation": r"gemini|model-provider|generate-hook|/enrich",
    "tracking and inbox": r"tracking|poll-replies|refresh-inbox",
}

NET_BLOCK = textwrap.dedent('''
    import socket, sys
    def _deny(*a, **k):
        raise OSError("network blocked by the test suite")
    socket.socket.connect = _deny
    socket.socket.connect_ex = _deny
    socket.getaddrinfo = _deny
    for _name in {blocked!r}:
        sys.modules[_name] = None
''')


def run_python(code: str, tmp_path: Path, *args: str, timeout: float = 120):
    home = Path(os.environ["HOME"])
    prelude = NET_BLOCK.format(blocked=HEAVY_LIBRARIES)
    return subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(code), *args], cwd=str(Path.cwd()), env=cli_env(home),
        capture_output=True, text=True, encoding="utf-8", timeout=timeout,
    )


# ---------------------------------------------------------------------------------------------------------------
# Imports with the heavy dependencies missing and no network
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("module", [
    "vcoutreach.web.enrichment.pitchbook",
    "vcoutreach.web.llm.chain",
])
def test_each_integration_module_imports_without_its_heavy_library_and_without_network(tmp_path, module):
    result = run_python("import importlib, sys; importlib.import_module(sys.argv[1]); print('imported')",
                        tmp_path, module)
    assert result.returncode == 0 and "imported" in result.stdout, result.stderr[-800:]


def test_every_module_in_the_package_imports_and_none_pulls_in_the_old_private_packages(tmp_path):
    code = '''
        import importlib, json, pkgutil, sys
        import vcoutreach.web as pkg
        failures = []
        for info in pkgutil.walk_packages(pkg.__path__, "vcoutreach.web."):
            try:
                importlib.import_module(info.name)
            except BaseException as exc:
                failures.append(f"{info.name}: {type(exc).__name__}: {str(exc)[:160]}")
        leaked = [m for m in ("publik_outreach", "scripts", "app", "apscheduler") if m in sys.modules and sys.modules[m]]
        print(json.dumps({"failures": failures, "leaked": leaked}))
    '''
    result = run_python(code, tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["failures"] == [], "\n".join(report["failures"])
    assert report["leaked"] == []


def test_the_app_starts_and_the_main_pages_render_with_every_heavy_library_missing(tmp_path):
    code = '''
        import sys
        from pathlib import Path
        from vcoutreach.web import create_app
        ws = Path(sys.argv[1]); ws.mkdir()
        app = create_app(ws, testing=True, send_transport=None)
        client = app.test_client()
        base = "http://127.0.0.1:8765"
        token = next(app.config[k] for k in ("ACCESS_TOKEN", "OUTREACH_TOKEN", "BOOTSTRAP_TOKEN") if app.config.get(k))
        assert client.get("/?token=" + token, base_url=base).status_code in (302, 303)
        for path in ("/crm", "/settings", "/crm/drafts", "/replies", "/crm/analytics", "/crm/send-plan"):
            status = client.get(path, base_url=base).status_code
            assert status == 200, (path, status)
        print("pages ok")
    '''
    result = run_python(code, tmp_path, str(tmp_path / "ws-blocked"))
    assert result.returncode == 0 and "pages ok" in result.stdout, result.stderr[-800:]


# ---------------------------------------------------------------------------------------------------------------
# Routes of each integration on an EMPTY workspace without keys
# ---------------------------------------------------------------------------------------------------------------

def _family_rules(w: Web, pattern: str, method: str):
    return sorted({fill_rule(rule.rule) for rule in w.app.url_map.iter_rules()
                   if rule.endpoint != "static" and method in (rule.methods or set()) and re.search(pattern, rule.rule)})


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_integration_routes_without_keys_answer_with_a_friendly_message_never_a_server_error(make_web, no_spawn, net, family):
    w = make_web()
    pattern = FAMILIES[family]
    posts, gets = _family_rules(w, pattern, "POST"), _family_rules(w, pattern, "GET")
    assert posts or gets, f"no route found for {family}; the kept features need routes"
    problems = []
    for path in posts:
        r = w.post(path, {}, follow=True)
        if r.status_code >= 500 or "Traceback" in body(r):
            problems.append(f"POST {path}: {r.status_code}")
    for path in gets:
        r = w.get(path)
        if r.status_code >= 500 or "Traceback" in body(r):
            problems.append(f"GET {path}: {r.status_code}")
    assert not problems, "\n".join(problems)


def test_child_processes_use_fixed_argument_lists_and_only_scripts_shipped_with_the_app(make_web, no_spawn, net):
    w = make_web()
    for pattern in FAMILIES.values():
        for path in _family_rules(w, pattern, "POST"):
            w.post(path, {"confirm": "x"}, follow=True)
        for path in _family_rules(w, pattern, "GET"):
            w.get(path)
    for path in ("/settings", "/email-verification", "/crm"):
        w.get(path)
    scripts = (REPO_ROOT / "vcoutreach" / "web" / "scripts").resolve()
    for call in no_spawn.calls:
        args = call["args"]
        assert not call["kwargs"].get("shell"), f"shell=True used: {args}"
        assert isinstance(args, (list, tuple)), f"command given as a string: {args!r}"
        for part in map(str, args):
            if part.endswith((".py", ".sh", ".swift")):
                assert Path(part).resolve().is_relative_to(scripts), f"{part} is not a shipped script"


# ---------------------------------------------------------------------------------------------------------------
# What leaves the machine
# ---------------------------------------------------------------------------------------------------------------

class FakeResponse:
    status = 200

    def __init__(self, payload: bytes):
        self._payload = payload
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, *args):
        return self._payload

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class NetRecorder:
    def __init__(self):
        self.requests = []

    @property
    def hosts(self):
        return sorted({urlsplit(r["url"]).hostname or "" for r in self.requests})

    def answer(self, url: str) -> FakeResponse:
        payload = {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": HOOK}, "finish_reason": "stop"}],
            "id": "msg_test", "type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": HOOK}], "ok": True, "results": [], "data": {},
        }
        return FakeResponse(json.dumps(payload).encode("utf-8"))


@pytest.fixture
def net(monkeypatch):
    import http.client
    import urllib.request

    recorder = NetRecorder()

    def record(target, data=None, timeout=None, **kw):
        url = target if isinstance(target, str) else target.full_url
        headers = {} if isinstance(target, str) else {k.lower(): v for k, v in target.header_items()}
        recorder.requests.append({"url": url, "headers": headers})
        return recorder.answer(url)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, target, data=None, timeout=None: record(target))
    monkeypatch.setattr(urllib.request, "urlopen", lambda target, data=None, timeout=None, **kw: record(target))

    def refuse(self, *a, **k):
        recorder.requests.append({"url": f"{'https' if 'HTTPS' in type(self).__name__ else 'http'}://{self.host}/", "headers": {}})
        raise OSError("blocked by the test suite")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
    return recorder


@pytest.mark.parametrize("provider, env, host", [
    ("publik", {"PUBLIK_API_KEY": "fakekey_publik_0123456789"}, "publikhq.com"),
    ("openai", {"OPENAI_API_KEY": "fakekey_openai_0123456789", "OPENAI_MODEL": "model-x"}, "api.openai.com"),
    ("anthropic", {"ANTHROPIC_API_KEY": "fakekey_claude_0123456789", "ANTHROPIC_MODEL": "model-y"}, "api.anthropic.com"),
    ("openai", {"OPENAI_API_KEY": "fakekey_openai_0123456789", "OPENAI_MODEL": "model-x",
                "OPENAI_BASE_URL": "https://models.example.com/v1"}, "models.example.com"),
], ids=["publik", "openai", "anthropic", "openai-with-the-users-own-base-url"])
def test_model_providers_are_called_only_on_a_click_and_only_at_their_own_https_host(make_web, net, provider, env, host):
    w = make_web(env, sender=True)
    seed_contacts(w, [ADA])
    assert net.requests == [], "nothing may leave the machine before a click"
    w.post(w.url("drafts_write"), {"provider": provider}, follow=True)
    assert net.hosts == [host], net.requests
    assert all(r["url"].startswith("https://") for r in net.requests)
    key = next(v for k, v in env.items() if k.endswith("_KEY"))
    assert any(key in " ".join(r["headers"].values()) for r in net.requests), "the key must travel in a header"
    assert read_draft_file(w.ws, "ada") is not None
    drafts_text = "".join(p.read_text(encoding="utf-8") for p in (w.ws / "drafts").rglob("*") if p.is_file())
    assert key not in drafts_text


def test_loading_pages_with_every_key_set_makes_no_outbound_request_and_starts_no_process(make_web, net, no_spawn):
    keys = {name: f"fakekey_{name.lower()}_0123456789" for name in (
        "PUBLIK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "ENROW_API_KEY", "APOLLO_API_KEY")}
    keys.update({"OPENAI_MODEL": "m", "ANTHROPIC_MODEL": "m", "CLOUDFLARE_WORKER_URL": "https://tracker.example.com",
                 "TRACKING_WORKER_URL": "https://tracker.example.com"})
    w = make_web(keys, sender=True)
    for path in discovered_get_routes():
        w.get(path)
    assert net.requests == [], [r["url"] for r in net.requests]


def test_clicking_each_integration_with_keys_set_only_reaches_known_https_hosts_or_the_users_worker(make_web, net, no_spawn):
    worker = "https://tracker.example.com"
    keys = {name: f"fakekey_{name.lower()}_0123456789" for name in (
        "PUBLIK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "ENROW_API_KEY", "APOLLO_API_KEY")}
    keys.update({"OPENAI_MODEL": "m", "ANTHROPIC_MODEL": "m", "CLOUDFLARE_WORKER_URL": worker,
                 "TRACKING_WORKER_URL": worker})
    w = make_web(keys, sender=True)
    seed_contacts(w, [ADA])
    for pattern in FAMILIES.values():
        for path in _family_rules(w, pattern, "POST"):
            w.post(path, {"provider": "template"}, follow=True)
        for path in _family_rules(w, pattern, "GET"):
            w.get(path)
    for request in net.requests:
        parts = urlsplit(request["url"])
        host = parts.hostname or ""
        if host == "127.0.0.1" and parts.scheme == "http" and parts.port == 9223 and parts.path.startswith("/json"):
            # the PitchBook button asks the user's own Chrome (its DevTools port on this machine, default 9223) which
            # tabs are open; nothing leaves the machine (SPEC_QUESTIONS.md Q11)
            continue
        assert parts.scheme == "https", request["url"]
        assert re.fullmatch(r"[a-z0-9.-]+", host) and not re.fullmatch(r"[0-9.]+", host), request["url"]
        assert host == "tracker.example.com" or host.endswith(ALLOWED_HOST_SUFFIXES), request["url"]


# ---------------------------------------------------------------------------------------------------------------
# The Cloudflare Worker URL setting
# ---------------------------------------------------------------------------------------------------------------

WORKER_FIELDS = ("CLOUDFLARE_WORKER_URL", "TRACKING_WORKER_URL", "TRACKING_URL", "CF_WORKER_URL", "WORKER_URL",
                 "tracking_worker_url", "worker_url", "cloudflare_url", "tracking_url")


def _post_worker_url(w: Web, value: str):
    data = {name: value for name in WORKER_FIELDS}
    for path in {w.url("keys_save"), "/settings"}:
        w.post(path, data, follow=True)


def test_the_tracking_worker_url_never_defaults_to_a_real_host(web):
    for path in ("/settings", "/crm/tracking", "/crm/analytics"):
        markup = web.html(path)
        for tag in re.findall(r"(?is)<input\b[^>]*>", markup):
            value = re.search(r"""value=["']([^"']*)["']""", tag)
            if value:
                assert not re.search(r"(?i)workers\.dev|cloudflare|pages\.dev", value.group(1)), tag


@pytest.mark.parametrize("bad", [
    "http://tracker.example.com", "javascript:alert(1)",
], ids=["plain-http", "javascript"])
def test_a_worker_url_that_is_not_https_is_rejected_and_stored_nowhere(make_web, net, bad):
    w = make_web()
    _post_worker_url(w, bad)
    for path, text in workspace_text_files(w.ws).items():
        assert bad not in text, f"{bad} was stored in {path.name}"
    w.get("/crm/tracking")
    w.get("/crm/analytics")
    assert net.requests == []


def test_a_valid_https_worker_url_is_stored_in_the_workspace_and_is_the_only_tracking_host_used(make_web, net):
    w = make_web()
    _post_worker_url(w, "https://tracker.example.com")
    stored = [p for p, text in workspace_text_files(w.ws).items() if "https://tracker.example.com" in text]
    assert stored, "a validated https worker URL should be saved in the workspace"
    w.get("/crm/tracking")
    for path in _family_rules(w, r"tracking", "POST"):
        w.post(path, {}, follow=True)
    assert set(net.hosts) <= {"tracker.example.com"}, net.hosts
