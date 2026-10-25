"""View model for the Analytics page ("is my campaign working?").

Pure read-only helpers. Nothing here writes to the database, calls Gmail, or
sends anything. The data layer (vcoutreach.web.crm_analytics) stays the source
of truth for lead scores, cohort funnels and Wilson intervals; this module
only shapes it for the page and adds the date-range KPIs with a
previous-period comparison.

Honesty rules baked in:
- Opens and clicks are estimates ("likely person" requests), flagged `est`.
- A send with no tracking pixel or link is unknown, not zero: it is left out
  of open and click rates, and a metric that cannot be computed is returned as
  `na=True` so the page shows "Not tracked" instead of a fake zero.
- Replies are exact-address incoming mail after the send. Replies that were
  classified as bounce, auto reply or out of office are not counted as replies.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vcoutreach.web.crm_analytics import _dedupe_send_rows, _parse_ts, interpret_event
from vcoutreach.web.db import get_conn

RANGE_OPTIONS = (7, 14, 30)
DEFAULT_RANGE = 14
MIN_SENDS = 20
TAB_ROW_LIMIT = 100

_NOT_REPLIES = {"bounce", "auto_reply", "out_of_office"}

DIMENSIONS = (
    ("lane", "Outreach lane"),
    ("fit_grade", "Fit grade"),
    ("firm_type", "Firm type"),
    ("send_weekday", "Send weekday"),
    ("send_hour_bucket", "Send hour (UTC)"),
    ("cohort_era", "Cohort"),
)


def parse_range(raw) -> int:
    """Return 7, 14 or 30 from a querystring value; anything else is the default."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_RANGE
    return value if value in RANGE_OPTIONS else DEFAULT_RANGE


# -- Raw evidence ---------------------------------------------------------------------------------

def _load_evidence(db_path) -> dict:
    conn = get_conn(db_path)
    try:
        sends = _dedupe_send_rows(conn.execute(
            "SELECT partner_id, batch_key, observed_at FROM crm_send_evidence ORDER BY observed_at"
        ).fetchall())
        tracked = {row["partner_id"] for row in conn.execute("SELECT DISTINCT partner_id FROM crm_tracking_actions")}

        reply_rows = []
        for row in conn.execute(
            "SELECT partner_id, gmail_message_id, received_at, COALESCE(user_label, label) AS eff FROM replies"
        ):
            reply_rows.append({
                "partner_id": row["partner_id"], "message_id": row["gmail_message_id"],
                "at": _parse_ts(row["received_at"]), "label": (row["eff"] or "").strip().lower() or None,
            })
        mail_rows = [(row["partner_id"], row["message_id"], _parse_ts(row["observed_at"]))
                     for row in conn.execute("SELECT partner_id, message_id, observed_at FROM crm_mail_evidence")]

        events = []
        for row in conn.execute(
            "SELECT partner_id, occurred_at, event_type, source_kind, network_class, classification, "
            "reason_codes_json, seconds_since_send FROM crm_tracking_events"
        ):
            record = dict(row)
            try:
                record["reason_codes"] = json.loads(record.pop("reason_codes_json") or "[]")
            except (TypeError, ValueError):
                record["reason_codes"] = []
            kind = interpret_event(record)
            instant = _parse_ts(record["occurred_at"])
            if instant is not None and kind in ("person_open_likely", "person_click_likely", "person_deck_view_likely"):
                events.append((record["partner_id"], instant, "open" if kind == "person_open_likely" else "click"))
    finally:
        conn.close()

    not_reply_ids = {r["message_id"] for r in reply_rows if r["label"] in _NOT_REPLIES}
    reply_at: dict[int, list[datetime]] = {}
    for pid, message_id, instant in mail_rows:
        if instant is not None and message_id not in not_reply_ids:
            reply_at.setdefault(pid, []).append(instant)
    interested_at: dict[int, list[datetime]] = {}
    bounced_at: dict[int, list[datetime]] = {}
    for r in reply_rows:
        if r["partner_id"] is None or r["at"] is None:
            continue
        if r["label"] not in _NOT_REPLIES:
            reply_at.setdefault(r["partner_id"], []).append(r["at"])
        if r["label"] == "interested":
            interested_at.setdefault(r["partner_id"], []).append(r["at"])
        elif r["label"] == "bounce":
            bounced_at.setdefault(r["partner_id"], []).append(r["at"])

    events_by_partner: dict[int, list[tuple]] = {}
    for pid, instant, kind in events:
        events_by_partner.setdefault(pid, []).append((instant, kind))

    parsed_sends = []
    for row in sends:
        instant = _parse_ts(row["observed_at"])
        if instant is not None:
            parsed_sends.append((row["partner_id"], instant))
    return {
        "sends": parsed_sends, "tracked": tracked, "reply_at": reply_at, "interested_at": interested_at,
        "bounced_at": bounced_at, "events": events_by_partner,
        "classified": any(r["label"] for r in reply_rows),
    }


