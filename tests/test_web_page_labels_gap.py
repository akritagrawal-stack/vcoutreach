"""Page level checks for labels, UTC notes and ordering, rendered through the Flask test client.

No dash characters in this file.
"""
from __future__ import annotations

import json
import re
import sqlite3

from web_helpers import *  # noqa: F401,F403
from web_helpers import STANDARD_ROWS, body, go_live, page_text, send_batch


def _db(w):
    return sqlite3.connect(str(w.ws / "data" / "outreach.db"))


def _enrichment_markup(send_web):
    w = send_web(STANDARD_ROWS)
    r = w.get("/enrichment", follow_redirects=True)
    assert r.status_code == 200
    return w, body(r)


def test_enrichment_first_filter_control_has_action_zero(send_web):
    _, markup = _enrichment_markup(send_web)
    m = re.search(r'(?is)<button\b[^>]*>\s*Has PB API\s*</button>', markup)
    assert m, "the Has PB API filter control must be shown"
    assert 'data-action="enrichment_status-0"' in m.group(0)


def test_enrichment_shows_missing_linkedin_filter(send_web):
    _, markup = _enrichment_markup(send_web)
    assert re.search(r'(?is)<button\b[^>]*>\s*Missing LinkedIn\s*</button>', markup)


def test_enrichment_hook_chip_reads_cite_specific_not_cite(send_web):
    _, markup = _enrichment_markup(send_web)
    m = re.search(r'(?is)<button\b[^>]*class="[^"]*\bchip-hq-cite\b[^"]*"[^>]*>(.*?)</button>', markup) \
        or re.search(r'(?is)<button\b[^>]*id="chip-hq-cite"[^>]*>(.*?)</button>', markup)
    assert m, "the cite hook filter must be shown"
    assert page_text(m.group(1)).strip() == "Cite-specific"


def test_enrichment_cite_chip_exists_with_chip_class_and_hq_cite_id(send_web):
    _, markup = _enrichment_markup(send_web)
    found = False
    for m in re.finditer(r'(?is)<[a-z]+\b([^>]*)>', markup):
        attrs = m.group(1)
        cls = re.search(r'\bclass="([^"]*)"', attrs)
        ident = re.search(r'\bid="([^"]*)"', attrs)
        if ident and ident.group(1) == "chip-hq-cite" and cls and "chip" in cls.group(1).split():
            found = True
    assert found, "the cite hook control must be a chip with id chip-hq-cite"


def test_enrichment_rows_list_weakest_first_by_total_facts(send_web):
    w = send_web(STANDARD_ROWS)
    c = _db(w)
    try:
        ids = {r[0]: r[1] for r in c.execute("SELECT partner_name, id FROM partners")}
        seeded = {"Ada Lovelace": 5, "Linus Pauling": 2, "Grace Hopper": 0}
        for name, n in seeded.items():
            if n:
                c.execute("INSERT INTO enrichment (partner_id, source, url, facts_json) VALUES (?, 'linkedin', '', ?)",
                          (ids[name], json.dumps([{"text": f"f{i}"} for i in range(n)])))
        c.commit()
    finally:
        c.close()
    text = page_text(body(w.get("/enrichment", follow_redirects=True)))
    pos = {n: text.index(n) for n in seeded}
    order = sorted(seeded, key=lambda n: pos[n])
    assert order == ["Grace Hopper", "Linus Pauling", "Ada Lovelace"], order


def test_tracking_window_note_says_utc(web):
    snap = {"starts_at": "2026-01-01T00:00:00Z", "ends_at": "2026-01-31T00:00:00Z", "as_of": "2026-01-31T12:30:00Z",
            "stale": False, "sent_action_count": 1, "observed_unique_action_count": 1,
            "human_likely_click_unique_action_count": 0,
            "event_type_counts": {k: {"automation_likely": 0, "unknown": 0, "human_likely": 0} for k in ("open", "click")}}
    (web.ws / "data" / "tracking-snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    text = page_text(body(web.get("/crm/tracking", follow_redirects=True)))
    assert "Window: 2026-01-01 00:00 to 2026-01-31 00:00 UTC." in text
    assert "Read 2026-01-31 12:30 UTC." in text


def test_analytics_send_hour_label_names_utc(send_web):
    w = send_web(STANDARD_ROWS)
    empty = page_text(body(w.get("/crm/analytics", follow_redirects=True)))
    assert "No sends yet" in empty
    go_live(w)
    send_batch(w, "SEND BATCH OF 3")
    assert len(w.transport.calls) == 3, "setup: three live sends"
    text = page_text(body(w.get("/crm/analytics", follow_redirects=True)))
    assert "Send hour (UTC)" in text


def test_reply_detail_dates_carry_utc_suffix(send_web):
    w = send_web(STANDARD_ROWS)
    c = _db(w)
    try:
        c.execute("INSERT INTO replies (gmail_thread_id, gmail_message_id, from_email, from_name, subject, body_text, "
                  "received_at, discovered_at) VALUES ('t1', 'm1', 'ada@example.com', 'Ada Lovelace', 'Re: hi', 'thanks', "
                  "'2026-03-04T05:06:07Z', '2026-03-04T05:07:00Z')")
        c.commit()
        rid = c.execute("SELECT id FROM replies").fetchone()[0]
    finally:
        c.close()
    r = w.get(f"/replies/{rid}", follow_redirects=True)
    assert r.status_code == 200
    assert "2026-03-04 05:06 UTC" in page_text(body(r))


def test_partners_header_says_dropped_during_cleanup(send_web):
    w = send_web(STANDARD_ROWS)
    c = _db(w)
    try:
        c.execute("UPDATE partners SET verification_status='dropped', dropped_reason='bad domain' WHERE slug='linus'")
        c.commit()
    finally:
        c.close()
    text = page_text(body(w.get("/partners", follow_redirects=True)))
    assert re.search(r"1\s+dropped during cleanup", text), text[:600]
