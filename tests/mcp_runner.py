"""Child process wrapper used by the MCP tests (tests/mcp_helpers.py launches it).

It is NOT a test module. It installs the test doubles that a real first-time user's machine would not need
(no real network, no real Keychain, a fake Gmail service), then starts the public entry point exactly as a
client would: ``vcoutreach.cli.main(["mcp", "--workspace", <ws>])``.

Configuration arrives as JSON in the environment variable MCP_TEST_CONFIG:

    workspace      the workspace folder
    log            path of a JSON lines log this process appends to
    http_rules     [{"match": "<url substring>", "responses": [<response>, ...]}]; the last response repeats
                   <response> = {"status": 200, "hook_text": "..."} | {"status": 401, "body": "...",
                   "echo_auth": true} | {"fail": "reason"}  (fail raises the engine TransportFailure)
    hook_text      text returned for any /chat/completions call that no rule matched
    hook_subject   subject returned by the stubbed call_llm
    enrich         {"<source>": {"facts": [str | {"type","text"}], "errors": [str], "url": str}}
    gmail          true: replace the Gmail service with a fake that records every message
    faults         {"<tool>": "<exception message>"}: that handler raises RuntimeError(message)
    returns        {"<tool>": {...}}: that handler returns this dict instead of doing its work

Every record in the log has a "kind": http, call_llm, enrich, gmail_send, net_blocked, fault.
The log never contains an API key value (only whether an Authorization header was present).

No dash characters appear in this file.
"""
from __future__ import annotations

import base64
import email
import json
import os
import socket
import subprocess
import sys
import threading
from email import policy
from urllib.parse import urlsplit

CFG = json.loads(os.environ["MCP_TEST_CONFIG"])
LOG = CFG["log"]
_LOCK = threading.Lock()
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
DEFAULT_HOOK = ("I read your essay on weekly shipping. It was a good reminder that steady releases keep a small "
                "team honest, and that kind of discipline is rare in early stage companies.")
DEFAULT_SUBJECT = "A note on weekly shipping habits"


def log(**record):
    with _LOCK:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------------------------------------------
# Network guard: only loopback is reachable. Anything else is logged and refused.
# ---------------------------------------------------------------------------------------------------------------

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guard_connect(self, address):
    if isinstance(address, tuple) and address and str(address[0]) not in _LOOPBACK:
        log(kind="net_blocked", host=str(address[0]))
        raise OSError("network blocked by the test runner")
    return _real_connect(self, address)


def _guard_connect_ex(self, address):
    if isinstance(address, tuple) and address and str(address[0]) not in _LOOPBACK:
        log(kind="net_blocked", host=str(address[0]))
        return 1
    return _real_connect_ex(self, address)


def _guard_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host not in _LOOPBACK and host is not None:
        log(kind="net_blocked", host=str(host))
        raise socket.gaierror("network blocked by the test runner")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
socket.getaddrinfo = _guard_getaddrinfo


# ---------------------------------------------------------------------------------------------------------------
# Real Keychain: never touched. The engine falls back to the workspace .env.
# ---------------------------------------------------------------------------------------------------------------

try:
    from vcoutreach.web.server import runtime_secrets

    runtime_secrets._run = lambda *a, **k: subprocess.CompletedProcess(a, 2, b"", b"keychain_item_missing")
except Exception as exc:  # pragma: no cover - only if the module moved
    log(kind="runner_warning", detail="keychain stub not installed: " + type(exc).__name__)


# ---------------------------------------------------------------------------------------------------------------
# HTTP: the engine transport. Loopback passes through; the rules answer everything else.
# ---------------------------------------------------------------------------------------------------------------

from vcoutreach.providers import http as _http  # noqa: E402

_real_request = _http.UrllibTransport.request
_counters = {}


def _respond(rule, auth):
    if rule.get("fail"):
        raise _http.TransportFailure(str(rule["fail"]))
    status = int(rule.get("status", 200))
    if "hook_text" in rule:
        payload = {"choices": [{"index": 0, "message": {"role": "assistant", "content": rule["hook_text"]},
                                "finish_reason": "stop"}]}
        data = json.dumps(payload).encode("utf-8")
    else:
        data = str(rule.get("body", "")).encode("utf-8")
    if rule.get("echo_auth"):
        data += (" " + auth).encode("utf-8")
    headers = {"content-type": "application/json"}
    headers.update(rule.get("headers", {}))
    return _http.HttpResponse(status, headers, data)


def _fake_request(self, method, url, headers, body, timeout):
    host = (urlsplit(url).hostname or "").lower()
    auth = ""
    for key, value in dict(headers).items():
        if str(key).lower() == "authorization":
            auth = str(value)
    log(kind="http", method=method, url=url, has_auth=bool(auth))
    if host in _LOOPBACK:
        return _real_request(self, method, url, headers, body, timeout)
    for index, rule in enumerate(CFG.get("http_rules", [])):
        if rule["match"] in url:
            seq = rule["responses"]
            n = _counters.get(index, 0)
            _counters[index] = n + 1
            return _respond(seq[min(n, len(seq) - 1)], auth)
    if "/chat/completions" in url:
        return _respond({"status": 200, "hook_text": CFG.get("hook_text", DEFAULT_HOOK)}, auth)
    raise _http.TransportFailure("no route in the test runner")


