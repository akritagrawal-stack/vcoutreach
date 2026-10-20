"""Durable provider-attempt ledger for Apollo and Enrow contact evidence.

The ledger deliberately separates identity discovery from deliverability and
send eligibility. Provider results never unlock the Gmail send gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vcoutreach.web.db import get_conn


PROVIDERS = frozenset({"apollo", "enrow"})
OPERATIONS = frozenset({"email_find", "email_verify", "person_enrich"})
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CONSUMER_EMAIL_DOMAINS = frozenset({
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
})


class ContactProviderStoreError(ValueError):
    """Raised when provider evidence cannot be stored safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def email_hash(email: str) -> str:
    normalized = _validated_email(email)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def record_attempt(
    *,
    partner_id: int,
    provider: str,
    operation: str,
    result: Mapping[str, Any],
    idempotency_key: str,
    observed_at: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Insert one normalized provider result, returning the stored row."""

    provider_value = str(provider or "").strip().lower()
    operation_value = str(operation or "").strip().lower()
    if provider_value not in PROVIDERS:
        raise ContactProviderStoreError("provider_invalid")
    if operation_value not in OPERATIONS:
        raise ContactProviderStoreError("operation_invalid")
    if isinstance(partner_id, bool) or not isinstance(partner_id, int) or partner_id <= 0:
        raise ContactProviderStoreError("partner_id_invalid")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 200:
        raise ContactProviderStoreError("idempotency_key_invalid")

    state = _bounded_text(result.get("state"), 64, required=True)
    candidate = result.get("email")
    candidate_email = _validated_email(candidate) if candidate else None
    provider_status = _bounded_text(result.get("provider_email_status"), 80)
    confidence = _bounded_float(result.get("confidence"))
    evidence = _safe_evidence(result.get("evidence"))
    observed = _bounded_text(observed_at or utc_now(), 64, required=True)

    conn = get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM contact_provider_attempts WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            return _public_row(dict(existing))
        partner = conn.execute(
            "SELECT id, email FROM partners WHERE id = ?", (partner_id,)
        ).fetchone()
        if partner is None:
            raise ContactProviderStoreError("partner_not_found")
        current_email = str(partner["email"] or "").strip().casefold()
        if candidate_email is None:
            disposition = "none"
        elif candidate_email == current_email:
            disposition = "same_as_current"
        else:
            disposition = "review_required"

        send_gate_effect = "none"
        if provider_value == "enrow" and disposition == "same_as_current":
            if operation_value == "email_verify":
                projected_status = {
                    "verified": "valid",
                    "invalid": "invalid",
                    "risky": "catch_all",
                    "inconclusive": "inconclusive",
                }.get(state)
                projected_method = "enrow_verifier"
            else:
                projected_status = None
                projected_method = None
            if projected_status is not None:
                send_gate_effect = "eligible" if projected_status == "valid" else "blocked"
                conn.execute(
                    """
                    UPDATE partners
                    SET email_verification_status = ?,
                        email_verification_method = ?,
                        email_verification_confidence = ?,
                        email_verified_at = ?
                    WHERE id = ?
                    """,
                    (projected_status, projected_method, confidence, observed, partner_id),
                )

        conn.execute(
            """
            INSERT INTO contact_provider_attempts (
                partner_id, provider, operation, state, candidate_email,
                candidate_email_hash, provider_email_status, confidence,
                http_status, request_count, retry_count, provider_cost_microunits,
                candidate_disposition, send_gate_effect, evidence_json,
                idempotency_key, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                partner_id,
                provider_value,
                operation_value,
                state,
                candidate_email,
                email_hash(candidate_email) if candidate_email else None,
                provider_status,
                confidence,
                _bounded_int(result.get("http_status"), 0, 599),
                _bounded_int(result.get("request_count"), 0, 100, default=1),
                _bounded_int(result.get("retry_count"), 0, 100),
                _bounded_int(
                    result.get("provider_cost_microunits", result.get("cost_micro_usd")),
                    0,
                    10_000_000_000,
                ),
                disposition,
                send_gate_effect,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                key,
                observed,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM contact_provider_attempts WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise ContactProviderStoreError("attempt_not_stored")
        return _public_row(dict(row))
    finally:
        conn.close()


def list_attempts(
    *, partner_id: int | None = None, limit: int = 100, db_path: Path | None = None
) -> list[dict[str, Any]]:
    limit_value = max(1, min(int(limit), 500))
    conn = get_conn(db_path)
    try:
        if partner_id is None:
            rows = conn.execute(
                """
                SELECT a.*, p.partner_name, p.firm, p.email AS current_email,
                       p.email_verification_status AS canonical_email_status
                FROM contact_provider_attempts a
                JOIN partners p ON p.id = a.partner_id
                ORDER BY a.observed_at DESC, a.id DESC
                LIMIT ?
                """,
                (limit_value,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT a.*, p.partner_name, p.firm, p.email AS current_email,
                       p.email_verification_status AS canonical_email_status
                FROM contact_provider_attempts a
                JOIN partners p ON p.id = a.partner_id
                WHERE a.partner_id = ?
                ORDER BY a.observed_at DESC, a.id DESC
                LIMIT ?
                """,
                (partner_id, limit_value),
            ).fetchall()
        return [_public_row(dict(row)) for row in rows]
    finally:
        conn.close()


def contact_queue(
    *, query: str = "", status: str = "", limit: int = 100, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Return contact rows with their most recent provider evidence."""

    clauses = ["p.dedup_of IS NULL", "p.dropped_reason IS NULL"]
    params: list[Any] = []
    query_value = str(query or "").strip()
    if query_value:
        clauses.append("(p.partner_name LIKE ? OR p.firm LIKE ? OR p.email LIKE ?)")
        like = f"%{query_value}%"
        params.extend([like, like, like])
    status_value = str(status or "").strip().lower()
    if status_value == "needs_review":
        clauses.append("latest.candidate_disposition = 'review_required'")
    elif status_value == "verified":
        clauses.append("p.email_verification_status IN ('valid', 'deliverable', 'pb_verified')")
    elif status_value == "unverified":
        clauses.append("COALESCE(p.email_verification_status, '') NOT IN ('valid', 'deliverable', 'pb_verified')")
    elif status_value == "never_checked":
        clauses.append("latest.id IS NULL")
    elif status_value:
        raise ContactProviderStoreError("status_filter_invalid")

    limit_value = max(1, min(int(limit), 500))
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT p.id, p.partner_name, p.firm, p.email, p.firm_domain,
                   p.linkedin_url, p.email_verification_status,
                   p.email_verification_method, p.email_verified_at,
                   latest.provider AS latest_provider,
                   latest.state AS latest_provider_state,
                   latest.provider_email_status,
                   latest.candidate_email,
                   latest.candidate_disposition,
                   latest.observed_at AS provider_observed_at
            FROM partners p
            LEFT JOIN contact_provider_attempts latest ON latest.id = (
                SELECT a.id FROM contact_provider_attempts a
                WHERE a.partner_id = p.id
                ORDER BY a.observed_at DESC, a.id DESC LIMIT 1
            )
            WHERE {' AND '.join(clauses)}
            ORDER BY p.id DESC
            """,
            params,
        ).fetchall()
        contacts: list[dict[str, Any]] = []
        for raw_row in rows:
            row = dict(raw_row)
            current_email = str(row.get("email") or "").strip().casefold()
            current_domain = current_email.rpartition("@")[2]
            placeholder = (
                not _EMAIL_RE.fullmatch(current_email)
                or current_domain.endswith(".invalid")
                or "needs-enrichment" in current_email
            )
            firm_domain = _usable_domain(row.get("firm_domain"))
            if not firm_domain and not placeholder and current_domain not in _CONSUMER_EMAIL_DOMAINS:
                firm_domain = _usable_domain(current_domain)
            person_name = str(row.get("partner_name") or "").strip().casefold()
            identity_ready = bool(person_name and "contact tbd" not in person_name and "firm contact" not in person_name)
            row["firm_domain"] = firm_domain or None
            row["identity_ready"] = identity_ready
            row["can_verify"] = not placeholder
            row["can_enrow_find"] = bool(identity_ready and firm_domain)
            row["can_apollo_enrich"] = bool(identity_ready and (firm_domain or row.get("linkedin_url")))
            contacts.append(row)
        contacts.sort(
            key=lambda row: (
                0 if row.get("candidate_disposition") == "review_required" else 1,
                0 if row.get("can_verify") or row.get("can_enrow_find") or row.get("can_apollo_enrich") else 1,
                -int(row["id"]),
            )
        )
        return contacts[:limit_value]
    finally:
        conn.close()


def provider_summary(*, db_path: Path | None = None) -> dict[str, Any]:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT provider, COUNT(*) AS attempts,
                   SUM(CASE WHEN candidate_email IS NOT NULL THEN 1 ELSE 0 END) AS found,
                   SUM(CASE WHEN candidate_disposition = 'review_required' THEN 1 ELSE 0 END) AS needs_review,
                   MAX(observed_at) AS last_run_at
            FROM contact_provider_attempts
            GROUP BY provider
            """
        ).fetchall()
        summary = {
            provider: {"attempts": 0, "found": 0, "needs_review": 0, "last_run_at": None}
            for provider in PROVIDERS
        }
        for row in rows:
            summary[row["provider"]] = {
                "attempts": int(row["attempts"] or 0),
                "found": int(row["found"] or 0),
                "needs_review": int(row["needs_review"] or 0),
                "last_run_at": row["last_run_at"],
            }
        return summary
    finally:
        conn.close()


def record_connection_health(
    *, provider: str, result: Mapping[str, Any], db_path: Path | None = None
) -> dict[str, Any]:
    provider_value = str(provider or "").strip().lower()
    if provider_value not in PROVIDERS:
        raise ContactProviderStoreError("provider_invalid")
    status = _bounded_text(result.get("status"), 64, required=True)
    error_code = _bounded_text(result.get("error_code"), 120)
    checked_at = utc_now()
    credits = _bounded_float_nonnegative(result.get("credits_remaining"))
    conn = get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO contact_provider_connections (
                provider, health_status, credits_remaining, http_status,
                last_error_code, checked_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                health_status = excluded.health_status,
                credits_remaining = excluded.credits_remaining,
                http_status = excluded.http_status,
                last_error_code = excluded.last_error_code,
                checked_at = excluded.checked_at,
                updated_at = excluded.updated_at
            """,
            (
                provider_value,
                status,
                credits,
                _bounded_int(result.get("http_status"), 0, 599),
                error_code,
                checked_at,
                checked_at,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM contact_provider_connections WHERE provider = ?",
            (provider_value,),
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def connection_health(*, db_path: Path | None = None) -> dict[str, dict[str, Any]]:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM contact_provider_connections").fetchall()
        return {str(row["provider"]): dict(row) for row in rows}
    finally:
        conn.close()


def apply_candidate(
    *, attempt_id: int, confirmation: str, db_path: Path | None = None
) -> dict[str, Any]:
    """Apply one reviewed candidate address without granting send eligibility."""

    confirmed = _validated_email(confirmation)
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM contact_provider_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise ContactProviderStoreError("attempt_not_found")
        candidate = str(row["candidate_email"] or "").strip().casefold()
        if not candidate or candidate != confirmed:
            raise ContactProviderStoreError("candidate_confirmation_mismatch")
        if row["candidate_disposition"] != "review_required":
            raise ContactProviderStoreError("candidate_not_reviewable")
        duplicate = conn.execute(
            "SELECT id FROM partners WHERE email = ? COLLATE NOCASE AND id != ?",
            (candidate, row["partner_id"]),
        ).fetchone()
        if duplicate is not None:
            raise ContactProviderStoreError("candidate_email_already_used")

        provider_status = "apollo_verified" if row["provider"] == "apollo" else "enrow_confirmed"
        provider_method = row["provider"]
        send_gate_effect = "none"
        now = utc_now()
        conn.execute(
            """
            UPDATE partners
            SET email = ?, email_uncertain = ?,
                email_verification_status = ?, email_verification_method = ?,
                email_verification_confidence = ?, email_verified_at = ?
            WHERE id = ?
            """,
            (
                candidate,
                1,
                provider_status,
                provider_method,
                row["confidence"],
                now,
                row["partner_id"],
            ),
        )
        conn.execute(
            """
            UPDATE contact_provider_attempts
            SET candidate_disposition = 'applied', applied_at = ?
            WHERE id = ?
            """,
            (now, attempt_id),
        )
        conn.commit()
        return {
            "attempt_id": attempt_id,
            "partner_id": row["partner_id"],
            "candidate_disposition": "applied",
            "canonical_email_status": provider_status,
            "send_gate_effect": send_gate_effect,
        }
    finally:
        conn.close()


def dismiss_candidate(*, attempt_id: int, db_path: Path | None = None) -> dict[str, Any]:
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT partner_id, candidate_disposition FROM contact_provider_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise ContactProviderStoreError("attempt_not_found")
        if row["candidate_disposition"] != "review_required":
            raise ContactProviderStoreError("candidate_not_reviewable")
        now = utc_now()
        conn.execute(
            """
            UPDATE contact_provider_attempts
            SET candidate_disposition = 'dismissed', dismissed_at = ?
            WHERE id = ?
            """,
            (now, attempt_id),
        )
        conn.commit()
        return {"attempt_id": attempt_id, "partner_id": row["partner_id"], "candidate_disposition": "dismissed"}
    finally:
        conn.close()


def _safe_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "matched_domain",
        "matched_name",
        "matched_organization",
        "provider_person_id",
        "source",
    }
    clean: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, bool):
            clean[key] = item
        elif isinstance(item, str) and item.strip():
            clean[key] = item.strip()[:240]
    return clean


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.pop("evidence_json", "{}")
    try:
        row["evidence"] = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        row["evidence"] = {}
    row["send_gate_effect"] = row.get("send_gate_effect") or "none"
    return row


def _validated_email(value: Any) -> str:
    if not isinstance(value, str):
        raise ContactProviderStoreError("email_invalid")
    normalized = value.strip().casefold()
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise ContactProviderStoreError("email_invalid")
    return normalized


def _bounded_text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        if required:
            raise ContactProviderStoreError("text_invalid")
        return None
    return value.strip()[:maximum]


def _bounded_int(value: Any, minimum: int, maximum: int, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))


def _bounded_float_nonnegative(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _usable_domain(value: Any) -> str:
    from vcoutreach.web.enrichment.email_guess import is_free_email_domain
    if not isinstance(value, str):
        return ""
    normalized = value.strip().casefold()
    for prefix in ("https://", "http://"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    normalized = normalized.split("/", 1)[0].removeprefix("www.")
    if (
        not normalized
        or "@" in normalized
        or "." not in normalized
        or normalized.endswith(".invalid")
        or len(normalized) > 253
        or is_free_email_domain(normalized)
    ):
        return ""
    return normalized


__all__ = [
    "ContactProviderStoreError",
    "OPERATIONS",
    "PROVIDERS",
    "apply_candidate",
    "contact_queue",
    "connection_health",
    "dismiss_candidate",
    "email_hash",
    "list_attempts",
    "provider_summary",
    "record_connection_health",
    "record_attempt",
    "utc_now",
]
