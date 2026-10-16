"""What is working, what to improve, and which leads are hot.

Reads crm_send_evidence, crm_mail_evidence, crm_profiles, and the new
crm_tracking_events / crm_tracking_actions tables (migration 029). Never
writes to any table this module does not own, never calls Gmail or any send
path, and never claims a person read an email when the Worker only reports a
classified request. "Untracked" and "zero observed events" are always kept
distinct in every function here.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vcoutreach.web.crm_tracking import EventsEndpointMissing, TrackingUnavailable, action_partner_map, fetch_action_events
from vcoutreach.web.db import get_conn

_HALF_LIFE_DAYS = 3.0
_SHA_SUFFIX = re.compile(r":sha256:[0-9a-f]{16,}$")
_FOLLOWUP_BATCH = re.compile(r"^outreach-[a-z0-9-]+-followup-(\d{8})$")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z") if value else None


def interpret_event(event: dict) -> str:
    """Classify one stored/fetched tracking event as a person signal or automation.

    Clicks and deck views trust the Worker's own classification (human_likely
    counts as a person signal). The Worker never marks an open human, so an
    open is judged locally: a Gmail-proxy fetch reached at least five minutes
    after send (Gmail loads images when a person opens the message, not at
    send time) or a direct-browser open on a residential/mobile network after
    the same five minutes. Everything inside that five-minute window, every
    scanner or link-rewriter hit, and every hosting/security-network request
    is automation.
    """
    event_type = event.get("event_type")
    classification = event.get("classification")
    source_kind = event.get("source_kind")
    network_class = event.get("network_class")
    seconds_since_send = event.get("seconds_since_send")
    reason_codes = event.get("reason_codes") or []

    automation_reasons = {"machine_cadence", "link_fanout", "security_scanner", "gmail_proxy",
                          "automation_user_agent_family", "automation_network_class",
                          "post_send_latency_under_180_seconds"}
    if event_type == "click":
        if classification == "human_likely":
            return "person_click_likely"
        # The Worker only marks residential/mobile clicks human; most ISPs are "unknown".
        # A real browser, off datacenter/security networks, 3+ minutes after send,
        # with no automation reason, is treated as a likely person click.
        if (classification == "unknown" and source_kind == "direct_browser"
                and network_class not in ("hosting", "security")
                and seconds_since_send is not None and seconds_since_send >= 180
                and not automation_reasons & set(reason_codes)):
            return "person_click_likely"
        if classification == "unknown":
            return "unknown"
        return "automation"
    if event_type == "deck_view":
        if classification == "human_likely":
            return "person_deck_view_likely"
        if classification == "unknown":
            return "unknown"
        return "automation"
    if event_type == "open":
        if seconds_since_send is None or seconds_since_send < 300:
            return "automation"
        if source_kind == "gmail_proxy" and "machine_cadence" not in reason_codes:
            return "person_open_likely"
        if source_kind == "direct_browser" and network_class not in ("hosting", "security"):
            return "person_open_likely"
        # A desktop or phone mail client (no browser token in its user agent) fetching
        # from a residential or mobile network, not a proxy or datacenter.
        if source_kind == "unknown" and network_class in ("residential", "mobile"):
            return "person_open_likely"
        return "automation"
    return "unknown"


# ── Sync ─────────────────────────────────────────────────────────────────────

def sync_tracking_events(db_path: Path | None = None, *, fetch=fetch_action_events) -> dict:
    """Pull events for recently activated, mapped actions into local tables.

    Read-only toward Cloudflare (GET only, never a write). Only actions
    activated in the last 30 days are synced. Never stores IPs or raw user
    agent strings, only the Worker's own coarse family/network labels.
    Returns the per-status count so a caller can flash it and never silently
    swallow a partial failure.
    """
    mapping = action_partner_map(db_path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    status_counts = {"ok": 0, "endpoint_missing": 0, "unavailable": 0}
    now_iso = _iso(datetime.now(timezone.utc))
    conn = get_conn(db_path)
    try:
        for action_id, info in mapping.items():
            activated_at = _parse_ts(info.get("activated_at"))
            if activated_at is not None and activated_at < cutoff:
                continue
            partner_id = info["partner_id"]
            try:
                result = fetch(action_id)
                events = result["events"]
                status = "ok"
            except EventsEndpointMissing:
                events = []
                status = "endpoint_missing"
            except TrackingUnavailable:
                events = []
                status = "unavailable"
            status_counts[status] += 1
            with conn:
                conn.execute(
                    "INSERT INTO crm_tracking_actions(action_id,partner_id,batch_key,activated_at,"
                    "last_synced_at,sync_status) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(action_id) DO UPDATE SET "
                    "partner_id=excluded.partner_id, "
                    "batch_key=COALESCE(excluded.batch_key, crm_tracking_actions.batch_key), "
                    "activated_at=COALESCE(excluded.activated_at, crm_tracking_actions.activated_at), "
                    "last_synced_at=excluded.last_synced_at, sync_status=excluded.sync_status",
                    (action_id, partner_id, info.get("batch_key"), info.get("activated_at"), now_iso, status),
                )
                for event in events:
                    conn.execute(
                        "INSERT INTO crm_tracking_events(action_id,partner_id,occurred_at,event_type,"
                        "source_kind,user_agent_family,network_class,classification,reason_codes_json,"
                        "seconds_since_send,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(action_id,occurred_at,event_type,source_kind) DO NOTHING",
                        (action_id, partner_id, event["occurred_at"], event["event_type"], event["source_kind"],
                         event.get("user_agent_family"), event.get("network_class"), event["classification"],
                         json.dumps(event.get("reason_codes") or []), event.get("seconds_since_send"), now_iso),
                    )
        conn.commit()
    finally:
        conn.close()
    return status_counts


# ── Lead scoring ─────────────────────────────────────────────────────────────

def _decay(age_days: float) -> float:
    return 0.5 ** (age_days / _HALF_LIFE_DAYS)


def _format_recency(age_days: float) -> str:
    hours = age_days * 24
    if hours < 1:
        return "under 1h ago"
    if hours < 24:
        return f"{round(hours)}h ago"
    return f"{round(age_days)}d ago"


def _lead_reasons(deck_views: list[dict], clicks: list[dict], opens: list[dict],
                   distinct_days: set, any_events: bool) -> list[str]:
    reasons = []
    if deck_views:
        reasons.append(f"Viewed deck {len(deck_views)}x, last {_format_recency(deck_views[0]['age_days'])}")
        if len(distinct_days) >= 2:
            reasons.append(f"Deck views across {len(distinct_days)} days")
    if clicks:
        reasons.append(f"Clicked link (likely person) {len(clicks)}x, last {_format_recency(clicks[0]['age_days'])}")
    if opens:
        reasons.append(f"Opened (likely person) {len(opens)}x, last {_format_recency(opens[0]['age_days'])}")
    if not reasons:
        reasons.append("Only scanner/bot activity" if any_events else "No tracked signal recorded")
    return reasons


def _next_step(bucket: str, days_since_send: float | None, replied: bool) -> str:
    if replied:
        return "Reply in the thread, this is a conversation, not a cold follow-up"
    if bucket == "hot":
        return "Follow up now, reference the deck"
    if bucket == "warm":
        return "Follow up on schedule"
    if bucket == "engaged":
        return "Opened or clicked earlier without replying: prioritize in the next follow-up wave"
    if bucket == "untracked":
        if days_since_send is not None and days_since_send >= 6:
            return "Due for follow-up"
        return "No tracking data yet; wait for the normal follow-up window"
    return "No action needed yet"


def lead_scores(db_path: Path | None = None, now: datetime | None = None) -> list[dict]:
    """Score every current contact's engagement and bucket them.

    Excludes dropped/suppressed contacts and stages hold/passed. A contact
    with incoming mail after their latest send is a reply to answer, not a
    lead to chase, so it goes in its own 'replied' bucket. A contact whose
    sends were never instrumented (no mapped tracking action at all) is
    'untracked', which is distinct from 'cold' (instrumented, low signal).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    conn = get_conn(db_path)
    try:
        partner_rows = conn.execute(
            """
            SELECT p.id AS partner_id, p.partner_name AS name, p.firm, p.email,
                   cp.stage AS stage,
                   (SELECT MAX(s.observed_at) FROM crm_send_evidence s WHERE s.partner_id=p.id) AS latest_send_at,
                   (SELECT s.batch_key FROM crm_send_evidence s WHERE s.partner_id=p.id
                    ORDER BY s.observed_at DESC, s.id DESC LIMIT 1) AS latest_send_batch,
                   (SELECT MAX(m.observed_at) FROM crm_mail_evidence m WHERE m.partner_id=p.id) AS latest_mail_at,
                   EXISTS(SELECT 1 FROM crm_tracking_actions a WHERE a.partner_id=p.id) AS has_tracking_action
            FROM partners p LEFT JOIN crm_profiles cp ON cp.partner_id=p.id
            WHERE (p.dropped_reason IS NULL OR p.dropped_reason='')
              AND COALESCE(cp.stage,'') NOT IN ('hold','passed')
            """
        ).fetchall()
        events_by_partner: dict[int, list[dict]] = {}
        for row in conn.execute(
            "SELECT partner_id, occurred_at, event_type, source_kind, network_class, "
            "classification, reason_codes_json, seconds_since_send FROM crm_tracking_events"
        ):
            events_by_partner.setdefault(row["partner_id"], []).append(dict(row))
    finally:
        conn.close()

    results = []
    for row in partner_rows:
        partner_id = row["partner_id"]
        latest_send_at = _parse_ts(row["latest_send_at"])
        latest_mail_at = _parse_ts(row["latest_mail_at"])
        replied = bool(latest_mail_at and latest_send_at and latest_mail_at > latest_send_at)

        deck_views, clicks, opens = [], [], []
        for event in events_by_partner.get(partner_id, []):
            try:
                reason_codes = json.loads(event.get("reason_codes_json") or "[]")
            except (TypeError, ValueError):
                reason_codes = []
            interpreted = interpret_event({**event, "reason_codes": reason_codes})
            occurred = _parse_ts(event["occurred_at"])
            if occurred is None:
                continue
            age_days = max((now - occurred).total_seconds() / 86400, 0.0)
            entry = {"occurred_at": occurred, "age_days": age_days}
            if interpreted == "person_deck_view_likely":
                deck_views.append(entry)
            elif interpreted == "person_click_likely":
                clicks.append(entry)
            elif interpreted == "person_open_likely":
                opens.append(entry)
        deck_views.sort(key=lambda e: e["age_days"])
        clicks.sort(key=lambda e: e["age_days"])
        opens.sort(key=lambda e: e["age_days"])

        score = 0.0
        for entry in deck_views[:3]:
            score += 40 * _decay(entry["age_days"])
        distinct_days = {entry["occurred_at"].date() for entry in deck_views}
        if len(distinct_days) >= 2:
            score += 15 * _decay(deck_views[0]["age_days"])
        for entry in clicks[:2]:
            score += 30 * _decay(entry["age_days"])
        for entry in opens[:3]:
            score += 12 * _decay(entry["age_days"])

        all_signals = deck_views + clicks + opens
        last_signal_at = max((e["occurred_at"] for e in all_signals), default=None)
        recent_signal = bool(last_signal_at and (now - last_signal_at).total_seconds() <= 7 * 86400)

        is_tracked = bool(row["has_tracking_action"])
        if replied:
            bucket = "replied"
        elif not is_tracked:
            bucket = "untracked"
        elif score >= 35 and recent_signal:
            bucket = "hot"
        elif score >= 12:
            bucket = "warm"
        elif last_signal_at and (now - last_signal_at).total_seconds() <= 30 * 86400:
            bucket = "engaged"
        else:
            bucket = "cold"

        any_events = bool(events_by_partner.get(partner_id))
        reasons = _lead_reasons(deck_views, clicks, opens, distinct_days, any_events)
        days_since_send = (now - latest_send_at).total_seconds() / 86400 if latest_send_at else None
        next_step = _next_step(bucket, days_since_send, replied)

        results.append({
            "partner_id": partner_id,
            "name": row["name"],
            "firm": row["firm"],
            "email": row["email"],
            "bucket": bucket,
            "score": round(score, 1),
            "reasons": reasons,
            "last_signal_at": _iso(last_signal_at),
            "latest_send_batch": row["latest_send_batch"],
            "latest_send_at": row["latest_send_at"],
            "days_since_send": round(days_since_send, 1) if days_since_send is not None else None,
            "next_step": next_step,
        })
    results.sort(key=lambda r: (-r["score"], r["name"] or ""))
    return results


