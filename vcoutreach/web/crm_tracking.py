"""Read the existing Cloudflare tracking aggregate for the local CRM.

The Worker deliberately returns only campaign-wide counts. This adapter never
turns an event into a contact-level open or click claim.
"""
from __future__ import annotations

import json
import re
import os
from urllib.parse import urlsplit
from vcoutreach.web.vendor import workspace_path, setting_value, validated_url, credential_value, fixed_urlopen as urlopen
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from vcoutreach.web.db import get_conn
from vcoutreach.web.vendor import email_identity_hash

# Empty until the user configures a Worker in Settings or their workspace .env.
TRACKING_ORIGIN = ""
_CLASSES = ("automation_likely", "unknown", "human_likely")
_EVENT_TYPES = ("open", "click", "deck_view")
_SOURCE_KINDS = ("gmail_proxy", "safe_links", "proofpoint", "generic_scanner", "direct_browser", "unknown")
_NETWORK_CLASSES = ("security", "hosting", "mobile", "residential", "unknown")
_ACTION_ID = re.compile(r"action_[0-9a-f]{32}\Z")
from vcoutreach.web.config import WorkspacePath
_PRIVATE_ROOT = WorkspacePath("outputs")


class TrackingUnavailable(RuntimeError):
    """The aggregate could not be read; zero must not be implied."""


class EventsEndpointMissing(TrackingUnavailable):
    """The per-action events endpoint is not deployed yet (404). Callers must
    degrade to the existing counts endpoint rather than treat this as zero."""


def _dashboard_secret() -> str:
    value = _tracking_settings().get("tracking_worker_token") or setting_value("TRACKING_WORKER_TOKEN") or setting_value("CLOUDFLARE_TRACKING_TOKEN") or setting_value("TRACKING_API_TOKEN")
    if not value:
        raise TrackingUnavailable("Configure a tracking Worker token in Settings or TRACKING_WORKER_TOKEN")
    try:
        return credential_value(value)
    except ValueError as exc:
        raise TrackingUnavailable("Tracking Worker token is invalid") from exc


def _tracking_settings() -> dict:
    try:
        from vcoutreach.web.server.workspace_settings import load_settings
        settings = load_settings()
        return settings if isinstance(settings, dict) else {}
    except (ImportError, OSError, ValueError):
        return {}


def _tracking_origin() -> str:
    settings = _tracking_settings()
    value = (settings.get("tracking_worker_url") or settings.get("tracking_url") or setting_value("TRACKING_WORKER_URL") or setting_value("CLOUDFLARE_TRACKING_URL") or setting_value("TRACKER_PUBLIC_URL") or setting_value("TRACKING_ORIGIN") or TRACKING_ORIGIN)
    if not str(value or "").strip():
        raise TrackingUnavailable("Configure a tracking Worker URL in Settings")
    try:
        return validated_url(value, origin_only=True)
    except ValueError as exc:
        raise TrackingUnavailable("Configure a valid HTTPS tracking Worker URL") from exc


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TrackingUnavailable("Cloudflare returned an invalid tracking snapshot")
    return value