def _after(instants: list[datetime] | None, moment: datetime) -> bool:
    return any(instant > moment for instant in (instants or ()))


def _period(evidence: dict, start: datetime, end: datetime) -> dict:
    sent = replied = tracked_sends = opened = clicked = interested = bounced = 0
    for pid, moment in evidence["sends"]:
        if not (start <= moment < end):
            continue
        sent += 1
        replied += int(_after(evidence["reply_at"].get(pid), moment))
        interested += int(_after(evidence["interested_at"].get(pid), moment))
        bounced += int(_after(evidence["bounced_at"].get(pid), moment))
        if pid in evidence["tracked"]:
            tracked_sends += 1
            signals = [(t, k) for t, k in evidence["events"].get(pid, ()) if t >= moment]
            opened += int(any(k == "open" for _, k in signals))
            clicked += int(any(k == "click" for _, k in signals))
    return {"sent": sent, "replied": replied, "tracked_sends": tracked_sends, "opened": opened,
            "clicked": clicked, "interested": interested, "bounced": bounced}


# -- KPI shaping ----------------------------------------------------------------------------------

def _pct(part: int, whole: int) -> float | None:
    return (part / whole * 100.0) if whole else None


def _delta(current, previous, *, kind: str, days: int, higher_is_better: bool = True) -> dict | None:
    """kind is 'count' (relative to last period, absolute change) or 'rate' (percentage points)."""
    if current is None or previous is None:
        return None
    change = current - previous
    if kind == "rate":
        if abs(change) < 0.05:
            text, direction = f"no change vs previous {days} days", "flat"
        else:
            text = f"{abs(change):.1f} pts vs previous {days} days"
            direction = "up" if change > 0 else "down"
    else:
        if change == 0:
            text, direction = f"no change vs previous {days} days", "flat"
        else:
            text = f"{abs(int(change))} {'more' if change > 0 else 'fewer'} than previous {days} days"
            direction = "up" if change > 0 else "down"
    if direction == "flat":
        tone = "neutral"
    else:
        good = (direction == "up") == higher_is_better
        tone = "good" if good else "bad"
    return {"text": text, "direction": direction, "tone": tone}


def _rate_text(value: float | None) -> str:
    return "No sends" if value is None else f"{value:.1f}%"