# ── Cohort / batch classification shared by funnel + segments ─────────────────

def _base_batch(batch_key: str) -> str:
    """Strip the sha256 dedup suffix some batches (e.g. new100) carry twice."""
    return _SHA_SUFFIX.sub("", batch_key or "")


def _dedupe_send_rows(rows: list) -> list:
    best = {}
    for row in rows:
        record = dict(row)
        key = (record['partner_id'], _base_batch(record['batch_key']))
        previous = best.get(key)
        if previous is None or (record.get('observed_at') or '') < (previous.get('observed_at') or ''):
            best[key] = record
    return list(best.values())


def _cohort_key(batch_key: str, observed_at: str | None) -> str:
    base = _base_batch(batch_key)
    if base == "delivery_receipt":
        date_str = (observed_at or "")[:10] or "unknown-date"
        return f"delivery_receipt:{date_str}"
    if base == "workspace_research":
        return "workspace_research"
    followup_match = _FOLLOWUP_BATCH.match(base)
    if followup_match:
        digits = followup_match.group(1)
        return f"followup:{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if base.startswith("update"):
        return "update"
    return base


def _lane_for_batch(batch_key: str) -> str:
    lowered = (batch_key or "").lower()
    if "followup" in lowered or "follow-up" in lowered:
        return "follow_up"
    if lowered.startswith("warm-update"):
        return "first_touch"
    return "first_touch"


