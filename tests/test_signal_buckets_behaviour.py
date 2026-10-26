"""Behaviour tests for the signal buckets on the Analytics page (/crm/analytics).

Hot, Warm, Engaged and Replied share ONE tab strip. These tests drive the page the way a
person meets it: a workspace with real contacts, real (recorded, never delivered) sends,
and link, deck and mail activity that happened days after the send. They read only what the
page renders and how a real browser reacts to it.

Public contract under test
  1. Each tab's number equals the contacts listed in its panel (sizes differ, one is zero).
  2. Exactly one tab is selected on load (aria-selected="true" plus the active class): the
     first non-empty bucket, or the first tab when every bucket is empty. The rest are false.
  3. Click, ArrowRight, ArrowLeft, Home and End on the tablist show that tab's panel, hide
     the others and keep aria-selected honest (real browser, skipped only if truly absent).
  4. Every tab's aria-controls points at an existing role="tabpanel", whose aria-labelledby
     points back at the tab. Tab ids and panel ids are unique.
  5. One panel per tab. Only the default panel is visible on load, the others carry the
     hidden attribute. Contacts show as rows in their own bucket's panel. An empty bucket
     says "Nobody here yet".

Mutation check: each test names the breakage it kills in a "Catches:" line.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import body, go_live
from test_ux_pass_wave2 import db, parse, send_batch

pytestmark = pytest.mark.local_http  # loopback only, to the app under test (browser test)

PYTHON = sys.executable
STAGING = os.environ.get("VCO_STAGING_SRC", "/Users/akrit/opensource-app-staging/vcoutreach")
SITE = os.environ.get("VCO_SITE_PACKAGES", "/Users/akrit/twitter-x-automation/venv/Lib/site-packages")
BROWSER_PYTHON = "/Users/akrit/VC emails/.venv/bin/python"

ORDER = ["hot", "warm", "engaged", "replied"]
LABELS = {"hot": "Hot", "warm": "Warm", "engaged": "Engaged", "replied": "Replied"}
EMPTY_TEXT = "Nobody here yet"
PEOPLE = ["Ann Ash", "Bob Birch", "Cy Cedar", "Di Dogwood", "Ed Elm", "Flo Fir", "Gus Gum"]
ANN, BOB, CY, DI, ED, FLO, GUS = range(1, 8)  # partner ids follow import order


def _event(pid, kind, days_ago, label):
    # (partner id, event type, who did it, days after "now" it happened, source)
    source = {"click": "link", "deck_view": "deck", "open": "pixel"}[kind]
    return (pid, kind, "human_likely", days_ago, f"{source}-{label}")


# Each scenario: seeded activity, who wrote back, and the known truth the page must show.
SCENARIOS = {
    # Hot 2, Warm 1, Engaged 3, Replied 0. Default is the first tab.
    "mixed": dict(
        events=[
            _event(ANN, "click", 1, "a"), _event(ANN, "click", 2, "b"),
            _event(BOB, "click", 1, "a"), _event(BOB, "deck_view", 3, "a"),
            _event(CY, "deck_view", 2, "a"),
            _event(DI, "click", 15, "a"), _event(ED, "click", 20, "a"), _event(FLO, "click", 25, "a"),
        ],
        replied=[],
        buckets={"hot": ["Ann Ash", "Bob Birch"], "warm": ["Cy Cedar"],
                 "engaged": ["Di Dogwood", "Ed Elm", "Flo Fir"], "replied": []},
        default="hot",
    ),
    # Hot 0, Warm 0, Engaged 2, Replied 1. Default is the THIRD tab.
    "late": dict(
        events=[_event(ANN, "click", 10, "a"), _event(DI, "click", 15, "a")],
        replied=[GUS],
        buckets={"hot": [], "warm": [], "engaged": ["Ann Ash", "Di Dogwood"], "replied": ["Gus Gum"]},
        default="engaged",
    ),
    # Hot 0, Warm 2, Engaged 0, Replied 0. Default is the SECOND tab.
    "warm_only": dict(
        events=[_event(CY, "deck_view", 2, "a"), _event(DI, "deck_view", 3, "a")],
        replied=[],
        buckets={"hot": [], "warm": ["Cy Cedar", "Di Dogwood"], "engaged": [], "replied": []},
        default="warm",
    ),
    # Sent, but nobody did anything. Every bucket empty, default is the FIRST tab.
    "quiet": dict(
        events=[], replied=[],
        buckets={"hot": [], "warm": [], "engaged": [], "replied": []},
        default="hot",
    ),
}
SCENARIO_IDS = list(SCENARIOS)


def _contact(i, name):
    return csv_row(
        id=f"p{i}", name=name, email=f"p{i}@example.com", org=f"Org{i}", role="Partner",
        facts=f"your essay on how small teams ship weekly | your role leading Org{i}",
        source_urls="https://example.com/e",
    )


def build_workspace(send_web, scenario):
    """Seven contacts, seven recorded live-mode sends 40 days ago, then the scenario's activity."""
    spec = SCENARIOS[scenario]
    w = send_web([_contact(i, n) for i, n in enumerate(PEOPLE)])
    go_live(w)
    send_batch(w, "SEND BATCH OF 7")
    assert len(w.transport.calls) == 7, "setup: seven sends should have gone through the recording transport"
    c = db(w)
    now = datetime.now(timezone.utc)

    def ts(days):
        return (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    c.execute("update crm_send_evidence set observed_at=?", (ts(40),))
    c.execute("update web_send_journal set created_at=?, completed_at=?", (ts(40), ts(40)))
    for pid in range(1, 8):
        c.execute(
            "insert into crm_tracking_actions(action_id,partner_id,batch_key,activated_at,last_synced_at,sync_status)"
            " values(?,?,?,?,?,?)", (f"a{pid}", pid, "b", ts(40), ts(0), "ok"))
    for pid, kind, who, days, source in spec["events"]:
        c.execute(
            "insert into crm_tracking_events(action_id,partner_id,occurred_at,event_type,source_kind,"
            "classification,seconds_since_send) values(?,?,?,?,?,?,?)",
            (f"a{pid}", pid, ts(days), kind, source, who, 86400 * (40 - days)))
    for pid in spec["replied"]:
        c.execute(
            "insert into crm_mail_evidence(message_id,partner_id,observed_at,thread_id,source_mailbox,evidence_kind)"
            " values(?,?,?,?,'inbox','incoming_mail')", (f"mm{pid}", pid, ts(3), f"t{pid}"))
    c.commit()
    c.close()
    return w


_PAGES: dict[str, str] = {}


def page_for(send_web, scenario):
    """The rendered Analytics page for a scenario, built once per test session."""
    if scenario not in _PAGES:
        w = build_workspace(send_web, scenario)
        r = w.get("/crm/analytics", follow_redirects=True)
        assert r.status_code == 200
        _PAGES[scenario] = body(r)
    return _PAGES[scenario]


class Strip:
    """What a person sees: the four tabs, their numbers, and each panel's contact rows."""

    def __init__(self, markup):
        self.root = parse(markup)
        self.tabs = [n for n in self.root.walk() if n.attrs.get("role") == "tab"]
        self.panels = [n for n in self.root.walk() if n.attrs.get("role") == "tabpanel"]
        self.tablists = [n for n in self.root.walk() if n.attrs.get("role") == "tablist"]
        self.by_label = {}
        for t in self.tabs:
            text = t.all_text()
            for key, label in LABELS.items():
                if text.startswith(label):
                    self.by_label[key] = t
        self.panel_by_id = {p.attrs.get("id"): p for p in self.panels}

    def number(self, key):
        tab = self.by_label[key]
        m = re.search(r"(\d+)\s*$", tab.all_text())
        assert m, f"{key} tab shows no number: {tab.all_text()!r}"
        return int(m.group(1))

    def panel(self, key):
        tab = self.by_label[key]
        return self.panel_by_id[tab.attrs.get("aria-controls")]

    def contact_rows(self, key):
        return [tr for tr in self.panel(key).find_all("tr")
                if "an-empty" not in tr.classes and EMPTY_TEXT not in tr.all_text()]

    def names_in(self, key):
        found = []
        for tr in self.contact_rows(key):
            text = tr.all_text()
            found.extend(n for n in PEOPLE if n in text)
        return found


@pytest.fixture
def strip_for(send_web):
    return lambda scenario: Strip(page_for(send_web, scenario))


# ---------------------------------------------------------------------------------------------------------------
# Spec 1: numbers equal contacts shown
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_each_tab_number_equals_the_contacts_in_its_panel(strip_for, scenario):
    # Catches: a count taken from the wrong bucket, off by one, or computed separately from the rows.
    s = strip_for(scenario)
    assert [k for k in ORDER if k in s.by_label] == ORDER, "all four buckets need a tab"
    for key in ORDER:
        expected = SCENARIOS[scenario]["buckets"][key]
        assert s.number(key) == len(expected), f"{key} tab number"
        assert len(s.contact_rows(key)) == s.number(key), f"{key} rows vs tab number"


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_each_contact_sits_in_the_bucket_its_activity_earns(strip_for, scenario):
    # Catches: panels swapped, rows rendered into the neighbouring panel, or a bucket sharing another's rows.
    s = strip_for(scenario)
    for key in ORDER:
        assert sorted(s.names_in(key)) == sorted(SCENARIOS[scenario]["buckets"][key]), key


def test_the_seed_really_gives_buckets_of_different_sizes_including_zero(strip_for):
    # Guards the fixture itself: counts must be distinguishable, so a swapped count cannot hide.
    s = strip_for("mixed")
    sizes = [s.number(k) for k in ORDER]
    assert sizes == [2, 1, 3, 0]
    assert len(set(sizes)) == 4


# ---------------------------------------------------------------------------------------------------------------
# Spec 2: default tab
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_exactly_one_tab_is_selected_and_it_is_the_first_non_empty_bucket(strip_for, scenario):
    # Catches: default always the first tab, always the last, the biggest bucket, or no tab selected.
    s = strip_for(scenario)
    selected = [k for k in ORDER if s.by_label[k].attrs.get("aria-selected") == "true"]
    assert selected == [SCENARIOS[scenario]["default"]]
    first_non_empty = next((k for k in ORDER if SCENARIOS[scenario]["buckets"][k]), ORDER[0])
    assert selected == [first_non_empty]
    for key in ORDER:
        tab = s.by_label[key]
        is_default = key == selected[0]
        assert tab.attrs.get("aria-selected") == ("true" if is_default else "false"), key
        assert ("is-active" in tab.classes) == is_default, f"{key} active class"


# ---------------------------------------------------------------------------------------------------------------
# Spec 4: tab and panel wiring
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["mixed", "quiet"])
def test_every_tab_controls_a_real_panel_that_points_back(strip_for, scenario):
    # Catches: aria-controls aimed at a missing or wrong panel, panel labelled by the wrong tab.
    s = strip_for(scenario)
    assert len(s.tabs) == 4 and len(s.panels) == 4
    for key in ORDER:
        tab = s.by_label[key]
        target = tab.attrs.get("aria-controls")
        assert target, f"{key} tab has no aria-controls"
        assert target in s.panel_by_id, f"{key} tab controls {target!r}, which is not a tabpanel"
        assert s.panel_by_id[target].attrs.get("aria-labelledby") == tab.attrs.get("id"), key
    controlled = [s.by_label[k].attrs.get("aria-controls") for k in ORDER]
    assert len(set(controlled)) == 4, "two tabs controlling one panel"