def build_kpis(cur: dict, prev: dict, classified: bool, days: int) -> list[dict]:
    sent_delta = _delta(cur["sent"], prev["sent"], kind="count", days=days) if prev["sent"] else None

    open_rate = _pct(cur["opened"], cur["tracked_sends"])
    prev_open = _pct(prev["opened"], prev["tracked_sends"])
    reply_rate = _pct(cur["replied"], cur["sent"])
    prev_reply = _pct(prev["replied"], prev["sent"])

    kpis = [{
        "key": "sent", "label": "Sent", "value": f"{cur['sent']:,}", "na": False, "est": False,
        "caption": f"Emails sent in the last {days} days", "delta": sent_delta,
    }]
    if open_rate is None:
        kpis.append({"key": "open_rate", "label": "Open rate", "value": "Not tracked", "na": True, "est": True,
                     "caption": "No emails in this period carried an open or click tracker", "delta": None})
    else:
        kpis.append({"key": "open_rate", "label": "Open rate", "value": f"{open_rate:.1f}%", "na": False, "est": True,
                     "caption": f"{cur['opened']} of {cur['tracked_sends']} tracked emails look opened by a person",
                     "delta": _delta(open_rate, prev_open, kind="rate", days=days)})
    if reply_rate is None:
        kpis.append({"key": "reply_rate", "label": "Reply rate", "value": "No sends", "na": True, "est": False,
                     "caption": f"Nothing was sent in the last {days} days", "delta": None})
    else:
        kpis.append({"key": "reply_rate", "label": "Reply rate", "value": f"{reply_rate:.1f}%", "na": False, "est": False,
                     "caption": f"{cur['replied']} of {cur['sent']} contacts wrote back",
                     "delta": _delta(reply_rate, prev_reply, kind="rate", days=days)})
    if not classified:
        for key, label in (("interested", "Interested replies"), ("bounced", "Bounced")):
            kpis.append({"key": key, "label": label, "value": "Not tracked", "na": True, "est": False,
                         "caption": "Replies have not been sorted yet. Open Replies to classify them.", "delta": None})
    else:
        kpis.append({"key": "interested", "label": "Interested replies", "value": f"{cur['interested']:,}", "na": False, "est": False,
                     "caption": "Replies sorted as interested", "delta": _delta(cur["interested"], prev["interested"], kind="count", days=days) if prev["sent"] else None})
        kpis.append({"key": "bounced", "label": "Bounced", "value": f"{cur['bounced']:,}", "na": False, "est": False,
                     "caption": "Emails that could not be delivered",
                     "delta": _delta(cur["bounced"], prev["bounced"], kind="count", days=days, higher_is_better=False) if prev["sent"] else None})
    return kpis


def build_funnel(cur: dict, classified: bool) -> list[dict]:
    sent, tracked = cur["sent"], cur["tracked_sends"]

    def width(value: int) -> float:
        return round(min(100.0, value / sent * 100.0), 1) if sent else 0.0

    def stage(label, value, *, est=False, note="", pct_of=None, available=True):
        if not available:
            return {"label": label, "value": None, "text": "Not tracked", "width": 0.0, "pct": "", "est": est, "note": note}
        pct = ""
        denominator = sent if pct_of is None else pct_of
        if denominator:
            pct = f"{value / denominator * 100:.0f}% of {denominator:,}" + (" tracked" if pct_of is not None else "")
        return {"label": label, "value": value, "text": f"{value:,}", "width": width(value), "pct": pct, "est": est, "note": note}

    return [
        stage("Sent", sent),
        stage("Opened", cur["opened"], est=True, pct_of=tracked, available=bool(tracked),
              note="Only emails with a tracker are counted"),
        stage("Clicked or deck viewed", cur["clicked"], est=True, pct_of=tracked, available=bool(tracked),
              note="Only emails with a tracker are counted"),
        stage("Replied", cur["replied"]),
        stage("Interested", cur["interested"], available=classified, note="Needs replies to be sorted"),
    ]


# -- Contact tabs ---------------------------------------------------------------------------------

_TABS = (
    ("hot", "Hot", "Likely-person click or deck view in the last 7 days."),
    ("warm", "Warm", "Some likely-person activity. Worth a follow-up."),
    ("engaged", "Engaged", "Opened or clicked in the last 30 days but no reply."),
    ("replied", "Replied", "Wrote back after your latest email. Answer in the thread."),
)


