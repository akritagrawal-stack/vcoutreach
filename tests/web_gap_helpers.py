"""Extra helpers for the test_web_gap_*.py files (mutation-gap tests for the web app).

Written from SPEC_V3.md, SPEC.md sections 3 and 10, CONTRACT.md, the README, docs/*.md, the documented
schema file (vcoutreach/web/schema.sql) and the existing web tests. Nothing here reads the implementation.

Everything works through public behavior: HTTP routes and forms, workspace files (.env, drafts, the
shared journal), the documented SQLite tables, and the injected fake send transport.

No dash characters appear in this file.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

import pytest

from web_helpers import (  # noqa: F401
    ADA, GRACE, LINUS, SENDER_EMAIL, STANDARD_ROWS, TEST_INBOX, Web, banner_mode, body, contact_ref,
    create_web_app, csv_row, go_live, go_test, page_text, read_env, send_url, set_stage, typed, write_env,
)

# ---------------------------------------------------------------------------------------------------------------
# More fictional contacts (example.com only)
# ---------------------------------------------------------------------------------------------------------------

def person(slug: str, name: str) -> Dict[str, str]:
    org = f"{name.split()[0]} Works"
    return csv_row(id=slug, name=name, email=f"{slug}@example.com", org=org, role="Partner",
                   facts=f"your note on {slug} shipping weekly | your role at {org}",
                   source_urls=f"https://example.com/{slug}")


BEA = person("bea", "Bea Arthur")
CYD = person("cyd", "Cyd Charisse")
DOV = person("dov", "Dov Hammer")
ELI = person("eli", "Eli Whitney")
FAY = person("fay", "Fay Wray")
GUS = person("gus", "Gus Grissom")


# ---------------------------------------------------------------------------------------------------------------
# The workspace database (documented schema: vcoutreach/web/schema.sql)
# ---------------------------------------------------------------------------------------------------------------

def db_file(w: Web) -> Path:
    return Path(w.ws) / "data" / "outreach.db"


def sql(w: Web, query: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
    conn = sqlite3.connect(str(db_file(w)), timeout=10)
    try:
        return list(conn.execute(query, tuple(params)))
    finally:
        conn.close()


def sql_exec(w: Web, query: str, params: Sequence[Any] = ()) -> None:
    conn = sqlite3.connect(str(db_file(w)), timeout=10)
    try:
        conn.execute(query, tuple(params))
        conn.commit()
    finally:
        conn.close()


def partner_row_id(w: Web, slug: str) -> int:
    rows = sql(w, "SELECT id FROM partners WHERE slug=?", (slug,))
    assert rows, f"no contact with slug {slug!r} in the workspace database"
    return int(rows[0][0])


def draft_row_id(w: Web, slug: str) -> int:
    rows = sql(w, "SELECT id FROM drafts WHERE partner_id=? ORDER BY id DESC", (partner_row_id(w, slug),))
    assert rows, f"no draft row for {slug!r}"
    return int(rows[0][0])


def draft_status(w: Web, slug: str) -> str:
    rows = sql(w, "SELECT status FROM drafts WHERE partner_id=? ORDER BY id DESC", (partner_row_id(w, slug),))
    return str(rows[0][0]) if rows else ""


def scheduled_rows(w: Web) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(str(db_file(w)), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM scheduled_sends ORDER BY id")]
    finally:
        conn.close()


def live_journal_rows(w: Web) -> List[Tuple[Any, ...]]:
    return sql(w, "SELECT recipient, lane, mode, state FROM web_send_journal WHERE mode='live'")


def sent_to(w: Web) -> List[str]:
    """Every recipient the fake transport was handed (flat, lower case)."""
    assert w.transport is not None
    out: List[str] = []
    for call in w.transport.calls:
        out.extend(call.recipients)
    return out


def drop_env(w: Web, *names: str) -> None:
    """Remove NAME=... lines from the workspace .env, keeping every other line."""
    path = Path(w.ws) / ".env"
    kept = [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.split("=", 1)[0].strip() not in names]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    path.chmod(0o600)


def set_env(w: Web, **values: str) -> None:
    """Set NAME=value lines in the workspace .env, keeping every other line."""
    drop_env(w, *values)
    path = Path(w.ws) / ".env"
    with open(path, "a", encoding="utf-8") as handle:
        for name, value in values.items():
            handle.write(f"{name}={value}\n")
    path.chmod(0o600)


# ---------------------------------------------------------------------------------------------------------------
# Pushing selected drafts (the batch form and /push)
# ---------------------------------------------------------------------------------------------------------------

def push_drafts(w: Web, slugs: Sequence[str], route: str = "send_batch", *, follow: bool = True):
    """Select these drafts, then try the typed batch phrase for each plausible count, biggest first,
    until the fake transport receives something. The spec says n is the number of selected approved drafts, and
    a selection can mix eligible and ineligible drafts, so the right n is not fixed here: what the tests assert
    is who the transport was handed to. Returns the last response."""
    assert w.transport is not None
    ids = [str(draft_row_id(w, s)) for s in slugs]
    response = None
    for n in range(len(ids), 0, -1):
        before = len(w.transport.calls)
        payload = typed(f"SEND BATCH OF {n}")
        payload["draft_ids"] = ids
        response = w.post(w.url(route), payload, follow=follow)
        if len(w.transport.calls) > before:
            break
    return response


def push_all(w: Web, *, max_n: int = 8):
    """POST /push without a selection, trying every plausible count until something is sent."""
    assert w.transport is not None
    response = None
    for n in range(max_n, 0, -1):
        before = len(w.transport.calls)
        response = w.post(w.url("push"), typed(f"SEND BATCH OF {n}"), follow=True)
        if len(w.transport.calls) > before:
            break
    return response


# ---------------------------------------------------------------------------------------------------------------
# A lock held by ANOTHER process (works for flock and for fcntl.lockf style single-flight locks)
# ---------------------------------------------------------------------------------------------------------------

_HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys
    path = sys.argv[1]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        pass
    sys.stdout.write("held\\n")
    sys.stdout.flush()
    sys.stdin.read()
    """
)