def test_tab_ids_and_panel_ids_are_unique_and_present(strip_for):
    # Catches: duplicated or missing ids, which break the label and control links.
    s = strip_for("mixed")
    ids = [n.attrs.get("id") for n in s.tabs] + [n.attrs.get("id") for n in s.panels]
    assert all(ids), "every tab and panel needs an id"
    assert len(ids) == len(set(ids)) == 8
    every_id = [n.attrs["id"] for n in s.root.walk() if n.attrs.get("id")]
    for i in ids:
        assert every_id.count(i) == 1, f"id {i} appears more than once on the page"


def test_all_four_buckets_share_one_tablist(strip_for):
    # Catches: buckets split into separate tab strips, or tabs outside the tablist.
    s = strip_for("mixed")
    assert len(s.tablists) == 1
    inside = [n for n in s.tablists[0].walk() if n.attrs.get("role") == "tab"]
    assert len(inside) == 4 == len(s.tabs)
    assert [t.all_text().split()[0] for t in inside] == [LABELS[k] for k in ORDER]


# ---------------------------------------------------------------------------------------------------------------
# Spec 5: panels, visibility, empty state
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_only_the_default_panel_is_visible_on_load(strip_for, scenario):
    # Catches: every panel visible at once, none visible, or the visible panel not matching the selected tab.
    s = strip_for(scenario)
    default = SCENARIOS[scenario]["default"]
    for key in ORDER:
        panel = s.panel(key)
        if key == default:
            assert "hidden" not in panel.attrs, f"{key} is the default and must be shown"
        else:
            assert "hidden" in panel.attrs, f"{key} must be hidden on load"


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_an_empty_bucket_says_nobody_here_yet_and_a_full_one_does_not(strip_for, scenario):
    # Catches: empty-state text removed, shown on a bucket that has people, or an empty panel left blank.
    s = strip_for(scenario)
    for key in ORDER:
        text = s.panel(key).all_text()
        if SCENARIOS[scenario]["buckets"][key]:
            assert EMPTY_TEXT not in text, f"{key} has people but says it is empty"
        else:
            assert EMPTY_TEXT in text, f"{key} is empty and should say so"
            assert s.contact_rows(key) == []