def _era_for_batch(batch_key: str, observed_at: str | None) -> str:
    key = _cohort_key(batch_key, observed_at)
    if key.startswith("delivery_receipt:"):
        month = key.split(":", 1)[1][:7]
        return f"daily_receipt_{month}" if len(month) == 7 else "daily_receipt_unknown"
    if key == "workspace_research":
        return "workspace_research"
    if key.startswith("followup:"):
        return "follow_up"
    if key == "update":
        return "updates"
    return "other"


def _firm_type(firm: str | None, partner_name: str | None) -> str:
    firm_norm = (firm or "").strip().casefold()
    name_norm = (partner_name or "").strip().casefold()
    if "angel" in firm_norm or (firm_norm and firm_norm == name_norm):
        return "angel_or_individual"
    return "institutional"


_HOUR_BUCKETS = (
    (0, 6, "night_00_05_utc"),
    (6, 12, "morning_06_11_utc"),
    (12, 18, "afternoon_12_17_utc"),
    (18, 24, "evening_18_23_utc"),
)


def _hour_bucket(hour: int) -> str:
    for start, end, label in _HOUR_BUCKETS:
        if start <= hour < end:
            return label
    return "unknown"


# ── Funnel ───────────────────────────────────────────────────────────────────

def campaign_funnel(db_path: Path | None = None) -> list[dict]:
    """Per-cohort funnel: sent, tracked share, person signals, replies, outcomes.

    A cohort is a daily delivery_receipt date, a named batch (new100, a
    follow-up day, the warm update, or any other batch as itself). Person
    signal counts are contacts with at least one person-likely event of that
    kind after their send in this cohort, not a raw event count.
    """
    conn = get_conn(db_path)
    try:
        send_rows = _dedupe_send_rows(conn.execute(
            "SELECT partner_id, batch_key, observed_at FROM crm_send_evidence ORDER BY observed_at"
        ).fetchall())
        dropped_partners = {row["id"] for row in conn.execute(
            "SELECT id FROM partners WHERE dropped_reason IS NOT NULL AND dropped_reason != ''"
        )}
        stage_by_partner = {row["partner_id"]: row["stage"] for row in conn.execute(
            "SELECT partner_id, stage FROM crm_profiles"
        )}
        tracked_partners = {row["partner_id"] for row in conn.execute(
            "SELECT DISTINCT partner_id FROM crm_tracking_actions"
        )}
        mail_by_partner: dict[int, list[datetime]] = {}
        for row in conn.execute("SELECT partner_id, observed_at FROM crm_mail_evidence"):
            instant = _parse_ts(row["observed_at"])
            if instant:
                mail_by_partner.setdefault(row["partner_id"], []).append(instant)
        for row in conn.execute('SELECT partner_id, received_at FROM replies WHERE partner_id IS NOT NULL'):
            instant = _parse_ts(row['received_at'])
            if instant:
                mail_by_partner.setdefault(row['partner_id'], []).append(instant)
        batch_names = [row['name'] for row in conn.execute('SELECT name FROM batches ORDER BY id')]
        events_by_partner: dict[int, list[tuple]] = {}
        for row in conn.execute(
            "SELECT partner_id, occurred_at, event_type, source_kind, network_class, classification, "
            "reason_codes_json, seconds_since_send FROM crm_tracking_events"
        ):
            record = dict(row)
            try:
                record["reason_codes"] = json.loads(record.pop("reason_codes_json") or "[]")
            except (TypeError, ValueError):
                record["reason_codes"] = []
            interpreted = interpret_event(record)
            if interpreted in ("person_open_likely", "person_click_likely", "person_deck_view_likely"):
                instant = _parse_ts(record["occurred_at"])
                if instant:
                    events_by_partner.setdefault(record["partner_id"], []).append((instant, interpreted))
    finally:
        conn.close()

    cohorts: dict[str, dict[int, datetime]] = {}
    for row in send_rows:
        key = row["batch_key"] if row["batch_key"] in batch_names else _cohort_key(row["batch_key"], row["observed_at"])
        instant = _parse_ts(row["observed_at"])
        if instant is None:
            continue
        bucket = cohorts.setdefault(key, {})
        if row["partner_id"] not in bucket or instant < bucket[row["partner_id"]]:
            bucket[row["partner_id"]] = instant

    for name in batch_names:
        cohorts.setdefault(name, {})
    results = []
    for cohort_name, partner_sent_at in cohorts.items():
        partner_ids = list(partner_sent_at.keys())
        sent = len(partner_ids)
        tracked = sum(1 for pid in partner_ids if pid in tracked_partners)
        opens = clicks = deck_views = 0
        first_signal_hours = []
        for pid in partner_ids:
            sent_at = partner_sent_at[pid]
            partner_events = [e for e in events_by_partner.get(pid, []) if e[0] >= sent_at]
            kinds = {kind for _, kind in partner_events}
            opens += int("person_open_likely" in kinds)
            clicks += int("person_click_likely" in kinds)
            deck_views += int("person_deck_view_likely" in kinds)
            if partner_events:
                first = min(instant for instant, _ in partner_events)
                first_signal_hours.append((first - sent_at).total_seconds() / 3600)
        replies = sum(1 for pid in partner_ids
                      if any(instant >= partner_sent_at[pid] for instant in mail_by_partner.get(pid, [])))
        suppressed = sum(1 for pid in partner_ids if pid in dropped_partners)
        warm_count = sum(1 for pid in partner_ids if stage_by_partner.get(pid) == "warm")
        passed_count = sum(1 for pid in partner_ids if stage_by_partner.get(pid) == "passed")
        median_hours = None
        if first_signal_hours:
            first_signal_hours.sort()
            n = len(first_signal_hours)
            mid = n // 2
            median_hours = (first_signal_hours[mid] if n % 2
                             else (first_signal_hours[mid - 1] + first_signal_hours[mid]) / 2)
        results.append({
            "cohort": cohort_name,
            "wilson_low": _wilson_interval(replies, sent)[0],
            "wilson_high": _wilson_interval(replies, sent)[1],
            "sent": sent,
            "tracked_share": round(tracked / sent, 3) if sent else None,
            "person_opens": opens,
            "person_clicks": clicks,
            "person_deck_views": deck_views,
            "incoming_replies_after_send": replies,
            "reply_rate": round(replies / sent, 3) if sent else None,
            "suppressed": suppressed,
            "warm_count": warm_count,
            "passed_count": passed_count,
            "median_hours_to_first_person_signal": round(median_hours, 1) if median_hours is not None else None,
        })
    results.sort(key=lambda r: r["cohort"])
    return results


