"""Behaviour tests for deck-upload messages and campaign step navigation.

Black box only. A real dev server (`python -m vcoutreach ui`) runs on a loopback port
with a throwaway workspace, always in Test Mode, with Gmail never connected, so
nothing here can send an email. Tests read only what a person sees: flash messages,
visible button text, the active step and the URL hash.

Specs covered
  1. Uploading something that is not a PDF shows a "not a PDF" message that is
     distinct from the "damaged or incomplete PDF" message.
  2. Campaign step 3 offers a Next button labelled "Next: Review".
  3. In a real browser, Next on campaign steps 1 to 4 opens the following step.

The wording of the upload messages was observed by driving the app, and only the
user-meaningful key words are pinned ("not a PDF", "damaged" or "incomplete").

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

NOT_A_PDF = re.compile(r"not a pdf", re.I)
DAMAGED = re.compile(r"damaged|incomplete", re.I)


# ---------------------------------------------------------------------------
# Plumbing: server process and a logged-in session
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class App:
    """One dev server on one workspace, always Test Mode. Can be stopped and restarted."""

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


def _text_of(markup: str) -> str:
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", body))).strip()


class Reply:
    """What a person sees after one request: the page text and its flash messages."""

    def __init__(self, resp: requests.Response):
        self.status = resp.status_code
        self.html = resp.text
        self.text = _text_of(self.html)
        self.flashes = [
            (kind, _text_of(inner).replace("×", "").strip())
            for kind, inner in re.findall(r'class="flash flash-(\w+)">(.*?)</li>', self.html, re.S)
        ]

    def kinds(self) -> list[str]:
        return [k for k, _ in self.flashes]

    def flash_text(self) -> str:
        return " | ".join(t for _, t in self.flashes)


class Client:
    """A logged-in browser session (the one-time link has been used)."""

    def __init__(self, app: App):
        self.app = app
        self.s = requests.Session()
        r = self.s.get(app.link, allow_redirects=True, timeout=30)
        assert r.status_code == 200, f"login link failed: {r.status_code}"
        self._csrf = None

    @property
    def base(self) -> str:
        return self.app.base

    def get(self, path: str) -> Reply:
        return Reply(self.s.get(self.base + path, timeout=60))

    def raw_get(self, path: str) -> requests.Response:
        return self.s.get(self.base + path, timeout=60)

    def csrf(self) -> str:
        if not self._csrf:
            html = self.s.get(self.base + "/crm", timeout=30).text
            m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
            assert m, "no csrf_token field on /crm"
            self._csrf = m.group(1)
        return self._csrf

    def post(self, path: str, data=None, files=None) -> Reply:
        d = dict(data or {})
        d["csrf_token"] = self.csrf()
        return Reply(self.s.post(self.base + path, data=d, files=files, timeout=120))

    def upload_deck(self, name: str, content: bytes, ctype: str = "application/pdf") -> Reply:
        return self.post("/crm/deck/upload", files={"deck": (name, content, ctype)})


def _onboard(c: Client) -> None:
    """A founder who finished the parts of setup possible without Gmail."""
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


@pytest.fixture(scope="module")
def onboarded():
    root = Path(tempfile.mkdtemp(prefix="pk-nav-onboarded-"))
    app = App(root)
    try:
        app.start()
        c = Client(app)
        _onboard(c)
        yield c
    finally:
        app.destroy()


@pytest.fixture(scope="module")
def real_pdf(onboarded):
    """A genuine, complete PDF: the example deck the app itself offers to a stuck user."""
    r = onboarded.post("/crm/deck/example")
    assert "success" in r.kinds(), r.flash_text()
    resp = onboarded.raw_get("/crm/deck")
    assert resp.status_code == 200 and resp.content.startswith(b"%PDF")
    assert resp.content.rstrip().endswith(b"%%EOF"), "example deck should end with %%EOF"
    return resp.content


def _deck_state(c: Client) -> tuple[int, int]:
    r = c.raw_get("/crm/deck")
    return r.status_code, len(r.content)


# ---------------------------------------------------------------------------
# Spec 1: deck upload tells "not a PDF" apart from "damaged or incomplete PDF"
# ---------------------------------------------------------------------------
PNG_BLOB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TEXT_BLOB = b"hello there, these are my meeting notes and not a deck at all"

NOT_PDF_UPLOADS = [
    ("text_named_notes_pdf", "notes.pdf", TEXT_BLOB, "application/pdf"),
    ("text_named_notes_txt", "notes.txt", TEXT_BLOB, "text/plain"),
    ("png_named_image_pdf", "image.pdf", PNG_BLOB, "application/pdf"),
    ("png_named_image_png", "image.png", PNG_BLOB, "image/png"),
]


def _damaged_variants(real: bytes) -> dict[str, bytes]:
    return {
        "truncated_real_pdf": real[: len(real) // 2],
        "real_pdf_missing_eof_marker": real.replace(b"%%EOF", b""),
        "pdf_header_only": b"%PDF-1.4\nx",
    }


class TestUploadMessages:
    @pytest.mark.parametrize(
        "case,name,content,ctype", NOT_PDF_UPLOADS, ids=[c[0] for c in NOT_PDF_UPLOADS]
    )
    def test_a_file_that_is_not_a_pdf_is_called_not_a_pdf(self, onboarded, case, name, content, ctype):
        """A person who uploads a text file or a picture, under a .pdf name or its true
        extension, is told in an error that the file is not a PDF, and no deck is attached."""
        # Catches: validation removed, a generic or success message, or the not-a-PDF
        # wording lost so the person is told the file is "damaged" instead.
        before = _deck_state(onboarded)
        r = onboarded.upload_deck(name, content, ctype)
        assert r.kinds() and set(r.kinds()) <= {"error", "warning"}, r.flashes
        msg = r.flash_text()
        assert NOT_A_PDF.search(msg), f"{case}: message should say the file is not a PDF, got {msg!r}"
        assert not DAMAGED.search(msg), f"{case}: a wrong file type must not be called damaged: {msg!r}"
        assert _deck_state(onboarded) == before

    def test_damaged_pdf_message_differs_from_the_not_a_pdf_message(self, onboarded, real_pdf):
        """A real PDF that is cut short, lacks its end marker, or is only a header gets a
        'damaged or incomplete' message that is a different message from 'not a PDF'."""
        # Catches: both failures collapsed into one message, so a person with a broken
        # export cannot tell it from picking the wrong file.
        not_pdf = onboarded.upload_deck("notes.pdf", TEXT_BLOB).flash_text()
        assert NOT_A_PDF.search(not_pdf), not_pdf
        for label, content in _damaged_variants(real_pdf).items():
            r = onboarded.upload_deck("deck.pdf", content)
            assert r.kinds() and set(r.kinds()) <= {"error", "warning"}, (label, r.flashes)
            msg = r.flash_text()
            assert DAMAGED.search(msg), f"{label}: message should say damaged or incomplete, got {msg!r}"
            assert not NOT_A_PDF.search(msg), f"{label}: a real but broken PDF is not 'not a PDF': {msg!r}"
            assert msg != not_pdf, label

    def test_every_bad_file_keeps_the_good_deck_attached(self, onboarded, real_pdf):
        """After a good deck is attached, neither kind of bad upload replaces or removes it."""
        # Catches: a failed upload of either kind deleting or overwriting the attached deck.
        before = _deck_state(onboarded)
        assert before[0] == 200 and before[1] == len(real_pdf)
        onboarded.upload_deck("notes.pdf", TEXT_BLOB)
        for content in _damaged_variants(real_pdf).values():
            onboarded.upload_deck("deck.pdf", content)
        assert _deck_state(onboarded) == before

    def test_a_good_pdf_still_uploads_after_both_kinds_of_failure(self, onboarded, real_pdf):
        """The person can retry after either error and a complete PDF is accepted with a
        success message and none of the failure wording."""
        # Catches: the failure path leaving the upload stuck, or a good PDF being rejected.
        onboarded.upload_deck("notes.pdf", TEXT_BLOB)
        onboarded.upload_deck("deck.pdf", real_pdf[: len(real_pdf) // 2])
        r = onboarded.upload_deck("my deck.pdf", real_pdf)
        assert "success" in r.kinds(), r.flashes
        assert not NOT_A_PDF.search(r.flash_text()) and not DAMAGED.search(r.flash_text())
        assert _deck_state(onboarded) == (200, len(real_pdf))


# ---------------------------------------------------------------------------
# Spec 2: campaign step 3 has a Next button that names the step it leads to
# ---------------------------------------------------------------------------
def _step_sections(html: str) -> dict[int, str]:
    """Split the campaign page into its numbered steps: {step number: markup up to the next step}."""
    opens = [
        (m.start(), int(m.group(1)))
        for m in re.finditer(r'<section\b[^>]*\bdata-step="(\d+)"[^>]*>', html)
    ]
    out: dict[int, str] = {}
    for i, (pos, n) in enumerate(opens):
        end = opens[i + 1][0] if i + 1 < len(opens) else len(html)
        out[n] = html[pos:end]
    return out


def _next_button_labels(section_markup: str) -> list[str]:
    return [
        _text_of(inner)
        for inner in re.findall(r"<button\b[^>]*\bdata-step-next\b[^>]*>(.*?)</button>", section_markup, re.S)
    ]


class TestStepThreeNextLabel:
    def test_step_three_next_button_reads_exactly_next_review(self, onboarded):
        """On campaign step 3 the primary forward control reads exactly 'Next: Review'
        (capital N and R, a colon, one space)."""
        # Catches: the label reverting to a bare 'Next', 'Next: Send', 'Next: review' or
        # 'NEXT: REVIEW', so the person cannot tell what step the button leads to.
        sections = _step_sections(onboarded.raw_get("/crm/drafts").text)
        assert 3 in sections, f"campaign page has no step 3 section, found {sorted(sections)}"
        labels = _next_button_labels(sections[3])
        assert labels, "step 3 has no Next button"
        assert labels[0] == "Next: Review", labels
        assert all(label == "Next: Review" for label in labels), labels

    def test_each_step_one_to_four_next_button_names_the_following_step(self, onboarded):
        """Every campaign step from 1 to 4 has a Next button whose label starts with
        'Next: ' and names a step different for each step, so no two buttons promise the
        same destination."""
        # Catches: buttons all saying 'Next', or one label copy-pasted onto several steps.
        sections = _step_sections(onboarded.raw_get("/crm/drafts").text)
        seen = []
        for n in range(1, 5):
            assert n in sections, f"no step {n} section"
            labels = _next_button_labels(sections[n])
            assert labels, f"step {n} has no Next button"
            assert re.fullmatch(r"Next: \S.*", labels[0]), (n, labels)
            seen.append(labels[0])
        assert len(set(seen)) == 4, seen


# ---------------------------------------------------------------------------
# Spec 3: in a real browser, Next opens the FOLLOWING step
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def browser_walk():
    """Walk campaign steps 1 to 4 clicking Next in a real Chrome and record what changed."""
    if not Path(BROWSER_PYTHON).exists():
        pytest.skip("no Playwright venv on this machine")
    root = Path(tempfile.mkdtemp(prefix="pk-nav-browser-"))
    app = App(root)
    try:
        app.start()
        _onboard(Client(app))
        app.stop()
        app.start()  # fresh one-time link for the browser
        proc = subprocess.run(
            [BROWSER_PYTHON, "-I", "-c", _WALK_SCRIPT, app.link, app.base],
            capture_output=True, text=True, timeout=240,
        )
        if proc.returncode == 3:
            pytest.skip("Chrome cannot be launched for Playwright here")
        assert proc.returncode == 0, proc.stderr[-800:]
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        app.destroy()


class TestNextOpensTheFollowingStep:
    @pytest.mark.parametrize("n", [1, 2, 3, 4])
    def test_clicking_next_on_step_opens_the_following_step(self, browser_walk, n):
        """Clicking the visible Next button on campaign step N makes step N+1 the active,
        visible step, moves the step indicator to N+1, and updates the URL hash to
        #step-(N+1). The person never stays on step N."""
        # Catches: a Next button that does nothing, reopens the same step, skips a step,
        # or opens a step whose indicator and hash disagree with what is on screen.
        walk = browser_walk[str(n)]
        assert walk["start_step"] == str(n), walk
        assert walk["next_visible"], walk
        assert walk["active_after"] != walk["start_step"], f"stayed on step {n}: {walk}"
        assert walk["active_after"] == str(n + 1), walk
        assert walk["indicator_after"] == str(n + 1), walk
        assert walk["next_section_visible"], walk
        assert walk["url_hash_after"] == f"#step-{n + 1}", walk
        assert not walk["old_next_still_visible"], f"step {n} Next is still showing: {walk}"

    def test_step_three_next_button_is_labelled_next_review_in_the_browser(self, browser_walk):
        """The Next button a person actually sees and clicks on step 3 reads 'Next: Review'."""
        # Catches: markup that says one thing while CSS or script shows another label.
        assert browser_walk["3"]["next_text"] == "Next: Review", browser_walk["3"]

    def test_the_walk_ended_in_test_mode_with_no_page_errors(self, browser_walk):
        """Walking the steps leaves the app in Test Mode and logs no console errors."""
        # Catches: navigation script errors or a mode flip during the walk.
        assert browser_walk["test_mode_banner"], browser_walk
        assert not browser_walk["live_mode_banner"], browser_walk
        assert browser_walk["console_errors"] == [], browser_walk["console_errors"]