# ---------------------------------------------------------------------------------------------------------------
# Spec 3: real browser behaviour
# ---------------------------------------------------------------------------------------------------------------

def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class LiveApp:
    def __init__(self, workspace, home, logs):
        self.workspace, self.home, self.logs = workspace, home, logs
        self.port = _free_port()
        self.proc = None
        self.link = ""

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        env = dict(os.environ)
        env.update(HOME=str(self.home), SEND_MODE="test", OUTREACH_HOME=str(self.workspace),
                   PYTHONPATH=f"{SITE}:{STAGING}")
        for k in list(env):
            if k.endswith("_API_KEY") or k in ("GMAIL_APP_PASSWORD", "OUTREACH_SCHEDULER"):
                env.pop(k)
        log_path = self.logs / "server.log"
        with open(log_path, "w") as log:
            self.proc = subprocess.Popen(
                [PYTHON, "-m", "vcoutreach", "ui", "--workspace", str(self.workspace),
                 "--port", str(self.port), "--no-browser"],
                cwd=str(self.workspace), env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("app exited at startup:\n" + log_path.read_text()[-800:])
            m = re.search(r"http://127\.0\.0\.1:\d+/\?token=\S+", log_path.read_text())
            if m:
                self.link = m.group(0)
                return self
            time.sleep(0.15)
        raise RuntimeError("app did not print its one-time link in 60 seconds")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)