# ── Segment performance (with Wilson intervals) ─────────────────────────────

def _wilson_interval(successes: int, n: int, z: float = 1.959963985) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    low = (center - margin) / denom
    high = (center + margin) / denom
    return (max(0.0, low), min(1.0, high))


def segment_performance(db_path: Path | None = None) -> dict[str, list[dict]]:
    """Reply rate with a Wilson 95% interval, broken out by several dimensions.

    Each send-evidence row (after the same new100 dedup as the funnel) is one
    observation; a reply counts when incoming mail from that contact lands
    after that specific send.
    """
    conn = get_conn(db_path)
    try:
        send_rows = _dedupe_send_rows(conn.execute(
            "SELECT s.partner_id, s.batch_key, s.observed_at, p.firm, p.partner_name, "
            "COALESCE(cp.fit_grade,'unreviewed') AS fit_grade "
            "FROM crm_send_evidence s JOIN partners p ON p.id=s.partner_id "
            "LEFT JOIN crm_profiles cp ON cp.partner_id=s.partner_id"
        ).fetchall())
        mail_by_partner: dict[int, list[datetime]] = {}
        for row in conn.execute("SELECT partner_id, observed_at FROM crm_mail_evidence"):
            instant = _parse_ts(row["observed_at"])
            if instant:
                mail_by_partner.setdefault(row["partner_id"], []).append(instant)
    finally:
        conn.close()

    dimensions: dict[str, dict[str, dict[str, int]]] = {
        "lane": {}, "fit_grade": {}, "firm_type": {}, "send_weekday": {}, "send_hour_bucket": {}, "cohort_era": {},
    }
    for row in send_rows:
        sent_at = _parse_ts(row["observed_at"])
        if sent_at is None:
            continue
        replied = any(instant > sent_at for instant in mail_by_partner.get(row["partner_id"], []))
        values = {
            "lane": _lane_for_batch(row["batch_key"]),
            "fit_grade": row["fit_grade"],
            "firm_type": _firm_type(row["firm"], row["partner_name"]),
            "send_weekday": sent_at.strftime("%A"),
            "send_hour_bucket": _hour_bucket(sent_at.hour),
            "cohort_era": _era_for_batch(row["batch_key"], row["observed_at"]),
        }
        for dimension, value in values.items():
            bucket = dimensions[dimension].setdefault(value, {"n": 0, "replies": 0})
            bucket["n"] += 1
            bucket["replies"] += int(replied)

    output: dict[str, list[dict]] = {}
    for dimension, values in dimensions.items():
        rows = []
        for value, counts in values.items():
            n, replies = counts["n"], counts["replies"]
            rate = replies / n if n else 0.0
            low, high = _wilson_interval(replies, n)
            rows.append({
                "value": value, "n": n, "replies": replies,
                "rate": round(rate, 3), "wilson_low": round(low, 3), "wilson_high": round(high, 3),
            })
        rows.sort(key=lambda r: (-r["n"], r["value"]))
        output[dimension] = rows
    return output