_WALK_SCRIPT = r'''
import json, sys
link, base = sys.argv[1], sys.argv[2]
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sys.exit(3)
ACTIVE = """() => { const e = document.querySelector('section[data-step][aria-current]'); return e ? e.dataset.step : ''; }"""
INDICATOR = """() => { const e = document.querySelector('[data-step-jump][aria-current]'); return e ? e.dataset.stepJump : ''; }"""
out = {"console_errors": []}
with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        sys.exit(3)
    page = browser.new_context(viewport={"width": 1280, "height": 860}).new_page()
    page.on("console", lambda m: out["console_errors"].append(m.text[:160]) if m.type == "error" else None)
    page.goto(link); page.wait_for_load_state("networkidle")
    page.goto(base + "/crm/drafts#step-1"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(700)
    for n in range(1, 5):
        rec = {"start_step": page.evaluate(ACTIVE)}
        nxt = page.locator('section[data-step="%d"] [data-step-next]' % n).first
        rec["next_visible"] = nxt.is_visible()
        rec["next_text"] = nxt.inner_text().strip()
        nxt.click(); page.wait_for_timeout(900)
        rec["active_after"] = page.evaluate(ACTIVE)
        rec["indicator_after"] = page.evaluate(INDICATOR)
        rec["next_section_visible"] = page.locator('section[data-step="%d"]' % (n + 1)).first.is_visible()
        rec["url_hash_after"] = page.evaluate("location.hash")
        rec["old_next_still_visible"] = page.locator('section[data-step="%d"] [data-step-next]' % n).first.is_visible()
        out[str(n)] = rec
    body = page.inner_text("body")
    out["test_mode_banner"] = "TEST MODE" in body
    out["live_mode_banner"] = "LIVE MODE" in body
    browser.close()
print(json.dumps(out))
'''