_BROWSER_SCRIPT = r'''
import json, sys
link, base = sys.argv[1], sys.argv[2]
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sys.exit(3)
SNAP = """() => {
  const tabs = [...document.querySelectorAll('[role=tablist] [role=tab]')];
  const panels = [...document.querySelectorAll('[role=tabpanel]')];
  const vis = el => { const cs = getComputedStyle(el); return !el.hidden && cs.display !== 'none' && cs.visibility !== 'hidden'; };
  return {
    tabs: tabs.map(t => ({label: t.textContent.trim().split(/\\s+/)[0], selected: t.getAttribute('aria-selected'),
                          active: t.classList.contains('is-active'), controls: t.getAttribute('aria-controls')})),
    panels: panels.map(p => ({id: p.id, visible: vis(p), hasHidden: p.hasAttribute('hidden'),
                              rows: [...p.querySelectorAll('tr')].filter(r => !r.classList.contains('an-empty')).map(r => r.textContent.replace(/\\s+/g,' ').trim())})),
    focused: document.activeElement ? (document.activeElement.getAttribute('role') + ':' + document.activeElement.textContent.trim().split(/\\s+/)[0]) : ''
  };
}"""
steps = []
with sync_playwright() as p:
    try:
        browser = p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        sys.exit(3)
    page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:160]))
    page.goto(link); page.wait_for_load_state("networkidle")
    page.goto(base + "/crm/analytics"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(300)
    def tab(label):
        return page.locator('[role=tablist] [role=tab]', has_text=label).first
    def snap(name):
        s = page.evaluate(SNAP); s["step"] = name; steps.append(s)
    snap("load")
    for label in ("Engaged", "Replied", "Warm", "Hot"):
        tab(label).click(); page.wait_for_timeout(150); snap("click " + label)
    tab("Warm").click(); page.wait_for_timeout(100)
    tab("Warm").focus()
    for key in ("ArrowRight", "ArrowRight", "ArrowLeft", "ArrowLeft", "ArrowRight"):
        page.keyboard.press(key); page.wait_for_timeout(150); snap("arrow " + key)
    tab("Warm").click(); page.wait_for_timeout(100)
    tab("Warm").focus()
    for key in ("End", "Home", "End", "ArrowLeft", "Home"):
        page.keyboard.press(key); page.wait_for_timeout(150); snap("edge " + key)
    out = {"steps": steps, "errors": errors}
    browser.close()
print(json.dumps(out))
'''