@contextlib.contextmanager
def lock_held_elsewhere(path: Path) -> Iterator[None]:
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.strip() == "held", "the helper process could not take the lock"
        yield
    finally:
        with contextlib.suppress(Exception):
            assert proc.stdin is not None
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------------------------------------------
# The shared journal file (.vcoutreach/journal.jsonl, engine format as documented in docs/web-app.md)
# ---------------------------------------------------------------------------------------------------------------

def journal_key(recipient: str, lane: str = "first_touch") -> str:
    return hashlib.sha256(f"{recipient.lower()}|{lane}".encode("utf-8")).hexdigest()


def append_journal_line(w: Web, recipient: str, mode: str, *, lane: str = "first_touch") -> None:
    path = Path(w.ws) / ".vcoutreach" / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "key": journal_key(recipient, lane), "mode": mode,
        "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "contact_id": recipient.split("@")[0], "lane": lane, "recipient": recipient,
        "routed_to": recipient, "message_id": "<earlier@mail.test>",
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


def insert_attempt(w: Web, recipient: str, state: str, *, mode: str = "live", lane: str = "first_touch") -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql_exec(w, "INSERT INTO web_send_journal(recipient, lane, mode, state, target_email, created_at) "
                "VALUES(?,?,?,?,?,?)", (recipient, lane, mode, state, recipient, now))


# ---------------------------------------------------------------------------------------------------------------
# Running the scheduler without waiting a minute (OUTREACH_SCHEDULER=1)
# ---------------------------------------------------------------------------------------------------------------

class _StopLoop(BaseException):
    pass


class SchedulerHarness:
    """create_app with OUTREACH_SCHEDULER=1 starts one daemon thread. Thread.start is intercepted so the thread
    never runs on its own; run_ticks() then runs its loop body on a helper thread with the waiting calls
    (time.sleep, Event.wait) shortened, so each call to run_ticks() performs the scheduler's work without
    waiting for its interval. The loop is cut after `sleeps_allowed` waits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, build, *, switch: Optional[str] = "1"):
        self.monkeypatch = monkeypatch
        self.captured: List[threading.Thread] = []
        real_start = threading.Thread.start
        self.real_start = real_start
        captured = self.captured

        def capture(thread: threading.Thread, *a: Any, **k: Any) -> None:
            captured.append(thread)

        if switch is not None:
            monkeypatch.setenv("OUTREACH_SCHEDULER", switch)
        monkeypatch.setattr(threading.Thread, "start", capture)
        try:
            self.web: Web = build()
        finally:
            monkeypatch.setattr(threading.Thread, "start", real_start)

    def run_ticks(self, *, sleeps_allowed: int = 2, timeout: float = 25.0) -> bool:
        """Run every captured thread's loop body. Returns False when it did not stop in time."""
        real_sleep = time.sleep
        real_wait = threading.Event.wait
        count = {"n": 0}
        runner_name = "gap-scheduler-runner"

        def short_sleep(*a: Any, **k: Any) -> None:
            if threading.current_thread().name != runner_name:
                return real_sleep(*a, **k)
            count["n"] += 1
            if count["n"] >= sleeps_allowed:
                raise _StopLoop()
            return None

        def short_wait(event: threading.Event, timeout: Optional[float] = None) -> bool:
            if threading.current_thread().name != runner_name:
                return real_wait(event, timeout)
            short_sleep()
            return event.is_set()

        def body_() -> None:
            try:
                for thread in list(self.captured):
                    thread.run()
            except _StopLoop:
                pass
            except BaseException:  # a failing job must not leak out of the helper thread
                pass

        with self.monkeypatch.context() as mp:
            mp.setattr(time, "sleep", short_sleep)
            mp.setattr(threading.Event, "wait", short_wait)
            runner = threading.Thread(target=body_, name=runner_name, daemon=True)
            self.real_start(runner)
            runner.join(timeout)
            return not runner.is_alive()


