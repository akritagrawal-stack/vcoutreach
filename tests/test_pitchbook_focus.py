"""PitchBook card: bring the dedicated Chrome to the front and report sign-in status.

Spec only. The Flask test client talks to the app; the boundary is faked:
* ``urlopen`` in the app module is replaced by a fake Chrome DevTools (CDP) endpoint on loopback.
* ``subprocess`` entry points are replaced by a recorder with a fake ``lsof`` and a fake ``osascript``.
No real browser, no AppleScript and no network is ever started. No dash characters appear in this file.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime as real_datetime
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest

from web_helpers import *  # noqa: F401,F403

DEFAULT_PORT = 9223
STATUSES = {"idle", "browser_open", "waiting_for_sign_in", "session_ready", "attention_required"}
SIGN_IN_URL = "https://my.pitchbook.com/sign-in?returnUrl=%2Fdashboard"
DASHBOARD_URL = "https://my.pitchbook.com/dashboard"
FAKE_PID = 40521


# ---------------------------------------------------------------------------------------------------------------
# Fakes at the process and network boundary
# ---------------------------------------------------------------------------------------------------------------

def tid(label):
    """Chrome DevTools target ids are 32 upper case hex characters."""
    return hashlib.md5(label.encode()).hexdigest().upper()


class FakeChrome:
    """A DevTools endpoint: /json/list returns tabs, /json/activate/<id> focuses one."""

    def __init__(self):
        self.tabs = []
        self.reachable = True
        self.activate_ok = True
        self.urls = []
        self.events = []

    def tab(self, tab_id, url, title="", kind="page"):
        row = {"id": tid(tab_id), "type": kind, "url": url, "title": title,
               "webSocketDebuggerUrl": f"ws://127.0.0.1:{DEFAULT_PORT}/devtools/page/{tid(tab_id)}"}
        self.tabs.append(row)
        return row

    def urlopen(self, target, data=None, timeout=None, **kw):
        url = target if isinstance(target, str) else target.full_url
        self.urls.append(url)
        parts = urlsplit(url)
        if not self.reachable:
            raise URLError("connection refused")
        if parts.path == "/json/list" or parts.path == "/json":
            return _Reply(json.dumps(self.tabs))
        if parts.path.startswith("/json/activate/"):
            if not self.activate_ok:
                raise URLError("no such target")
            self.events.append(("activate", parts.path.rsplit("/", 1)[-1]))
            return _Reply("Target activated")
        return _Reply("{}")

    def activated_ids(self):
        return [urlsplit(u).path.rsplit("/", 1)[-1] for u in self.urls if "/json/activate/" in urlsplit(u).path]


class _Reply:
    def __init__(self, text):
        self._raw = text.encode()
        self.status = 200

    def read(self, *a):
        return self._raw

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeProc:
    def __init__(self, argv, returncode=0, stdout="", stderr="", pid=77001):
        self.args = argv
        self.returncode = returncode
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.pid = pid
        self.stdout = stdout
        self.stderr = stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, *a, **k):
        return self.stdout_text, self.stderr_text

    def kill(self):
        pass

    def terminate(self):
        pass


class _StillRunning(_FakeProc):
    def __init__(self, argv):
        super().__init__(argv, returncode=None)

    def poll(self):
        return None


class Procs:
    """Records every child process the app tries to start and answers lsof and osascript."""

    def __init__(self):
        self.calls = []
        self.kwargs = []
        self.events = []
        self.running_refresher = False       # a refresher that has not exited yet
        self.listener_pids = [FAKE_PID]      # what lsof says is listening on the CDP port
        self.osascript_rc = 0                 # exit status of the raise
        self.refresher_rc = 0

    def _argv(self, args):
        if isinstance(args, (list, tuple)):
            return [str(a) for a in args]
        return [str(args)]

    def answer(self, args, **kw):
        argv = self._argv(args)
        self.calls.append(argv)
        self.kwargs.append(kw)
        self.events.append(("proc", argv[0].rsplit("/", 1)[-1]))
        head = " ".join(argv)
        if kw.get("shell"):
            raise AssertionError(f"shell=True is not allowed: {head}")
        exe = argv[0].rsplit("/", 1)[-1]
        if exe == "lsof":
            if "-F" in argv or any(a.startswith("-F") for a in argv):
                text = "".join(f"p{p}\nn127.0.0.1:{DEFAULT_PORT}\n" for p in self.listener_pids)
            elif "-t" in argv or any(re.fullmatch(r"-[a-zA-Z]*t[a-zA-Z]*", a) for a in argv):
                text = "".join(f"{p}\n" for p in self.listener_pids)
            else:
                text = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n" + "".join(
                    f"Google {p} akrit 24u IPv4 0x1 0t0 TCP 127.0.0.1:{DEFAULT_PORT} (LISTEN)\n" for p in self.listener_pids)
            return _FakeProc(argv, 0 if self.listener_pids else 1, text, "")
        if exe == "osascript":
            return _FakeProc(argv, self.osascript_rc, "", "" if self.osascript_rc == 0 else "execution error")
        if "pb_cookie_refresher" in head:
            if self.running_refresher:
                return _StillRunning(argv)
            return _FakeProc(argv, self.refresher_rc, json.dumps({"ok": self.refresher_rc == 0, "saved": self.refresher_rc == 0}), "")
        return _FakeProc(argv, 0, "", "")

    def where(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]

    def osascripts(self):
        return [c for c in self.calls if c[0].rsplit("/", 1)[-1] == "osascript"]

    def refreshers(self):
        return self.where("pb_cookie_refresher")

    def launchers(self):
        # the launch script for the dedicated Chrome (anything that is not lsof, osascript or the refresher)
        return [c for c in self.calls
                if c[0].rsplit("/", 1)[-1] not in ("lsof", "osascript") and "pb_cookie_refresher" not in " ".join(c)]


class Clock:
    """Shifts every clock the app might read, so a test can say 'thirty seconds later'."""

    def __init__(self):
        self.offset = 0.0

    def advance(self, seconds):
        self.offset += seconds


@pytest.fixture
def chrome(monkeypatch):
    fake = FakeChrome()
    import vcoutreach.web.server.app as appmod

    monkeypatch.setattr(appmod, "urlopen", fake.urlopen)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


@pytest.fixture
def procs(monkeypatch):
    rec = Procs()
    monkeypatch.setattr(subprocess, "Popen", lambda args, *a, **k: rec.answer(args, **k))

    def run(args, *a, **k):
        proc = rec.answer(args, **k)
        if k.get("check") and proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, args, output=proc.stdout_text)
        if not (k.get("text") or k.get("universal_newlines") or k.get("encoding")):
            proc.stdout, proc.stderr = proc.stdout_text.encode(), proc.stderr_text.encode()
        return proc

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "call", lambda args, *a, **k: rec.answer(args, **k).returncode)
    monkeypatch.setattr(subprocess, "check_call", lambda args, *a, **k: rec.answer(args, **k).returncode)

    def check_output(args, *a, **k):
        proc = rec.answer(args, **k)
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, args, output=proc.stdout_text)
        return proc.stdout_text if k.get("text") or k.get("universal_newlines") else proc.stdout_text.encode()

    monkeypatch.setattr(subprocess, "check_output", check_output)
    import os

    monkeypatch.setattr(os, "system", lambda cmd: pytest.fail(f"os.system must never be used: {cmd}"))
    return rec


@pytest.fixture
def order(chrome, procs):
    """One ordered log of CDP activations and child process starts."""
    log = []
    chrome.events = log
    procs.events = log
    return log


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    real_time, real_mono = time.time, time.monotonic
    monkeypatch.setattr(time, "time", lambda: real_time() + c.offset)
    monkeypatch.setattr(time, "monotonic", lambda: real_mono() + c.offset)
    import datetime as dtmod
    import vcoutreach.web.server.app as appmod
    from datetime import timedelta

    class ShiftedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=c.offset)

        @classmethod
        def utcnow(cls):
            return real_datetime.utcnow() + timedelta(seconds=c.offset)

    monkeypatch.setattr(appmod, "datetime", ShiftedDatetime)
    return c


@pytest.fixture
def pb(make_web, chrome, procs, clock, monkeypatch):
    monkeypatch.delenv("PITCHBOOK_CDP_PORT", raising=False)
    return make_web()


def click(w):
    return w.post("/demo/pitchbook-visible", {})


def status(w):
    return w.get("/demo/pitchbook-status")


def state_of(response):
    data = response.get_json()
    assert isinstance(data, dict), response.get_data(as_text=True)[:200]
    for key in ("state", "status", "pitchbook_state"):
        if isinstance(data.get(key), str):
            return data[key]
    pytest.fail(f"no status value among the JSON keys: {sorted(data)}")


def message_of(response):
    data = response.get_json()
    for key in ("message", "msg", "detail"):
        if isinstance(data.get(key), str):
            return data[key]
    pytest.fail(f"no message among the JSON keys: {sorted(data)}")


def open_signed_in_chrome(chrome):
    chrome.tab("DEVTOOLS1", "devtools://devtools/bundled/inspector.html", "DevTools")
    chrome.tab("BLANK1", "about:blank", "")
    chrome.tab("PB-DASH", DASHBOARD_URL, "PitchBook Dashboard")
    chrome.tab("PB-SECOND", "https://my.pitchbook.com/profile/123/company", "Acme")


def open_sign_in_chrome(chrome):
    chrome.tab("BLANK1", "about:blank", "")
    chrome.tab("PB-LOGIN", SIGN_IN_URL, "PitchBook Sign In")


# ---------------------------------------------------------------------------------------------------------------
# Raising an already running Chrome
# ---------------------------------------------------------------------------------------------------------------

def test_with_tabs_open_the_first_pitchbook_tab_is_activated_then_the_listening_pid_is_raised(pb, chrome, procs, order):
    open_signed_in_chrome(chrome)
    response = click(pb)
    assert response.status_code < 400, response.get_data(as_text=True)[:200]
    assert chrome.activated_ids() == [tid("PB-DASH")]          # not devtools, not about:blank, not the second PitchBook tab
    scripts = procs.osascripts()
    assert len(scripts) == 1 and str(FAKE_PID) in scripts[0]
    kinds = [e[0] for e in order if e[0] == "activate" or e == ("proc", "osascript")]
    assert kinds == ["activate", "proc"]                   # tab first, then the process
    assert procs.launchers() == [], "a running Chrome must be raised, not launched again"


def test_the_message_says_in_front_when_the_raise_worked(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    response = click(pb)
    assert "in front" in message_of(response).lower()
    assert state_of(response) in STATUSES


def test_a_failed_osascript_never_claims_the_window_is_in_front(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    procs.osascript_rc = 1
    response = click(pb)
    message = message_of(response).lower()
    assert "in front" not in message
    assert "dock" in message and "pitchbook" in message
    assert state_of(response) in STATUSES


def test_no_listener_found_by_lsof_means_no_osascript_and_no_in_front_claim(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    procs.listener_pids = []
    response = click(pb)
    assert procs.osascripts() == []
    message = message_of(response).lower()
    assert "in front" not in message and "dock" in message


@pytest.mark.parametrize("junk", ["not-a-pid", "123; do shell script \"touch /tmp/pwn\""])
def test_a_listener_that_is_not_a_positive_integer_is_never_handed_to_osascript(pb, chrome, procs, junk):
    open_signed_in_chrome(chrome)
    procs.listener_pids = [junk]
    response = click(pb)
    for argv in procs.osascripts():
        assert junk not in " ".join(argv)
    assert "in front" not in message_of(response).lower()


def test_activation_failure_does_not_crash_the_route(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    chrome.activate_ok = False
    response = click(pb)
    assert response.status_code < 500
    assert state_of(response) in STATUSES


def test_page_titles_and_urls_from_chrome_never_reach_any_process_argument(pb, chrome, procs):
    chrome.tab("DEVTOOLS1", "devtools://devtools/bundled/inspector.html", "DevTools")
    chrome.tab("PB-EVIL", DASHBOARD_URL + "?q=$(id)", 'Evil"; do shell script "touch /tmp/pwn" -- `id`')
    click(pb)
    status(pb)
    everything = "\n".join(" ".join(argv) for argv in procs.calls)
    assert "do shell script" not in everything.replace('tell application "System Events"', "")
    assert "touch /tmp/pwn" not in everything and "$(id)" not in everything and "`id`" not in everything
    for argv in procs.osascripts():
        digits = [a for a in argv if a.isdigit()]
        assert digits == [str(FAKE_PID)]


# ---------------------------------------------------------------------------------------------------------------
# No tabs: launch through the launch script
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("reachable", [True, False], ids=["empty-tab-list", "chrome-not-running"])
def test_without_tabs_the_launch_script_runs_and_nothing_is_raised(pb, chrome, procs, reachable):
    chrome.reachable = reachable
    response = click(pb)
    assert response.status_code < 500
    assert len(procs.launchers()) == 1
    assert procs.osascripts() == []
    assert chrome.activated_ids() == []
    assert state_of(response) in STATUSES
    for kw in procs.kwargs:
        assert not kw.get("shell")


# ---------------------------------------------------------------------------------------------------------------
# CDP port validation and loopback only
# ---------------------------------------------------------------------------------------------------------------

def _hosts_and_ports(urls):
    return [(urlsplit(u).hostname, urlsplit(u).port) for u in urls]


def test_the_default_cdp_port_is_9223_on_loopback(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    click(pb)
    assert chrome.urls
    assert set(_hosts_and_ports(chrome.urls)) == {("127.0.0.1", DEFAULT_PORT)}
    assert any(f"-iTCP:{DEFAULT_PORT}" in " ".join(c) or f":{DEFAULT_PORT}" in " ".join(c) for c in procs.where("lsof"))


@pytest.mark.parametrize("port", ["9333"])
def test_a_valid_port_from_the_environment_is_used_everywhere(pb, chrome, procs, monkeypatch, port):
    monkeypatch.setenv("PITCHBOOK_CDP_PORT", port)
    open_signed_in_chrome(chrome)
    click(pb)
    status(pb)
    assert set(_hosts_and_ports(chrome.urls)) == {("127.0.0.1", int(port))}
    for argv in procs.where("lsof"):
        assert port in " ".join(argv) and str(DEFAULT_PORT) not in " ".join(argv)
    for argv in procs.refreshers():
        assert f"http://127.0.0.1:{port}" in argv


@pytest.mark.parametrize("bad", ["80", "abc", "9223 && touch /tmp/pwn"],
                         ids=lambda v: repr(v)[:24])
def test_an_invalid_port_is_never_used_and_never_reaches_a_process(pb, chrome, procs, monkeypatch, bad):
    monkeypatch.setenv("PITCHBOOK_CDP_PORT", bad)
    open_signed_in_chrome(chrome)
    r1 = click(pb)
    r2 = status(pb)
    assert r1.status_code < 500 and r2.status_code < 500
    for host, port in _hosts_and_ports(chrome.urls):
        assert host == "127.0.0.1" and 1024 <= port <= 65535
    for argv in procs.calls:
        joined = " ".join(argv)
        if bad and not bad.isdigit():
            assert bad not in joined
        for token in re.findall(r"-iTCP:(\S+?)(?:\s|$)", joined):
            assert token.isdigit() and 1024 <= int(token) <= 65535


# ---------------------------------------------------------------------------------------------------------------
# Status values
# ---------------------------------------------------------------------------------------------------------------

def test_status_is_idle_when_chrome_is_not_running_and_nothing_was_started(pb, chrome, procs):
    chrome.reachable = False
    response = status(pb)
    assert response.status_code == 200 and state_of(response) == "idle"
    assert procs.refreshers() == [] and procs.launchers() == []


def test_status_is_browser_open_when_chrome_has_no_pitchbook_tab(pb, chrome, procs):
    chrome.tab("BLANK1", "about:blank")
    chrome.tab("OTHER", "https://example.com/", "Example")
    assert state_of(status(pb)) in ("browser_open", "waiting_for_sign_in")
    assert procs.refreshers() == []


@pytest.mark.parametrize("url", [
    "https://my.pitchbook.com/sign-in?returnUrl=%2Fdashboard",
], ids=["sign-in"])
def test_a_tab_on_a_sign_in_page_is_waiting_for_sign_in_and_the_refresher_does_not_start(pb, chrome, procs, url):
    chrome.tab("PB-LOGIN", url, "PitchBook")
    for _ in range(3):
        assert state_of(status(pb)) == "waiting_for_sign_in"
    assert procs.refreshers() == []


def test_a_tab_past_sign_in_starts_the_refresher_and_the_session_becomes_ready(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    first = status(pb)
    assert len(procs.refreshers()) == 1
    assert state_of(first) in STATUSES
    assert state_of(status(pb)) == "session_ready"


def test_the_refresher_is_started_at_most_once_per_twenty_seconds(pb, chrome, procs, clock):
    open_signed_in_chrome(chrome)
    procs.running_refresher = True           # the first run has not finished
    for _ in range(6):
        status(pb)
    assert len(procs.refreshers()) == 1
    clock.advance(5)
    status(pb)
    assert len(procs.refreshers()) == 1
    clock.advance(25)
    status(pb)
    assert len(procs.refreshers()) <= 2


def test_a_failed_refresher_never_reports_a_ready_session_and_is_not_retried_in_a_burst(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    procs.refresher_rc = 1
    seen = {state_of(status(pb)) for _ in range(4)}
    assert "session_ready" not in seen
    assert seen <= STATUSES
    assert len(procs.refreshers()) == 1


def test_a_recently_saved_session_stays_ready_for_ten_minutes_then_expires(pb, chrome, procs, clock):
    open_signed_in_chrome(chrome)
    status(pb)
    assert state_of(status(pb)) == "session_ready"
    chrome.tabs.clear()
    chrome.tab("BLANK1", "about:blank")      # the user navigated the PitchBook tab away
    clock.advance(9 * 60)
    assert state_of(status(pb)) == "session_ready"
    clock.advance(3 * 60)
    assert state_of(status(pb)) != "session_ready"


def test_the_status_route_is_a_get_and_the_visible_route_needs_the_csrf_token(pb, chrome, procs):
    open_signed_in_chrome(chrome)
    assert pb.post("/demo/pitchbook-visible", {}, csrf=False).status_code in (400, 403)
    assert procs.calls == [] and chrome.activated_ids() == []
    assert pb.post("/demo/pitchbook-status", {}).status_code == 405


# ---------------------------------------------------------------------------------------------------------------
# Settings card and front end polling (string checks: the repo has no JS test harness)
# ---------------------------------------------------------------------------------------------------------------

def test_settings_has_a_pitchbook_card_with_the_open_sign_in_button(pb):
    html = pb.html("/settings")
    assert "data-pitchbook-launch" in html and "Open sign-in" in html