def _check_state(snap, expect_label, buckets):
    """One snapshot must show exactly one selected tab, its panel visible, the rest hidden."""
    labels = [t["label"] for t in snap["tabs"]]
    assert labels == [LABELS[k] for k in ORDER], snap["step"]
    selected = [t for t in snap["tabs"] if t["selected"] == "true"]
    assert [t["label"] for t in selected] == [expect_label], f"{snap['step']}: selected {selected}"
    for t in snap["tabs"]:
        if t["label"] != expect_label:
            assert t["selected"] == "false", f"{snap['step']}: {t['label']} should be aria-selected=false"
        assert t["active"] == (t["label"] == expect_label), f"{snap['step']}: active class on {t['label']}"
    visible = [p for p in snap["panels"] if p["visible"]]
    assert len(visible) == 1, f"{snap['step']}: {len(visible)} panels visible"
    chosen = next(t for t in snap["tabs"] if t["label"] == expect_label)
    assert visible[0]["id"] == chosen["controls"], f"{snap['step']}: wrong panel visible"
    key = next(k for k in ORDER if LABELS[k] == expect_label)
    names = [n for n in PEOPLE if any(n in row for row in visible[0]["rows"])]
    assert sorted(names) == sorted(buckets[key]), f"{snap['step']}: panel shows {names}"
    for p in snap["panels"]:
        if p is not visible[0]:
            assert p["hasHidden"], f"{snap['step']}: hidden panel {p['id']} lacks the hidden attribute"


_BROWSER_RUN: dict = {}


def browser_run(send_web, tmp_path):
    """Drive the real page once per session; every browser test reads the same recorded steps."""
    if "out" in _BROWSER_RUN:
        return _BROWSER_RUN["out"]
    if not Path(BROWSER_PYTHON).exists():
        pytest.skip("no Playwright venv on this machine (expected at " + BROWSER_PYTHON + ")")
    w = build_workspace(send_web, "mixed")
    home = tmp_path / "live-home"
    logs = tmp_path / "live-logs"
    home.mkdir()
    logs.mkdir()
    app = LiveApp(w.ws, home, logs)
    try:
        app.start()
        proc = subprocess.run([BROWSER_PYTHON, "-I", "-c", _BROWSER_SCRIPT, app.link, app.base],
                              capture_output=True, text=True, timeout=240)
    finally:
        app.stop()
    if proc.returncode == 3:
        pytest.skip("Playwright or Chrome cannot be launched here (exit 3)")
    assert proc.returncode == 0, proc.stderr[-800:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    _BROWSER_RUN["out"] = out
    return out


def _phase(out, prefix):
    return [s for s in out["steps"] if s["step"].startswith(prefix)]


def test_real_browser_click_shows_that_panel_and_hides_the_rest(send_web, tmp_path):
    # Catches: clicks that do nothing, panels not swapping, aria-selected or the active class left on the old tab.
    out = browser_run(send_web, tmp_path)
    steps = {s["step"]: s for s in out["steps"]}
    buckets = SCENARIOS["mixed"]["buckets"]
    for step, label in [("load", "Hot"), ("click Engaged", "Engaged"), ("click Replied", "Replied"),
                        ("click Warm", "Warm"), ("click Hot", "Hot")]:
        _check_state(steps[step], label, buckets)
    shown = next(p for p in steps["click Replied"]["panels"] if p["visible"])
    assert shown["rows"] == [], "the empty Replied bucket must list nobody"
    assert out["errors"] == [], out["errors"]


def test_real_browser_arrow_keys_move_between_neighbouring_tabs(send_web, tmp_path):
    # Catches: ArrowRight or ArrowLeft ignored, reversed, moving focus without showing the panel.
    out = browser_run(send_web, tmp_path)
    buckets = SCENARIOS["mixed"]["buckets"]
    walk = [("ArrowRight", "Engaged"), ("ArrowRight", "Replied"), ("ArrowLeft", "Engaged"),
            ("ArrowLeft", "Warm"), ("ArrowRight", "Engaged")]
    got = _phase(out, "arrow ")
    assert [s["step"] for s in got] == ["arrow " + k for k, _ in walk]
    for snap, (key, label) in zip(got, walk):
        _check_state(snap, label, buckets)


def test_real_browser_home_and_end_jump_to_the_first_and_last_tab(send_web, tmp_path):
    # Catches: Home or End ignored, or jumping to the wrong end of the strip.
    out = browser_run(send_web, tmp_path)
    buckets = SCENARIOS["mixed"]["buckets"]
    walk = [("End", "Replied"), ("Home", "Hot"), ("End", "Replied"), ("ArrowLeft", "Engaged"), ("Home", "Hot")]
    got = _phase(out, "edge ")
    assert [s["step"] for s in got] == ["edge " + k for k, _ in walk]
    for snap, (key, label) in zip(got, walk):
        _check_state(snap, label, buckets)
