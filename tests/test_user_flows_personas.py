"""Persona tests: drive the running Publikreach app like a real person would.

Black box only. Each persona gets a real dev server (`python -m vcoutreach ui`)
on a loopback port with a throwaway workspace, always in Test Mode, with Gmail
never connected, so nothing here can send an email. The tests read only what a
user sees (rendered pages, flash messages, links, forms, status codes) and use
only the public contract: README.md and the user-flow brief.

Personas
  1. First-run founder       fresh workspace, walks the setup path
  2. Returning user          closes and reopens the app with data on disk
  3. Back-stepper            goes back a step, edits, returns, follows links
  4. Bad-deck uploader       uploads files that are not valid decks, and bad CSVs
  5. Light-mode user         prefers the light theme (HTTP checks plus a real
                             browser check when Playwright and Chrome exist)

Overflow and layout checks are deliberately not part of this file.

Mutation check (what each test would catch if the app were broken that way).
Each test names the mutation it kills in a "Catches:" line.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.local_http  # loopback only, to the app under test

PYTHON = sys.executable
STAGING = os.environ.get(
    "VCO_STAGING_SRC", "/Users/akrit/opensource-app-staging/vcoutreach"
)
SITE = os.environ.get(
    "VCO_SITE_PACKAGES", "/Users/akrit/twitter-x-automation/venv/Lib/site-packages"
)
BROWSER_PYTHON = "/Users/akrit/VC emails/.venv/bin/python"

MAIN_PAGES = [
    "/crm",
    "/crm/drafts",
    "/partners",
    "/replies",
    "/crm/analytics",
    "/settings",
    "/setup/ai",
    "/scheduled",
    "/crm/send-plan",
    "/enrichment",
]
NAV_LABELS = ["Contacts", "Campaign", "Drafts", "Send", "Replies", "Analytics", "Settings"]
# The app legitimately shows the user's own workspace folder, so only developer-side
# leaks count: a Python traceback or a path into the interpreter's packages.
PATH_LEAK = re.compile(r"site-packages|Traceback \(most recent|File \"[^\"]+\.py\", line")


# ---------------------------------------------------------------------------
# Plumbing: server process, session, page parsing
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class App:
    """One dev server on one workspace. Can be stopped and started again."""

    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "ws"
        self.home = root / "home"
        self.logs = root / "logs"
        for d in (self.workspace, self.home, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        self.proc = None
        self.port = 0
        self.link = ""
        self.token = ""
        self._runs = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "App":
        self._runs += 1
        self.port = _free_port()
        env = dict(os.environ)
        env.update(
            HOME=str(self.home),
            SEND_MODE="test",
            OUTREACH_HOME=str(self.workspace),
            PYTHONPATH=f"{SITE}:{STAGING}",
        )
        for k in list(env):
            if k.endswith("_API_KEY") or k in ("GMAIL_APP_PASSWORD", "OUTREACH_SCHEDULER"):
                env.pop(k)
        log_path = self.logs / f"server-{self._runs}.log"
        log = open(log_path, "w")
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "vcoutreach", "ui", "--workspace", str(self.workspace),
             "--port", str(self.port), "--no-browser"],
            cwd=str(self.workspace), env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("app exited at startup:\n" + log_path.read_text()[-800:])
            m = re.search(r"http://127\.0\.0\.1:\d+/\?token=(\S+)", log_path.read_text())
            if m:
                self.link = m.group(0)
                self.token = m.group(1)
                return self
            time.sleep(0.15)
        raise RuntimeError("app did not print its one-time link in 60 seconds")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None

    def destroy(self) -> None:
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)


class Page:
    """What a person sees in one HTML response."""

    def __init__(self, resp: requests.Response):
        self.resp = resp
        self.status = resp.status_code
        self.html = resp.text
        self.final_path = resp.url.split("/", 3)[-1] if resp.url else ""
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", self.html, flags=re.S | re.I)
        self.text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", body))).strip()
        m = re.search(r"<title>(.*?)</title>", self.html, re.S | re.I)
        self.title = re.sub(r"\s+", " ", html_lib.unescape(m.group(1))).strip() if m else ""
        self.flashes = [
            (kind, re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", inner))).replace("×", "").strip())
            for kind, inner in re.findall(r'class="flash flash-(\w+)">(.*?)</li>', self.html, re.S)
        ]
        self.ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        self.links = _links(self.html)
        self.forms = [
            (action, re.findall(r'<(?:input|select|textarea)[^>]*\bname="([^"]+)"', inner))
            for action, inner in re.findall(r'<form[^>]*\baction="([^"]+)"[^>]*>(.*?)</form>', self.html, re.S)
        ]

    def hrefs(self) -> list[str]:
        return [h for h, _ in self.links]

    def link_text_for(self, href: str) -> list[str]:
        return [t for h, t in self.links if h == href]

    def flash_kinds(self) -> list[str]:
        return [k for k, _ in self.flashes]

    def flash_text(self) -> str:
        return " | ".join(t for _, t in self.flashes)

    def banner_is_test(self) -> bool:
        return "TEST MODE" in self.text and "LIVE MODE" not in self.text


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[tuple[str, str]] = []
        self._href = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.out.append((self._href, re.sub(r"\s+", " ", "".join(self._buf)).strip()))
            self._href = None


def _links(markup: str) -> list[tuple[str, str]]:
    p = _LinkParser()
    p.feed(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I))
    return [(html_lib.unescape(h), t) for h, t in p.out]


class Client:
    """A logged-in browser session (the one-time link has been used)."""

    def __init__(self, app: App):
        self.app = app
        self.s = requests.Session()
        r = self.s.get(app.link, allow_redirects=True, timeout=30)
        assert r.status_code == 200, f"login link failed: {r.status_code}"
        self.landing = Page(r)
        self._csrf = None

    @property
    def base(self) -> str:
        return self.app.base

    def get(self, path: str, **kw) -> Page:
        return Page(self.s.get(self.base + path, timeout=60, **kw))

    def raw_get(self, path: str, **kw) -> requests.Response:
        return self.s.get(self.base + path, timeout=60, **kw)

    def csrf(self) -> str:
        if not self._csrf:
            html = self.s.get(self.base + "/crm", timeout=30).text
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
            assert m, "no csrf_token field on /crm"
            self._csrf = m.group(1)
        return self._csrf

    def post_raw(self, path: str, data=None, files=None, csrf=True, **kw) -> requests.Response:
        d = dict(data or {})
        if csrf:
            d["csrf_token"] = self.csrf()
        return self.s.post(self.base + path, data=d, files=files, timeout=120, **kw)

    def post(self, path: str, data=None, files=None, csrf=True, **kw) -> Page:
        return Page(self.post_raw(path, data=data, files=files, csrf=csrf, **kw))


def _boot(tmp_factory, name: str) -> tuple[App, Client]:
    root = Path(tempfile.mkdtemp(prefix=f"pk-{name}-"))
    app = App(root)
    try:
        app.start()
        return app, Client(app)
    except Exception:
        app.destroy()
        raise


@pytest.fixture(scope="module")
def founder():
    app, c = _boot(None, "founder")
    yield c
    app.destroy()


@pytest.fixture(scope="module")
def onboarded():
    """A founder who finished the parts of setup possible without Gmail."""
    app, c = _boot(None, "onboarded")
    r = c.post("/settings/keys", {"TEST_RECIPIENT_EMAIL": "me@example.org"})
    assert "Settings saved" in r.flash_text()
    r = c.post("/settings/sender", {
        "name": "Ada Founder", "sender_email": "ada@example.org",
        "signature": "Ada\nFounder, Acme", "next": "/crm/drafts",
    })
    assert "Sender profile saved" in r.flash_text()
    csv = (
        "name,email,org,role,facts\n"
        "Tess Tester,tess@example.com,Tess Capital,Partner,your 2024 essay on seed markets | the podcast about pricing\n"
        "Rob Reader,rob@example.net,Reader Ventures,Principal,your talk on marketplaces\n"
    )
    r = c.post("/crm/import", {"csv_text": csv, "next": "/crm/drafts"})
    assert "Imported 2 new contacts" in r.flash_text()
    r = c.post("/crm/drafts/write", {"provider": "template"})
    assert "Wrote 2 drafts" in r.flash_text()
    c.csv = csv
    yield c
    app.destroy()


@pytest.fixture(scope="module")
def sampled():
    """A user who loaded the fictional sample, in Test Mode."""
    app, c = _boot(None, "sampled")
    r = c.post("/sample/load")
    assert "Loaded 5 fictional sample contacts" in r.flash_text()
    yield c
    app.destroy()


# ---------------------------------------------------------------------------
# Persona 1: first-run founder
# ---------------------------------------------------------------------------
class TestFirstRunFounder:
    def test_stranger_without_the_link_is_locked_out(self, founder):
        # Catches: auth/cookie check removed, so anyone on the machine can read or post.
        for path in ("/crm", "/settings", "/crm/drafts"):
            assert requests.get(founder.base + path, timeout=10).status_code == 403
        assert requests.post(founder.base + "/sample/load", timeout=10).status_code == 403
        assert requests.get(founder.base + "/?token=wrong", timeout=10).status_code == 403

    def test_health_check_is_open(self, founder):
        # Catches: health endpoint gated or broken (the launcher cannot tell the app is up).
        r = requests.get(founder.base + "/healthz", timeout=10)
        assert r.status_code == 200 and r.json() == {"ok": True}

    def test_login_link_works_once_only(self, founder):
        # Catches: token left reusable after exchange.
        assert requests.get(founder.app.link, timeout=10, allow_redirects=False).status_code == 403

    def test_first_screen_is_contacts_in_test_mode(self, founder):
        # Catches: wrong landing page, Test Mode banner missing, or app defaulting to LIVE.
        p = founder.landing
        assert p.final_path.startswith("crm")
        assert p.title.startswith("Contacts")
        assert "TEST MODE" in p.text
        assert "Sends go to your test recipient only" in p.text
        assert "LIVE MODE" not in p.text

    def test_setup_banner_starts_at_step_one_and_points_at_gmail(self, founder):
        # Catches: wrong first step, broken Go link, test-recipient shortcut missing.
        p = founder.get("/crm")
        assert re.search(r"Step 1 of 7", p.text)
        assert "/settings#svc-gmail" in p.hrefs()
        assert "/settings#svc-test-recipient" in p.hrefs()

    def test_go_live_button_not_offered_before_setup_is_done(self, founder):
        # Catches: the Switch to LIVE control shown to a user who has not finished setup.
        for path in ("/crm", "/crm/drafts", "/partners", "/settings"):
            assert "Switch to LIVE mode" not in founder.get(path).text

    @pytest.mark.parametrize("path", MAIN_PAGES)
    def test_every_main_page_renders_in_test_mode(self, founder, path):
        # Catches: a page that 500s on an empty workspace, loses the nav or the banner,
        # or leaks a traceback or a file path to the user.
        p = founder.get(path)
        assert p.status == 200
        assert "Publikreach" in p.title
        assert p.banner_is_test()
        assert not PATH_LEAK.search(p.html)
        for label in NAV_LABELS:
            assert label in p.text
        assert "/crm" in p.hrefs() and "/crm/drafts" in p.hrefs() and "/settings" in p.hrefs()

    def test_empty_contacts_page_tells_a_new_user_what_to_do(self, founder):
        # Catches: blank empty state, import or sample forms missing.
        p = founder.get("/crm")
        actions = [a for a, _ in p.forms]
        assert "/crm/import" in actions
        assert "/sample/load" in actions
        assert re.search(r"import contacts", p.text, re.I)
        assert re.search(r"sample", p.text, re.I)

    def test_campaign_page_asks_what_the_campaign_is_for(self, founder):
        # Catches: chooser removed so the page has no wording for the user's goal.
        p = founder.get("/crm/drafts")
        assert re.search(r"what is this campaign for", p.text, re.I)
        assert re.search(r"raising money", p.text, re.I)
        assert re.search(r"looking for a role", p.text, re.I)

    def test_campaign_has_five_numbered_steps_to_jump_between(self, founder):
        # Catches: stepper anchors or step sections renamed or removed.
        p = founder.get("/crm/drafts")
        for n in range(1, 6):
            assert f'data-step="{n}"' in p.html
            assert f'data-step-jump="{n}"' in p.html
            assert f"#step-{n}" in p.hrefs()

    @pytest.mark.parametrize("typed", ["", "go live", "GO LIVE!", "GOLIVE", "yes", "LIVE"])
    def test_wrong_confirmation_phrase_never_goes_live(self, founder, typed):
        # Catches: confirm phrase compared loosely (case-insensitive, prefix, or empty).
        r = founder.post("/toggle-send-mode", {"confirm_text": typed})
        assert "GO LIVE" in r.flash_text()
        assert "error" in r.flash_kinds() or "warning" in r.flash_kinds() or "success" not in r.flash_kinds()
        assert founder.get("/crm").banner_is_test()

    def test_send_and_schedule_with_nothing_selected_are_refused(self, founder):
        # Catches: bulk send or schedule doing anything (or claiming success) with no drafts.
        r = founder.post("/send-batch", {"confirm_text": "SEND BATCH OF 1"})
        assert "success" not in r.flash_kinds()
        assert re.search(r"no drafts selected", r.flash_text(), re.I)
        r = founder.post("/schedule-send", {
            "sched_date": "2030-01-01", "sched_time": "10:00", "sched_mode": "live",
            "confirm_text": "SCHEDULE LIVE FOR 1",
        })
        assert "success" not in r.flash_kinds()
        assert founder.get("/crm").banner_is_test()

    def test_form_post_without_the_page_token_is_rejected_and_changes_nothing(self, founder):
        # Catches: CSRF check removed from a state-changing form.
        r = founder.post_raw("/crm/import", {"csv_text": "name,email\nSneaky Person,sneaky@example.com\n"}, csrf=False)
        assert r.status_code == 400
        assert "Sneaky" not in founder.get("/crm").text

    def test_unknown_address_shows_a_friendly_page_with_a_way_home(self, founder):
        # Catches: raw 404/500 error pages, leaked paths, or a dead end with no link home.
        for path in ("/nope", "/crm/drafts/zzz-missing"):
            p = founder.get(path)
            assert p.status == 404
            assert not PATH_LEAK.search(p.html)
            assert "/crm" in p.hrefs()

    def test_bad_test_recipient_is_explained_in_one_line(self, founder):
        # Catches: invalid recipient accepted (test sends would go to garbage), or a stack trace.
        r = founder.post("/settings/keys", {"TEST_RECIPIENT_EMAIL": "not an email"})
        assert r.status == 200
        assert "error" in r.flash_kinds()
        assert re.search(r"valid email", r.flash_text(), re.I)
        assert not PATH_LEAK.search(r.html)

    @pytest.mark.parametrize(
        "csv_text",
        ["", "just some words", "name,email\nNoEmail,\n", "name,email\nA Person,notanemail\n"],
    )
    def test_bad_csv_gets_a_readable_error_and_adds_nobody(self, founder, csv_text):
        # Catches: import crashing, or silently adding rows with bad emails.
        r = founder.post("/crm/import", {"csv_text": csv_text})
        assert r.status == 200
        assert "error" in r.flash_kinds()
        assert not PATH_LEAK.search(r.html)
        assert "A Person" not in founder.get("/crm").text

    def test_markup_in_a_contact_name_is_not_executed(self, founder):
        # Catches: output escaping removed (stored script injection in the contact list).
        founder.post("/crm/import", {"csv_text": "name,email\n<script>alert(1)</script>,xss@example.com\n"})
        page = founder.get("/crm")
        assert "<script>alert(1)" not in page.html
        assert "&lt;script&gt;" in page.html


# ---------------------------------------------------------------------------
# Persona 1b: the founder keeps going (everything possible without Gmail)
# ---------------------------------------------------------------------------
class TestFounderWorksThroughSetup:
    def test_test_recipient_shows_in_the_banner(self, onboarded):
        # Catches: the saved recipient not being used or shown (user cannot tell where tests go).
        assert "Test recipient: me@example.org" in onboarded.get("/crm").text

    def test_imported_contacts_appear_in_the_list(self, onboarded):
        # Catches: import reporting success but not saving, or list not rendering rows.
        p = onboarded.get("/crm")
        for needle in ("Tess Tester", "tess@example.com", "Rob Reader", "rob@example.net", "Tess Capital"):
            assert needle in p.text
        assert re.search(r"2 of 2", p.text)

    def test_importing_the_same_csv_again_skips_duplicates(self, onboarded):
        # Catches: duplicate contacts created on re-import (double emails later).
        r = onboarded.post("/crm/import", {"csv_text": onboarded.csv})
        assert re.search(r"Imported 0 new contacts", r.flash_text())
        assert re.search(r"skipped 2", r.flash_text())
        assert len(re.findall(r"tess@example\.com", onboarded.get("/crm").text)) == 1

    def test_drafts_are_written_for_each_contact_and_listed(self, onboarded):
        # Catches: draft writer skipping contacts, or the table not linking each draft.
        p = onboarded.get("/crm/drafts")
        assert "/crm/drafts/tess-example-com" in p.hrefs()
        assert "/crm/drafts/rob-example-net" in p.hrefs()
        assert re.search(r"2 drafts written", p.text)

    def test_campaign_stepper_reflects_progress(self, onboarded):
        # Catches: steps not advancing as the user completes them, or later steps unlocking early.
        p = onboarded.get("/crm/drafts")
        state = {int(n): s for n, s in re.findall(r"Step (\d) : (done|current|to do)", p.text)}
        assert state.get(1) == "done"
        assert state.get(3) == "done"
        assert state.get(4) != "done"
        assert state.get(5) != "done"

    def test_test_email_is_not_offered_while_gmail_is_not_connected(self, onboarded):
        # Catches: readiness gate bypassed (a send attempt with no Gmail), or readiness hidden.
        p = onboarded.get("/crm/drafts")
        assert re.search(r"Not ready: gmail", p.text, re.I)
        assert not re.search(r"Send a test to", p.text)
        assert p.banner_is_test()

    def test_setup_banner_does_not_claim_gmail_is_done(self, onboarded):
        # Catches: wizard counting a step done that is not (Gmail cannot be connected here).
        assert re.search(r"Step 1 of 7", onboarded.get("/crm").text)

    def test_draft_editor_opens_and_leads_back_to_the_campaign_step(self, onboarded):
        # Catches: editor page dead end (no way back to where the user came from).
        p = onboarded.get("/crm/drafts/tess-example-com")
        assert p.status == 200
        back = [h for h, t in p.links if t.lower().startswith("back") and h.startswith("/crm/drafts")]
        assert back, p.links
        target = back[0].split("#", 1)[1] if "#" in back[0] else ""
        n = re.fullmatch(r"step-([1-5])", target)
        assert n, back[0]
        assert f'data-step="{n.group(1)}"' in onboarded.get("/crm/drafts").html

    def test_forms_redirect_to_a_page_so_reload_or_back_does_not_resubmit(self, onboarded):
        # Catches: a form answering the POST directly (browser back/reload re-posts the form).
        for path, data in (
            ("/settings/sender", {"name": "Ada Founder", "sender_email": "ada@example.org",
                                  "signature": "Ada", "next": "/crm/drafts"}),
            ("/crm/import", {"csv_text": onboarded.csv}),
            ("/settings/keys", {"TEST_RECIPIENT_EMAIL": "me@example.org"}),
        ):
            r = onboarded.post_raw(path, data)
            assert r.history, f"{path} did not redirect"
            assert r.history[0].status_code in (302, 303)
            assert r.request.method == "GET"

    def test_sending_a_selected_draft_does_nothing_without_gmail(self, onboarded):
        # Catches: any path that records a send, or flips to LIVE, with Gmail disconnected.
        send = onboarded.get("/partners")
        ids = re.findall(r'name="draft_ids"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="draft_ids"', send.html)
        ids = [a or b for a, b in ids]
        r = onboarded.post("/send-batch", {"draft_ids": ids, "confirm_text": f"SEND BATCH OF {max(len(ids), 1)}"})
        assert ids, "the Send page offered no drafts to select"
        assert not [t for k, t in r.flashes if k == "success" and re.search(r"\bsent\b", t, re.I)]
        crm = onboarded.get("/crm").text
        assert re.search(r"Sent\s*0\b", crm)
        assert onboarded.get("/crm").banner_is_test()


# ---------------------------------------------------------------------------
# Persona 2: returning user
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def returning():
    root = Path(tempfile.mkdtemp(prefix="pk-returning-"))
    app = App(root)
    app.start()
    first = Client(app)
    first.post("/sample/load")
    first.post("/settings/keys", {"TEST_RECIPIENT_EMAIL": "me@example.org"})
    # The user switches to LIVE last session, then quits.
    r = first.post("/toggle-send-mode", {"confirm_text": "GO LIVE"})
    was_live = "LIVE MODE" in first.get("/crm").text
    old_token = app.token
    old_drafts = sorted(set(re.findall(r"/crm/drafts/([a-z-]+)", first.get("/crm/drafts").html)))
    app.stop()
    app.start()  # same workspace, a new launch
    second = Client(app)
    second.was_live = was_live
    second.old_token = old_token
    second.old_drafts = old_drafts
    second.second_link = app.link
    yield second
    app.destroy()


class TestReturningUser:
    def test_previous_session_really_was_live(self, returning):
        # Guard for the persona itself: without this the restart test proves nothing.
        assert returning.was_live is True

    def test_reopening_always_starts_in_test_mode(self, returning):
        # Catches: LIVE mode persisting across launches (a stray click could send for real).
        p = returning.get("/crm")
        assert p.banner_is_test()
        assert "Real partner delivery is on" not in p.text

    def test_reopening_every_page_shows_test_mode(self, returning):
        # Catches: only one page resetting, other pages still reporting LIVE.
        for path in MAIN_PAGES:
            assert returning.get(path).banner_is_test(), path

    def test_yesterdays_link_is_dead(self, returning):
        # Catches: old one-time token still accepted by the next launch.
        r = requests.get(f"{returning.base}/?token={returning.old_token}", timeout=10, allow_redirects=False)
        assert r.status_code == 403

    def test_contacts_and_drafts_survive_the_restart(self, returning):
        # Catches: workspace data lost or reset on launch.
        crm = returning.get("/crm")
        for name in ("Contact One", "Contact Two", "Contact Three", "Contact Four", "Contact Five"):
            assert name in crm.text
        drafts = sorted(set(re.findall(r"/crm/drafts/([a-z-]+)", returning.get("/crm/drafts").html)))
        assert drafts == returning.old_drafts and len(drafts) >= 5

    def test_test_recipient_survives_the_restart(self, returning):
        # Catches: settings not persisted.
        assert "Test recipient: me@example.org" in returning.get("/crm").text

    def test_loading_the_sample_again_does_not_wipe_work(self, returning):
        # Catches: sample load overwriting an existing workspace.
        r = returning.post("/sample/load")
        assert "error" in r.flash_kinds() or "success" not in r.flash_kinds()
        assert re.search(r"empty workspace", r.flash_text(), re.I)
        assert "Contact One" in returning.get("/crm").text

    def test_waiting_reply_is_counted_on_the_home_screen(self, returning):
        # Catches: reply counts not read from stored data.
        m = re.search(r"(\d+) repl\w+ to answer", returning.get("/crm").text)
        assert m and int(m.group(1)) >= 1

    def test_analytics_is_honest_that_nothing_was_sent(self, returning):
        # Catches: analytics inventing sends or opens for a workspace that never sent.
        p = returning.get("/crm/analytics")
        assert p.status == 200
        assert not re.search(r"\b[1-9]\d* sent\b", p.text, re.I)

    def test_switching_to_live_still_needs_the_typed_phrase(self, returning):
        # Catches: restart leaving the confirm step weakened.
        r = returning.post("/toggle-send-mode", {"confirm_text": "go live"})
        assert "GO LIVE" in r.flash_text()
        assert returning.get("/crm").banner_is_test()


# ---------------------------------------------------------------------------
# Persona 3: user who goes back a step
# ---------------------------------------------------------------------------
INTERNAL_SKIP = ("/static/", "/api/")


def _internal(href: str) -> bool:
    return href.startswith("/") and not href.startswith("//") and not href.startswith(INTERNAL_SKIP)


class TestBackStepper:
    @pytest.mark.parametrize("path", MAIN_PAGES)
    def test_nav_to_the_main_places_is_on_every_page_and_works(self, sampled, path):
        # Catches: a page missing the nav (dead end), or a nav target that no longer loads.
        p = sampled.get(path)
        nav_targets = {"/crm", "/crm/drafts", "/partners", "/replies", "/crm/analytics", "/settings"}
        assert nav_targets <= set(p.hrefs())
        for target in nav_targets:
            assert sampled.raw_get(target).status_code == 200

    def test_every_in_app_link_on_the_main_screens_goes_somewhere(self, sampled):
        # Catches: a link or button target that 404s or 500s (typos, renamed routes, missing ids).
        dead = []
        for path in ["/crm", "/crm/drafts", "/partners", "/replies", "/crm/analytics",
                     "/settings", "/crm/drafts/contact-one", "/setup/ai", "/scheduled",
                     "/crm/send-plan", "/enrichment"]:
            for href in sampled.get(path).hrefs():
                if not _internal(href):
                    continue
                code = sampled.raw_get(href.split("#")[0] or path).status_code
                if code >= 400:
                    dead.append((path, href, code))
        assert not dead, dead

    def test_campaign_steps_two_to_five_have_a_back_button(self, sampled):
        # Catches: a wizard step with no way back (user stuck going forward only).
        html = sampled.get("/crm/drafts").html
        buttons = re.findall(r"<button[^>]*data-step-back[^>]*>", html)
        visible = [b for b in buttons if not re.search(r"\bhidden\b", b)]
        assert len(visible) >= 4

    def test_campaign_steps_have_next_buttons_naming_the_next_step(self, sampled):
        # Catches: forward buttons lost or unlabeled.
        text = sampled.get("/crm/drafts").text
        assert len(re.findall(r"Next: \w+", text)) >= 3

    def test_draft_editor_has_a_back_link_to_an_existing_step(self, sampled):
        # Catches: back link pointing at an anchor that is not on the page it opens.
        p = sampled.get("/crm/drafts/contact-one")
        back = [(h, t) for h, t in p.links if t.lower().startswith("back")]
        assert back
        camp = sampled.get("/crm/drafts")
        for href, _ in back:
            assert href.startswith("/crm/drafts")
            anchor = href.split("#", 1)[1] if "#" in href else None
            if anchor is not None:
                n = re.fullmatch(r"step-([1-5])", anchor)
                assert n, href
                assert f'data-step="{n.group(1)}"' in camp.html

    def test_setup_with_ai_page_goes_back_to_settings(self, sampled):
        # Catches: help page with no return path.
        p = sampled.get("/setup/ai")
        assert "/settings" in [h for h, t in p.links if "back" in t.lower()]

    def test_going_back_to_settings_and_editing_the_sender_keeps_the_drafts(self, sampled):
        # Catches: editing an earlier step wiping or regenerating later steps' work.
        before = sorted(set(re.findall(r"/crm/drafts/([a-z-]+)", sampled.get("/crm/drafts").html)))
        assert len(before) >= 5
        r = sampled.post("/settings/sender", {
            "name": "Grace Hopper", "sender_email": "grace@example.org",
            "signature": "Grace", "next": "/crm/drafts",
        })
        assert "Sender profile saved" in r.flash_text()
        after = sorted(set(re.findall(r"/crm/drafts/([a-z-]+)", sampled.get("/crm/drafts").html)))
        assert after == before

    def test_going_back_to_step_one_and_reimporting_changes_nothing(self, sampled):
        # Catches: re-running an earlier step duplicating contacts.
        crm = sampled.get("/crm").text
        emails = sorted(set(re.findall(r"[\w.+-]+@example\.com", crm)))
        assert len(emails) >= 5
        csv = "name,email\n" + "".join(f"Again {i},{e}\n" for i, e in enumerate(emails))
        r = sampled.post("/crm/import", {"csv_text": csv})
        assert re.search(r"Imported 0 new", r.flash_text())
        assert sampled.get("/crm").text.count("one@example.com") == crm.count("one@example.com")

    def test_back_after_any_form_lands_on_a_normal_page(self, sampled):
        # Catches: a form leaving the user on a POST-only URL with a blank page.
        for path, data in (
            ("/settings/publik-recommendation", {"state": "pending"}),
            ("/settings/publik-recommendation", {"state": "skipped"}),
            ("/crm/deck/example", {}),
        ):
            r = sampled.post_raw(path, data)
            assert r.history and r.request.method == "GET"
            assert "<html" in r.text and r.status_code == 200

    def test_optional_publik_step_can_be_skipped_and_undone(self, sampled):
        # Catches: skip not recorded, or no undo (user locked into the choice).
        r = sampled.post("/settings/publik-recommendation", {"state": "skipped"})
        assert re.search(r"skipped", r.flash_text(), re.I)
        assert re.search(r"undo", r.flash_text(), re.I)
        r = sampled.post("/settings/publik-recommendation", {"state": "pending"})
        assert "error" not in r.flash_kinds()


# ---------------------------------------------------------------------------
# Persona 4: user with an invalid deck (and the other bad files)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def decker():
    app, c = _boot(None, "decker")
    c.app_root = app.root
    yield c
    app.destroy()


BAD_UPLOADS = [
    ("notes.txt", b"hello there", "text/plain"),
    ("empty.pdf", b"", "application/pdf"),
    ("fake.pdf", b"this is plain text pretending to be a pdf", "application/pdf"),
    ("program.exe", b"MZ\x00\x01binary", "application/octet-stream"),
    ("deck.pdf.exe", b"MZ\x00\x01binary", "application/pdf"),
    ("image.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png"),
]


def _deck_state(c: Client) -> tuple[int, str, int]:
    r = c.raw_get("/crm/deck")
    return r.status_code, r.headers.get("content-type", ""), len(r.content)


class TestInvalidDeckUploader:
    @pytest.mark.parametrize("name,content,ctype", BAD_UPLOADS, ids=[b[0] for b in BAD_UPLOADS])
    def test_not_a_pdf_is_rejected_with_a_plain_message(self, decker, name, content, ctype):
        # Catches: validation removed (anything attaches to every email), a crash, or a
        # success message for a bad file.
        before = _deck_state(decker)
        r = decker.post("/crm/deck/upload", files={"deck": (name, content, ctype)})
        assert r.status == 200 and r.final_path.startswith("crm/drafts")
        assert r.flash_kinds() and set(r.flash_kinds()) <= {"error", "warning"}
        assert re.search(r"pdf|empty", r.flash_text(), re.I)
        assert not PATH_LEAK.search(r.html)
        assert _deck_state(decker) == before

    def test_submitting_with_no_file_chosen_explains_what_to_do(self, decker):
        # Catches: missing-field crash (400/500) instead of guidance.
        r = decker.post("/crm/deck/upload")
        assert r.status == 200
        assert "error" in r.flash_kinds()
        assert re.search(r"choose|pdf", r.flash_text(), re.I)

    def test_page_still_lets_the_user_retry_after_a_failure(self, decker):
        # Catches: the upload form disappearing after an error.
        decker.post("/crm/deck/upload", files={"deck": ("x.txt", b"nope", "text/plain")})
        p = decker.get("/crm/drafts")
        assert "/crm/deck/upload" in [a for a, _ in p.forms]
        assert any("deck" in fields for a, fields in p.forms if a == "/crm/deck/upload")
        assert "/crm/deck/example" in [a for a, _ in p.forms]

    def test_failed_upload_leaves_the_app_in_test_mode(self, decker):
        # Catches: error path altering send mode or other settings.
        assert decker.get("/crm/drafts").banner_is_test()

    def test_oversize_upload_keeps_the_user_inside_the_app(self, decker):
        # Catches: a bare text page (no nav, no link) that strands a user in the desktop app,
        # or a 5xx. The page must either redirect with a message or be a real app page.
        big = b"%PDF-1.4\n" + b"0" * (30 * 1024 * 1024)
        r = decker.post_raw("/crm/deck/upload", files={"deck": ("big.pdf", big, "application/pdf")})
        assert r.status_code < 500
        page = Page(r)
        ctype = r.headers.get("content-type", "")
        assert "text/html" in ctype, f"oversize upload answered with {ctype!r}: {page.text[:80]!r}"
        assert "/crm" in page.hrefs() or "/crm/drafts" in page.hrefs()
        assert re.search(r"25 ?MB|too large|larger", page.text, re.I)
        assert decker.get("/crm").status == 200  # session still good

    def test_filename_with_path_pieces_cannot_write_outside_the_workspace(self, decker):
        # Catches: upload name used as a path (files appearing next to the workspace).
        root = decker.app_root
        before = sorted(p.name for p in root.iterdir())
        for name in ("../../evil.pdf", "..\\..\\evil.pdf", "/tmp/evil.pdf", "a b?.pdf"):
            r = decker.post_raw("/crm/deck/upload", files={"deck": (name, b"%PDF-1.4\nx", "application/pdf")})
            assert r.status_code < 500
        assert sorted(p.name for p in root.iterdir()) == before
        assert not Path("/tmp/evil.pdf").exists()

    def test_user_can_recover_with_the_example_deck(self, decker):
        # Catches: example deck broken, so a stuck user has no way forward.
        r = decker.post("/crm/deck/example")
        assert "success" in r.flash_kinds()
        status, ctype, size = _deck_state(decker)
        assert status == 200 and "pdf" in ctype and size > 100
        assert decker.get("/crm/drafts").banner_is_test()

    def test_bad_upload_after_a_good_deck_keeps_the_good_deck(self, decker):
        # Catches: a failed upload deleting the deck already attached.
        decker.post("/crm/deck/example")
        before = _deck_state(decker)
        decker.post("/crm/deck/upload", files={"deck": ("junk.txt", b"nope", "text/plain")})
        assert _deck_state(decker) == before


# ---------------------------------------------------------------------------
# Persona 5: light-mode user
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def light_app():
    root = Path(tempfile.mkdtemp(prefix="pk-light-"))
    app = App(root)
    app.start()
    c = Client(app)
    c.post("/sample/load")
    app.stop()
    app.start()  # fresh one-time link for a real browser
    yield app
    app.destroy()


@pytest.fixture(scope="module")
def light_client():
    app, c = _boot(None, "lightclient")
    c.post("/sample/load")
    yield c
    app.destroy()


class TestLightModeUser:
    @pytest.mark.parametrize("path", MAIN_PAGES + ["/crm/drafts/contact-one"])
    def test_every_page_carries_the_theme_switch(self, light_client, path):
        # Catches: the theme toggle or the theme attribute missing on a page.
        p = light_client.get(path)
        assert p.status == 200
        assert "themeToggle" in p.ids
        assert re.search(r"<html[^>]*\bdata-theme=", p.html)

    def test_server_does_not_force_a_dark_theme(self, light_client):
        # Catches: dark hard-coded in the served markup, which would override a light preference.
        for path in ("/crm", "/crm/drafts", "/settings"):
            m = re.search(r"<html[^>]*\bdata-theme=\"([^\"]*)\"", light_client.get(path).html)
            assert m and m.group(1) != "dark"

    def test_theme_script_remembers_the_choice_and_follows_the_system(self, light_client):
        # Catches: theme persistence key dropped or system preference ignored.
        p = light_client.get("/crm")
        srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', p.html)
        bundle = "\n".join(light_client.raw_get(s).text for s in srcs if s.startswith("/"))
        assert "publik-signal-theme" in bundle
        assert "prefers-color-scheme" in bundle
        assert "themeToggle" in bundle

    def test_stylesheets_define_a_light_and_a_dark_palette(self, light_client):
        # Catches: light palette removed or empty, leaving light-mode users with dark or broken colors.
        p = light_client.get("/crm")
        sheets = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"|<link[^>]*href="([^"]+)"[^>]*rel="stylesheet"', p.html)
        sheets = [a or b for a, b in sheets]
        assert sheets
        css = "\n".join(light_client.raw_get(s).text for s in sheets if s.startswith("/"))
        # Light is the base palette on :root; dark is layered on with [data-theme="dark"].
        assert re.search(r":root\s*\{[^}]*--[\w-]+\s*:\s*#", css)
        assert re.search(r"\[data-theme\s*=\s*[\"']?dark[\"']?\]\s*\{[^}]*--[\w-]+\s*:\s*#", css)

    def test_pages_load_nothing_from_other_sites(self, light_client):
        # Catches: external fonts, scripts, or trackers added (README promises none).
        for path in ("/crm", "/crm/drafts", "/settings", "/crm/analytics"):
            html = light_client.get(path).html
            assert not re.findall(r'<script[^>]*\bsrc="(?:https?:)?//', html)
            assert not re.findall(r'<link[^>]*\bhref="(?:https?:)?//[^"]*"', html)
            assert not re.findall(r'<(?:img|iframe)[^>]*\bsrc="(?:https?:)?//', html)
            assert not re.findall(r"url\((?:https?:)?//", html)
            assert "@import" not in html

    def test_real_browser_honours_light_and_dark_and_remembers(self, light_app):
        # Catches: stored 'light' not applied, toggle not flipping or not persisting,
        # light palette identical to dark (bright text on a bright page, or the reverse).
        if not Path(BROWSER_PYTHON).exists():
            pytest.skip("no Playwright venv on this machine")
        script = _BROWSER_SCRIPT
        proc = subprocess.run(
            [BROWSER_PYTHON, "-I", "-c", script, light_app.link, light_app.base],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 3:
            pytest.skip("Chrome cannot be launched for Playwright here")
        assert proc.returncode == 0, proc.stderr[-600:]
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["stored_light_applies"] == "light"
        assert out["light_bg_luma"] > 0.6, out
        assert out["light_contrast"] >= 4.5, out
        assert out["after_toggle"] == "dark"
        assert out["dark_bg_luma"] < 0.4, out
        assert out["dark_contrast"] >= 4.5, out
        assert out["persisted_after_reload"] == "dark"
        assert out["system_light_no_storage"] == "light"
        assert out["system_dark_no_storage"] == "dark"
        for page, theme in out["pages_light"].items():
            assert theme == "light", page
        assert out["console_errors"] == [], out["console_errors"]


@pytest.fixture(scope="module")
def step_app():
    root = Path(tempfile.mkdtemp(prefix="pk-steps-"))
    app = App(root)
    app.start()
    Client(app).post("/sample/load")
    app.stop()
    app.start()
    yield app
    app.destroy()


class TestBackLinkLandsOnTheRightStep:
    def test_back_to_step_link_opens_that_step_in_a_real_browser(self, step_app):
        # Catches: a "Back to step N" link whose anchor the page no longer honours, so the
        # user lands at the top of Campaign on the wrong step.
        if not Path(BROWSER_PYTHON).exists():
            pytest.skip("no Playwright venv on this machine")
        proc = subprocess.run(
            [BROWSER_PYTHON, "-I", "-c", _STEP_SCRIPT, step_app.link, step_app.base],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode == 3:
            pytest.skip("Chrome cannot be launched for Playwright here")
        assert proc.returncode == 0, proc.stderr[-600:]
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        assert out["hash_step_3_current"] == "3", out
        assert out["editor_back_href"].endswith("#step-3"), out
        assert out["after_back_click_current"] == "3", out
        assert out["no_hash_current"] != "", out


_STEP_SCRIPT = r'''
import json, sys
link, base = sys.argv[1], sys.argv[2]
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sys.exit(3)
CUR = """() => { const e = document.querySelector('[data-step][aria-current]'); return e ? e.dataset.step : ''; }"""
out = {}
with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        sys.exit(3)
    page = browser.new_context(viewport={"width": 1280, "height": 860}).new_page()
    page.goto(link); page.wait_for_load_state("networkidle")
    page.goto(base + "/crm/drafts#step-3"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(600)
    out["hash_step_3_current"] = page.evaluate(CUR)
    page.goto(base + "/crm/drafts"); page.wait_for_timeout(400)
    out["no_hash_current"] = page.evaluate(CUR)
    page.goto(base + "/crm/drafts/contact-one"); page.wait_for_load_state("networkidle")
    back = page.locator("a", has_text="Back to step").first
    out["editor_back_href"] = back.get_attribute("href")
    back.click(); page.wait_for_load_state("networkidle"); page.wait_for_timeout(600)
    out["after_back_click_current"] = page.evaluate(CUR)
    browser.close()
print(json.dumps(out))
'''


_BROWSER_SCRIPT = r'''
import json, sys
link, base = sys.argv[1], sys.argv[2]
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sys.exit(3)

def lum(rgb):
    def f(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

JS_COLORS = """() => {
  const parse = s => (s.match(/[\\d.]+/g) || [0,0,0,1]).map(Number);
  let el = document.body, bg = [0,0,0,0];
  while (el) {
    const c = parse(getComputedStyle(el).backgroundColor);
    if ((c[3] === undefined ? 1 : c[3]) > 0.5) { bg = c; break; }
    el = el.parentElement;
  }
  if (bg[3] === 0) bg = [255,255,255,1];
  const fg = parse(getComputedStyle(document.body).color);
  return {bg: bg.slice(0,3), fg: fg.slice(0,3)};
}"""

def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)

out = {"console_errors": []}
with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        sys.exit(3)
    ctx = browser.new_context(viewport={"width": 1280, "height": 860}, color_scheme="dark")
    page = ctx.new_page()
    page.on("console", lambda m: out["console_errors"].append(m.text[:120]) if m.type == "error" else None)
    page.goto(link)
    page.wait_for_load_state("networkidle")
    theme = lambda: page.evaluate("document.documentElement.getAttribute('data-theme')")
    page.evaluate("localStorage.setItem('publik-signal-theme','light')")
    page.reload(); page.wait_for_load_state("networkidle")
    out["stored_light_applies"] = theme()
    c = page.evaluate(JS_COLORS)
    out["light_bg_luma"] = lum(c["bg"]); out["light_contrast"] = contrast(c["bg"], c["fg"])
    out["pages_light"] = {}
    for path in ("/crm", "/crm/drafts", "/partners", "/replies", "/crm/analytics", "/settings"):
        page.goto(base + path); page.wait_for_load_state("networkidle")
        out["pages_light"][path] = theme()
    page.goto(base + "/crm"); page.wait_for_load_state("networkidle")
    page.click("#themeToggle")
    page.wait_for_timeout(1000)  # let colour transitions finish before measuring
    out["after_toggle"] = theme()
    c = page.evaluate(JS_COLORS)
    out["dark_bg_luma"] = lum(c["bg"]); out["dark_contrast"] = contrast(c["bg"], c["fg"])
    page.reload(); page.wait_for_load_state("networkidle")
    out["persisted_after_reload"] = theme()
    for scheme, key in (("light", "system_light_no_storage"), ("dark", "system_dark_no_storage")):
        page.evaluate("localStorage.removeItem('publik-signal-theme')")
        page.emulate_media(color_scheme=scheme)
        page.reload(); page.wait_for_load_state("networkidle")
        out[key] = theme()
    browser.close()
print(json.dumps(out))
'''