def fetch_tracking_snapshot() -> dict:
    """Fetch the protected, aggregate-only 30-day production snapshot."""
    request = Request(
        _tracking_origin() + "/api/dashboard",
        headers={"Authorization": "Bearer " + _dashboard_secret(), "Accept": "application/json",
                 "User-Agent": "outreach-signal.local/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise TrackingUnavailable("Cloudflare tracking is temporarily unavailable")
            raw = response.read(16385)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise TrackingUnavailable("Cloudflare tracking is temporarily unavailable") from exc
    if len(raw) > 16384:
        raise TrackingUnavailable("Cloudflare returned an invalid tracking snapshot")
    try:
        payload = json.loads(raw)
        if payload.get("classification") != "aggregate_only":
            raise ValueError("wrong scope")
        window = payload["window"]
        freshness = payload["freshness"]
        by_type = payload["event_type_counts"]
        by_class = payload["classification_counts"]
        if not all(isinstance(window[key], str) for key in ("starts_at", "ends_at")):
            raise ValueError("window missing")
        if not isinstance(freshness["as_of"], str) or type(freshness["stale"]) is not bool:
            raise ValueError("freshness missing")
        result = {
            "starts_at": window["starts_at"],
            "ends_at": window["ends_at"],
            "as_of": freshness["as_of"],
            "latest_event_at": freshness.get("latest_event_at"),
            "stale": freshness["stale"],
            "sent_action_count": _count(payload["sent_action_count"]),
            "observed_unique_action_count": _count(payload["observed_unique_action_count"]),
            "human_likely_click_unique_action_count": _count(payload["human_likely_click_unique_action_count"]),
            "classification_counts": {key: _count(by_class[key]) for key in _CLASSES},
            "event_type_counts": {
                kind: {key: _count(by_type[kind][key]) for key in _CLASSES}
                for kind in ("open", "click")
            },
        }
        if result["observed_unique_action_count"] > result["sent_action_count"]:
            raise ValueError("observed exceeds active")
        return result
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise TrackingUnavailable("Cloudflare returned an invalid tracking snapshot") from exc


def activated_actions_for_email(email: str, root: Path = _PRIVATE_ROOT) -> tuple[str, ...]:
    """Join local attested activation journals to one contact without exporting PII."""
    target = email_identity_hash(email)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    actions: set[str] = set()
    if not root.is_dir():
        return ()
    for path in root.glob("**/*.activation.json"):
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > 16384:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("status") != "tracking_activation_success"
                    or record.get("recipient_email_hash") != target):
                continue
            action_id = record.get("tracking_action_id")
            executed_at = datetime.fromisoformat(record["executed_at"].replace("Z", "+00:00"))
            if (_ACTION_ID.fullmatch(action_id or "") and executed_at.tzinfo is not None
                    and executed_at >= cutoff):
                actions.add(action_id)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return tuple(sorted(actions))


def fetch_action_snapshot(action_id: str) -> dict:
    """Read classified requests for one opaque action from the protected Worker."""
    if not _ACTION_ID.fullmatch(action_id):
        raise TrackingUnavailable("Invalid tracking action")
    request = Request(
        _tracking_origin() + "/api/actions/" + action_id,
        headers={"Authorization": "Bearer " + _dashboard_secret(), "Accept": "application/json",
                 "User-Agent": "outreach-signal.local/1.0"}, method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise TrackingUnavailable("Contact tracking is unavailable")
            raw = response.read(8193)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise TrackingUnavailable("Contact tracking is unavailable") from exc
    if len(raw) > 8192:
        raise TrackingUnavailable("Cloudflare returned an invalid contact snapshot")
    try:
        payload = json.loads(raw)
        if payload.get("classification") != "action_only" or payload.get("action_id") != action_id:
            raise ValueError("wrong action")
        by_type = payload["event_type_counts"]
        counts = {kind: {key: _count(by_type[kind][key]) for key in _CLASSES}
                  for kind in ("open", "click")}
        return {"counts": counts, "latest_event_at": payload.get("latest_event_at")}
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise TrackingUnavailable("Cloudflare returned an invalid contact snapshot") from exc


def fetch_contact_tracking(email: str) -> dict:
    """Keep an untracked send distinct from zero observed requests."""
    actions = activated_actions_for_email(email)
    if not actions:
        return {"status": "not_instrumented", "action_count": 0}
    if len(actions) > 10:
        return {"status": "unavailable", "reason": "Too many local tracking actions"}
    totals = {kind: {key: 0 for key in _CLASSES} for kind in ("open", "click")}
    try:
        for action_id in actions:
            snapshot = fetch_action_snapshot(action_id)
            for kind in totals:
                for key in _CLASSES:
                    totals[kind][key] += snapshot["counts"][kind][key]
    except TrackingUnavailable as exc:
        return {"status": "unavailable", "action_count": len(actions), "reason": str(exc)}
    return {"status": "tracked", "action_count": len(actions), "event_type_counts": totals}


def _validate_event(item: object) -> dict:
    """Strictly validate one classified request event from the Worker.

    Never trusts field presence or type; an event that fails validation makes
    the whole response invalid rather than being silently dropped or zeroed.
    """
    if not isinstance(item, dict):
        raise ValueError("event must be an object")
    occurred_at = item.get("occurred_at")
    event_type = item.get("event_type")
    source_kind = item.get("source_kind")
    classification = item.get("classification")
    network_class = item.get("network_class", "unknown")
    user_agent_family = item.get("user_agent_family")
    reason_codes = item.get("reason_codes", [])
    seconds_since_send = item.get("seconds_since_send")
    if not isinstance(occurred_at, str) or not occurred_at:
        raise ValueError("occurred_at is invalid")
    if event_type not in _EVENT_TYPES:
        raise ValueError("event_type is invalid")
    if source_kind not in _SOURCE_KINDS:
        raise ValueError("source_kind is invalid")
    if classification not in _CLASSES:
        raise ValueError("classification is invalid")
    if network_class not in _NETWORK_CLASSES:
        raise ValueError("network_class is invalid")
    if user_agent_family is not None and not isinstance(user_agent_family, str):
        raise ValueError("user_agent_family is invalid")
    if not isinstance(reason_codes, list) or not all(isinstance(code, str) for code in reason_codes):
        raise ValueError("reason_codes is invalid")
    if seconds_since_send is not None and (type(seconds_since_send) is not int or seconds_since_send < 0):
        raise ValueError("seconds_since_send is invalid")
    return {
        "occurred_at": occurred_at,
        "event_type": event_type,
        "source_kind": source_kind,
        "classification": classification,
        "network_class": network_class,
        "user_agent_family": user_agent_family,
        "reason_codes": reason_codes,
        "seconds_since_send": seconds_since_send,
    }


def fetch_action_events(action_id: str) -> dict:
    """Read classified request events for one opaque action from the Worker.

    The events endpoint is a parallel-agent addition that is not deployed yet.
    A 404 raises EventsEndpointMissing so callers can fall back to the existing
    counts endpoint instead of rendering it as zero events.
    """
    if not _ACTION_ID.fullmatch(action_id):
        raise TrackingUnavailable("Invalid tracking action")
    request = Request(
        _tracking_origin() + "/api/actions/" + action_id + "/events",
        headers={"Authorization": "Bearer " + _dashboard_secret(), "Accept": "application/json",
                 "User-Agent": "outreach-signal.local/1.0"}, method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status == 404:
                raise EventsEndpointMissing("Tracking events endpoint is not deployed yet")
            if response.status != 200:
                raise TrackingUnavailable("Contact tracking events are unavailable")
            raw = response.read(32769)
    except HTTPError as exc:
        if exc.code == 404:
            raise EventsEndpointMissing("Tracking events endpoint is not deployed yet") from exc
        raise TrackingUnavailable("Contact tracking events are unavailable") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise TrackingUnavailable("Contact tracking events are unavailable") from exc
    if len(raw) > 32768:
        raise TrackingUnavailable("Cloudflare returned an invalid events payload")
    try:
        payload = json.loads(raw)
        if payload.get("classification") != "action_events" or payload.get("action_id") != action_id:
            raise ValueError("wrong action")
        events_raw = payload["events"]
        if not isinstance(events_raw, list):
            raise ValueError("events must be a list")
        events = [_validate_event(item) for item in events_raw]
        activated_at = payload.get("activated_at")
        if activated_at is not None and not isinstance(activated_at, str):
            raise ValueError("activated_at is invalid")
        truncated = payload.get("truncated", False)
        if type(truncated) is not bool:
            raise ValueError("truncated is invalid")
        return {"action_id": action_id, "activated_at": activated_at, "events": events, "truncated": truncated}
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise TrackingUnavailable("Cloudflare returned an invalid events payload") from exc


def action_partner_map(db_path: Path | None = None) -> dict[str, dict]:
    """Join workspace activation journals to local contacts by exact email hash."""
    conn = get_conn(db_path)
    try:
        partner_rows = conn.execute(
            "SELECT id, email FROM partners WHERE email IS NOT NULL AND email != ''"
        ).fetchall()
    finally:
        conn.close()
    hash_to_partner: dict[str, int] = {}
    for row in partner_rows:
        try:
            hash_to_partner[email_identity_hash(row["email"])] = row["id"]
        except Exception:
            continue

    mapping: dict[str, dict] = {}
    if _PRIVATE_ROOT.is_dir():
        for path in _PRIVATE_ROOT.glob("**/*.activation.json"):
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(_PRIVATE_ROOT.resolve()) or path.stat().st_size > 16384:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("status") != "tracking_activation_success":
                    continue
                action_id = record.get("tracking_action_id")
                recipient_hash = record.get("recipient_email_hash")
                if not _ACTION_ID.fullmatch(action_id or "") or recipient_hash not in hash_to_partner:
                    continue
                mapping[action_id] = {
                    "partner_id": hash_to_partner[recipient_hash],
                    "activated_at": record.get("executed_at"),
                    "batch_key": None,
                }
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue

    return mapping