def ready_web(make_web, rows: Optional[Sequence[Mapping[str, str]]] = None, *, env: Optional[Mapping[str, str]] = None,
              approve: Optional[Sequence[str]] = None, verify: Optional[Sequence[str]] = None,
              sender_email: Optional[str] = SENDER_EMAIL, recipient: Optional[str] = TEST_INBOX) -> Web:
    """A workspace ready to send: contacts imported, engine drafts written, `approve` (default all ok drafts)
    approved, `verify` (default all) marked email-verified. sender_email/recipient None leaves the setting out."""
    from web_helpers import approve_drafts, mark_verified, read_draft_file, seed_contacts

    values: Dict[str, str] = {}
    if sender_email is not None:
        values["SENDER_EMAIL"] = sender_email
    if recipient is not None:
        values["TEST_RECIPIENT_EMAIL"] = recipient
    values.update(env or {})
    chosen = list(STANDARD_ROWS if rows is None else rows)
    w = make_web(values, sender=True)
    seed_contacts(w, chosen)
    w.post(w.url("drafts_write"), {"provider": "template"}, follow=True)
    ok = [r["id"] for r in chosen
          if read_draft_file(w.ws, r["id"]) is not None and read_draft_file(w.ws, r["id"]).status == "ok"]
    approve_drafts(w, ok if approve is None else [s for s in approve if s in ok])
    mark_verified(w, [r["email"] for r in chosen] if verify is None else list(verify))
    return w


def utc_iso(offset_seconds: float) -> str:
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def schedule_form(slugs_ids: Sequence[int], when: dt.datetime, mode: str, phrase: str) -> Dict[str, Any]:
    """The schedule form as the page posts it. `when` is local wall-clock time (the form's date and time inputs)."""
    return {
        "draft_ids": [str(i) for i in slugs_ids],
        "sched_date": when.strftime("%Y-%m-%d"),
        "sched_time": when.strftime("%H:%M:%S"),
        "sched_mode": mode,
        "confirm_text": phrase,
        **{k: phrase for k in typed(phrase)},
    }


def plan_rows(w: Web, path: str = "/crm/send-plan") -> List[Dict[str, str]]:
    """The rows of the send plan table as {header text: cell text}."""
    from html.parser import HTMLParser

    class Table(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.rows: List[List[str]] = []
            self.heads: List[str] = []
            self._cell: Optional[List[str]] = None
            self._row: Optional[List[str]] = None
            self._is_head = False

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag == "tr":
                self._row = []
            elif tag in ("td", "th"):
                self._cell = []
                self._is_head = tag == "th"

        def handle_endtag(self, tag: str) -> None:
            if tag in ("td", "th") and self._cell is not None and self._row is not None:
                text = " ".join("".join(self._cell).split())
                (self.heads if self._is_head else self._row).append(text)
                self._cell = None
            elif tag == "tr" and self._row is not None:
                if self._row:
                    self.rows.append(self._row)
                self._row = None

        def handle_data(self, data: str) -> None:
            if self._cell is not None:
                self._cell.append(data)

    parser = Table()
    parser.feed(body(w.get(path)))
    return [dict(zip(parser.heads, row)) for row in parser.rows]


def column(row: Mapping[str, str], *needles: str) -> str:
    for head, value in row.items():
        if any(n in head.lower() for n in needles):
            return value
    raise AssertionError(f"no column matching {needles} in {list(row)}")


def new_text(before: str, after: str) -> str:
    """Words the second page shows and the first does not: flash messages and inline errors."""
    import difflib

    old, new = before.split(), after.split()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return " ".join(" ".join(new[j1:j2]) for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
                    if tag in ("insert", "replace"))