# ── Tracking coverage (data-quality signal) ─────────────────────────────────

def tracking_coverage(db_path: Path | None = None, now: datetime | None = None) -> dict:
    """Share of recently-sent contacts with no mapped tracking action at all.

    A send with no pixel or link (every send from Sept 22 on, by design) is
    unknown, not zero; this is surfaced so the analytics page never implies
    those contacts had no opens when open state simply was not captured.
    """
    now = now or datetime.now(timezone.utc)
    conn = get_conn(db_path)
    try:
        latest_send_by_partner = {row["partner_id"]: row["latest_send_at"] for row in conn.execute(
            "SELECT partner_id, MAX(observed_at) AS latest_send_at FROM crm_send_evidence GROUP BY partner_id"
        )}
        tracked_partners = {row["partner_id"] for row in conn.execute(
            "SELECT DISTINCT partner_id FROM crm_tracking_actions"
        )}
        mail_evidence_count = conn.execute("SELECT COUNT(*) FROM crm_mail_evidence").fetchone()[0]
    finally:
        conn.close()
    cutoff = now - timedelta(days=14)
    recent = [pid for pid, sent_at in latest_send_by_partner.items()
              if (instant := _parse_ts(sent_at)) and instant >= cutoff]
    untracked_recent = [pid for pid in recent if pid not in tracked_partners]
    return {
        "recent_sent_count": len(recent),
        "recent_untracked_count": len(untracked_recent),
        "recent_untracked_share": round(len(untracked_recent) / len(recent), 3) if recent else None,
        "incoming_mail_unclassified_count": mail_evidence_count,
    }