_http.UrllibTransport.request = _fake_request


# ---------------------------------------------------------------------------------------------------------------
# Model calls on the web (not engine) path
# ---------------------------------------------------------------------------------------------------------------

def _fake_call_llm(prompt, json_mode=True, model_pref="auto"):
    log(kind="call_llm", model_pref=str(model_pref))
    return {"hook": CFG.get("hook_text", DEFAULT_HOOK), "subject": CFG.get("hook_subject", DEFAULT_SUBJECT),
            "_model_used": "stub-model"}


try:
    from vcoutreach.web.llm import chain as _chain
    from vcoutreach.web.llm import hook_generator as _hook_generator

    _chain.call_llm = _fake_call_llm
    _hook_generator.call_llm = _fake_call_llm
except Exception as exc:  # pragma: no cover
    log(kind="runner_warning", detail="call_llm stub not installed: " + type(exc).__name__)


# ---------------------------------------------------------------------------------------------------------------
# Enrichment sources: the engine runs, the network part is replaced by configured facts
# ---------------------------------------------------------------------------------------------------------------

try:
    from vcoutreach.web.enrichment import orchestrator as _orch

    def _make_enrich(source):
        def fake(partner, partner_id, conn):
            spec = CFG.get("enrich", {}).get(source, {})
            facts = []
            for item in spec.get("facts", []):
                entry = {"type": "fact", "text": item} if isinstance(item, str) else dict(item)
                entry.setdefault("type", "fact")
                entry["source_url"] = spec.get("url") or ("https://example.com/" + source)
                entry["confidence"] = 0.85
                facts.append(entry)
            log(kind="enrich", source=source, partner_id=partner_id, facts=len(facts))
            if facts:
                _orch._upsert_enrichment_row(conn, partner_id, source, spec.get("url") or ("https://example.com/" + source),
                                             "stub text for " + source, facts, 0.85)
            return {"errors": list(spec.get("errors", [])), "facts_count": len(facts)}
        return fake

    _orch._run_web_search = _make_enrich("web_search")
    _orch._run_linkedin = _make_enrich("linkedin")
    _orch._run_email_guess = _make_enrich("email_guess")
    _orch._run_firm_website = _make_enrich("firm_website")
    _orch._run_pitchbook = _make_enrich("pitchbook")
except Exception as exc:  # pragma: no cover
    log(kind="runner_warning", detail="enrichment stubs not installed: " + type(exc).__name__)


# ---------------------------------------------------------------------------------------------------------------
# Gmail: a fake service that records the message it is asked to send
# ---------------------------------------------------------------------------------------------------------------

if CFG.get("gmail"):
    _sent = []

    class _Exec:
        def __init__(self, fn):
            self._fn = fn

        def execute(self):
            return self._fn()

    class _Messages:
        def send(self, userId=None, body=None, **kwargs):
            def go():
                raw = body["raw"]
                data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                message = email.message_from_bytes(data, policy=policy.default)
                _sent.append(message)
                log(kind="gmail_send",
                    to=[str(v) for v in message.get_all("To", [])],
                    cc=[str(v) for v in message.get_all("Cc", [])],
                    bcc=[str(v) for v in message.get_all("Bcc", [])],
                    sender=str(message.get("From", "")),
                    subject=str(message.get("Subject", "")))
                return {"id": "fake-%d" % len(_sent)}
            return _Exec(go)

    class _Users:
        def messages(self):
            return _Messages()

        def getProfile(self, userId=None):
            return _Exec(lambda: {"emailAddress": "sender@example.com"})

    class _Service:
        def users(self):
            return _Users()

    try:
        from vcoutreach.web.gmail import api as _gmail_api

        _gmail_api.get_service = lambda *a, **k: _Service()
    except Exception as exc:  # pragma: no cover
        log(kind="runner_warning", detail="gmail stub not installed: " + type(exc).__name__)


# ---------------------------------------------------------------------------------------------------------------
# Handler faults and canned returns (an MCP handler table: name -> callable(dict) -> dict)
# ---------------------------------------------------------------------------------------------------------------

if CFG.get("faults") or CFG.get("returns"):
    from vcoutreach.mcp import tools as _tools

    def _raiser(name, message):
        def handler(arguments):
            log(kind="fault", tool=name)
            raise RuntimeError(message)
        return handler

    def _canned(name, payload):
        def handler(arguments):
            return json.loads(json.dumps(payload))
        return handler

    for _name, _message in CFG.get("faults", {}).items():
        _tools.HANDLERS[_name] = _raiser(_name, _message)
    for _name, _payload in CFG.get("returns", {}).items():
        _tools.HANDLERS[_name] = _canned(_name, _payload)


from vcoutreach.cli import main  # noqa: E402

sys.exit(main(["mcp", "--workspace", CFG["workspace"]]))