def build_tabs(buckets: dict) -> dict:
    tabs = []
    for key, label, hint in _TABS:
        rows = buckets.get(key) or []
        tabs.append({
            "key": key, "label": label, "hint": hint, "count": len(rows), "more": max(0, len(rows) - TAB_ROW_LIMIT),
            "rows": [{
                "partner_id": row["partner_id"], "name": row.get("name") or "Unknown contact",
                "firm": row.get("firm") or "", "why": "; ".join(row.get("reasons") or []),
                "last_signal_at": row.get("last_signal_at"), "latest_send_at": row.get("latest_send_at"),
                "next_step": row.get("next_step") or "",
            } for row in rows[:TAB_ROW_LIMIT]],
        })
    default = next((t["key"] for t in tabs if t["count"]), tabs[0]["key"])
    return {"tabs": tabs, "default": default}


# -- What gets replies ----------------------------------------------------------------------------

def _wilson_text(row: dict) -> str:
    return f"{row['wilson_low'] * 100:.1f}% to {row['wilson_high'] * 100:.1f}%"


def build_dimensions(segments: dict) -> dict:
    dims = []
    for key, label in DIMENSIONS:
        raw = segments.get(key) or []
        rows = []
        best = max((r["rate"] for r in raw), default=0.0) or 0.0
        for r in raw:
            rows.append({
                "value": str(r["value"]).replace("_", " "), "n": r["n"], "replies": r["replies"],
                "rate": f"{r['rate'] * 100:.1f}%", "rate_value": r["rate"],
                "width": round(r["rate"] / best * 100.0, 1) if best else 0.0,
                "wilson": _wilson_text(r), "few": r["n"] < MIN_SENDS,
            })
        rows.sort(key=lambda r: (r["few"], -r["rate_value"], -r["n"], r["value"]))
        dims.append({"key": key, "label": label, "rows": rows})
    default = next((d["key"] for d in dims if d["rows"]), DIMENSIONS[0][0])
    return {"dims": dims, "default": default}


# -- Tracking card --------------------------------------------------------------------------------

def build_tracking(coverage: dict, snapshot: dict | None) -> dict:
    recent = coverage.get("recent_sent_count") or 0
    untracked = coverage.get("recent_untracked_count") or 0
    out = {
        "recent_sent": recent, "recent_untracked": untracked, "recent_tracked": max(0, recent - untracked),
        "untracked_share": (round((coverage.get("recent_untracked_share") or 0) * 100) if recent else None),
        "snapshot": None,
    }
    if snapshot:
        events = snapshot.get("event_type_counts") or {}
        out["snapshot"] = {
            "as_of": snapshot.get("as_of"), "tracked": snapshot.get("sent_action_count", 0),
            "seen": snapshot.get("observed_unique_action_count", 0),
            "person": snapshot.get("human_likely_click_unique_action_count", 0),
            "stale": bool(snapshot.get("stale")), "events": events,
        }
    return out


# -- Entry point ----------------------------------------------------------------------------------

def build_analytics_view(db_path: Path | str | None, raw_range, now: datetime, *, buckets: dict, segments: dict,
                         coverage: dict, snapshot: dict | None) -> dict:
    days = parse_range(raw_range)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    evidence = _load_evidence(db_path)
    window = timedelta(days=days)
    cur = _period(evidence, now - window, now + timedelta(seconds=1))
    prev = _period(evidence, now - 2 * window, now - window)
    classified = evidence["classified"]
    return {
        "range_days": days,
        "range_options": RANGE_OPTIONS,
        "kpis": build_kpis(cur, prev, classified, days),
        "funnel": build_funnel(cur, classified),
        "has_sends": bool(cur["sent"]),
        "has_any_sends": bool(evidence["sends"]),
        "contact_tabs": build_tabs(buckets),
        "reply_dims": build_dimensions(segments),
        "min_sends": MIN_SENDS,
        "tracking": build_tracking(coverage, snapshot),
    }