# ── Insights ─────────────────────────────────────────────────────────────────

_DIMENSION_LABELS = (
    ("lane", "outreach lane"),
    ("fit_grade", "fit grade"),
    ("firm_type", "firm type"),
    ("send_weekday", "send weekday"),
    ("send_hour_bucket", "send hour (UTC)"),
    ("cohort_era", "cohort"),
)


def _cite(row: dict) -> str:
    return f"(n={row['n']}, reply rate {row['rate'] * 100:.0f}%, 95% CI {row['wilson_low'] * 100:.0f} to {row['wilson_high'] * 100:.0f}%)"


def insights(funnel: list[dict], segments: dict[str, list[dict]], coverage: dict) -> dict:
    """Turn the numbers into 'what is working' / 'what to improve' bullets.

    Every comparison cites n and its Wilson interval. Anything with fewer
    than 20 sends in a segment, or overlapping intervals between the best and
    worst segment, is reported as 'not enough sends to call' rather than a
    conclusion. Data-quality caveats are always included, not conditional on
    there being a clean story to tell.
    """
    working: list[str] = []
    improve: list[str] = []

    for dimension, label in _DIMENSION_LABELS:
        rows = [row for row in segments.get(dimension, []) if row["n"] >= 20]
        if len(rows) < 2:
            improve.append(f"Not enough sends to call a best {label}: fewer than two segments have 20 or more sends.")
            continue
        best = max(rows, key=lambda r: r["rate"])
        worst = min(rows, key=lambda r: r["rate"])
        if best["value"] == worst["value"]:
            improve.append(f"Not enough sends to call a best {label}: only one segment qualifies.")
        elif best["wilson_low"] > worst["wilson_high"]:
            working.append(
                f"{label.capitalize()} '{best['value']}' replies best {_cite(best)}, "
                f"ahead of '{worst['value']}' {_cite(worst)}."
            )
            improve.append(f"{label.capitalize()} '{worst['value']}' lags {_cite(worst)}; consider cutting or revising it.")
        else:
            improve.append(
                f"Not enough sends to call a best {label}: intervals for '{best['value']}' and "
                f"'{worst['value']}' overlap {_cite(best)} vs {_cite(worst)}."
            )

    if coverage.get("recent_sent_count"):
        share = coverage.get("recent_untracked_share")
        if share is not None:
            improve.append(
                f"{coverage['recent_untracked_count']} of {coverage['recent_sent_count']} contacts sent to in the "
                f"last 14 days ({share * 100:.0f}%) have no tracking pixel or link at all, so open and click state "
                "for them is unknown, not zero."
            )
    improve.append(
        f"{coverage.get('incoming_mail_unclassified_count', 0)} incoming messages are matched by exact sender "
        "address only; content is not classified, so some counted replies may be auto-replies or bounces."
    )

    if not working:
        working.append("Not enough sends with non-overlapping intervals yet to call a clear winner.")
    return {"working": working, "improve": improve}
