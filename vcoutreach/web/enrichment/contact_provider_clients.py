"""Bounded Apollo and Enrow clients for contact intelligence.

These clients can spend provider credits and disclose one named business
contact to the selected provider. They cannot draft, connect a mailbox, create
a sequence, or send email.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from vcoutreach.web.vendor import EnrowSingleEmailResolver, fixed_urlopen as urlopen


APOLLO_PEOPLE_MATCH_ENDPOINT = "https://api.apollo.io/api/v1/people/match"
APOLLO_HEALTH_ENDPOINT = "https://api.apollo.io/api/v1/auth/health"
ENROW_VERIFY_SINGLE_ENDPOINT = "https://api.enrow.io/email/verify/single"
ENROW_ACCOUNT_INFO_ENDPOINT = "https://api.enrow.io/account/info"
REQUEST_TIMEOUT_SECONDS = 12
ENROW_MAX_POLLS = 2
ENROW_POLL_INTERVAL_SECONDS = 3
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

HttpRequest = Callable[
    [str, str, Mapping[str, str], Mapping[str, Any] | None, int],
    tuple[int, Mapping[str, Any]],
]


class ContactProviderClientError(ValueError):
    """Raised for malformed provider inputs, never for a provider miss."""


def test_provider_connection(
    provider: str,
    *,
    api_key: str,
    request_fn: HttpRequest | None = None,
) -> dict[str, Any]:
    """Run a documented zero-credit authentication check."""

    provider_value = str(provider or "").strip().lower()
    if provider_value not in {"apollo", "enrow"}:
        raise ContactProviderClientError("provider_invalid")
    endpoint = APOLLO_HEALTH_ENDPOINT if provider_value == "apollo" else ENROW_ACCOUNT_INFO_ENDPOINT
    headers = {
        "x-api-key": _api_key(api_key),
        "accept": "application/json",
        "content-type": "application/json",
        "cache-control": "no-cache",
    }
    execute = request_fn or _stdlib_request
    try:
        status, body = execute("GET", endpoint, headers, None, REQUEST_TIMEOUT_SECONDS)
    except (OSError, ValueError):
        return {
            "ok": False,
            "status": "unreachable",
            "http_status": 0,
            "error_code": "provider_unreachable",
            "credits_remaining": None,
        }
    if status == 401:
        return {"ok": False, "status": "unauthorized", "http_status": status, "error_code": "key_rejected", "credits_remaining": None}
    if status == 403:
        return {"ok": False, "status": "plan_restricted", "http_status": status, "error_code": "scope_or_plan_restricted", "credits_remaining": None}
    if status == 429:
        return {"ok": False, "status": "rate_limited", "http_status": status, "error_code": "rate_limited", "credits_remaining": None}
    if status != 200:
        return {"ok": False, "status": "provider_error", "http_status": status, "error_code": "provider_error", "credits_remaining": None}
    if provider_value == "apollo":
        healthy = body.get("healthy") is True and body.get("is_logged_in") is True
        return {
            "ok": healthy,
            "status": "connected" if healthy else "response_invalid",
            "http_status": status,
            "error_code": None if healthy else "health_response_invalid",
            "credits_remaining": None,
        }
    credits = body.get("credits")
    credits_remaining = float(credits) if isinstance(credits, (int, float)) and not isinstance(credits, bool) else None
    return {
        "ok": True,
        "status": "connected",
        "http_status": status,
        "error_code": None,
        "credits_remaining": credits_remaining,
    }


def run_apollo_person_enrichment(
    partner: Mapping[str, Any],
    *,
    api_key: str,
    request_fn: HttpRequest | None = None,
) -> dict[str, Any]:
    """Enrich one named work contact using Apollo's synchronous endpoint."""

    identity = _identity(partner, require_domain=False)
    if not identity["firm_domain"] and not identity.get("linkedin_url"):
        raise ContactProviderClientError("partner_identity_incomplete")
    headers = {
        "x-api-key": _api_key(api_key),
        "accept": "application/json",
        "content-type": "application/json",
        "cache-control": "no-cache",
    }
    params: dict[str, str] = {
        "name": identity["person_name"],
        "reveal_personal_emails": "false",
        "reveal_phone_number": "false",
    }
    if identity["firm_domain"]:
        params["domain"] = identity["firm_domain"]
    if identity.get("linkedin_url"):
        params["linkedin_url"] = identity["linkedin_url"]
    endpoint = f"{APOLLO_PEOPLE_MATCH_ENDPOINT}?{urlencode(params)}"
    execute = request_fn or _stdlib_request
    try:
        status, body = execute("POST", endpoint, headers, None, REQUEST_TIMEOUT_SECONDS)
    except (OSError, ValueError):
        return _result("timeout", http_status=0)
    if status == 401:
        return _result("unauthorized", http_status=status)
    if status == 403:
        return _result("plan_restricted", http_status=status)
    if status == 429:
        return _result("rate_limited", http_status=status)
    if status >= 500:
        return _result("provider_error", http_status=status)
    if status != 200:
        return _result("request_rejected", http_status=status)

    person = body.get("person")
    if not isinstance(person, Mapping):
        return _result("no_result", http_status=status)
    raw_email = person.get("email")
    email = _optional_email(raw_email)
    provider_status = _bounded_text(person.get("email_status"), 80)
    if email is None:
        return _result(
            "no_result",
            http_status=status,
            provider_email_status=provider_status,
            evidence=_apollo_evidence(person, identity),
        )
    state = "confirmed" if provider_status == "verified" else "found_unverified"
    confidence = 0.92 if provider_status == "verified" else 0.62
    return _result(
        state,
        email=email,
        http_status=status,
        provider_email_status=provider_status,
        confidence=confidence,
        evidence=_apollo_evidence(person, identity),
    )


def run_enrow_email_find(
    partner: Mapping[str, Any],
    *,
    api_key: str,
    request_fn: HttpRequest | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Find one work email with the existing bounded Enrow adapter."""

    identity = _identity(partner)
    resolver = EnrowSingleEmailResolver(
        api_key=_api_key(api_key),
        request_fn=request_fn,
        sleep_fn=sleep_fn,
    )
    result = dict(
        resolver.resolve(
            {
                "candidate_id": str(identity["partner_id"]),
                "person_name": identity["person_name"],
                "firm_name": identity["firm_name"],
                "firm_domain": identity["firm_domain"],
            }
        )
    )
    result["provider_email_status"] = "valid" if result.get("state") == "confirmed" else None
    result["confidence"] = 0.88 if result.get("state") == "confirmed" else None
    result["provider_cost_microunits"] = int(result.pop("cost_micro_usd", 0) or 0)
    result["evidence"] = {
        "matched_domain": bool(result.get("email") and _email_domain(result["email"]) == identity["firm_domain"]),
        "matched_name": True,
        "matched_organization": True,
        "source": "enrow_email_finder",
    }
    return result


def run_enrow_email_verification(
    partner: Mapping[str, Any],
    *,
    api_key: str,
    request_fn: HttpRequest | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Verify the contact's current email with Enrow's async verifier."""

    identity = _identity(partner, require_domain=False)
    email = _required_email(partner.get("email"))
    headers = {
        "x-api-key": _api_key(api_key),
        "accept": "application/json",
        "content-type": "application/json",
    }
    execute = request_fn or _stdlib_request
    sleeper = sleep_fn or time.sleep
    try:
        status, body = execute(
            "POST",
            ENROW_VERIFY_SINGLE_ENDPOINT,
            headers,
            {"email": email, "custom": {"candidate_id": str(identity["partner_id"])}},
            REQUEST_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError):
        return _result("timeout", email=email, http_status=0)
    terminal = _enrow_verify_terminal(status, body, email, request_count=1, retry_count=0)
    if terminal is not None:
        return terminal
    job_id = _job_id(body)
    if job_id is None:
        return _result("response_invalid", email=email, http_status=status)
    for retry_count in range(1, ENROW_MAX_POLLS + 1):
        sleeper(ENROW_POLL_INTERVAL_SECONDS)
        try:
            status, body = execute(
                "GET",
                f"{ENROW_VERIFY_SINGLE_ENDPOINT}?{urlencode({'id': job_id})}",
                headers,
                None,
                REQUEST_TIMEOUT_SECONDS,
            )
        except (OSError, ValueError):
            return _result(
                "timeout",
                email=email,
                http_status=0,
                request_count=1 + retry_count,
                retry_count=retry_count,
            )
        terminal = _enrow_verify_terminal(
            status,
            body,
            email,
            request_count=1 + retry_count,
            retry_count=retry_count,
        )
        if terminal is not None:
            return terminal
    return _result(
        "timeout",
        email=email,
        http_status=0,
        request_count=1 + ENROW_MAX_POLLS,
        retry_count=ENROW_MAX_POLLS,
    )


def _enrow_verify_terminal(
    status: int,
    body: Mapping[str, Any],
    email: str,
    *,
    request_count: int,
    retry_count: int,
) -> dict[str, Any] | None:
    if status in {401, 403}:
        return _result("unauthorized", email=email, http_status=status, request_count=request_count, retry_count=retry_count)
    if status == 402:
        return _result("payment_required", email=email, http_status=status, request_count=request_count, retry_count=retry_count)
    if status == 429:
        return _result("rate_limited", email=email, http_status=status, request_count=request_count, retry_count=retry_count)
    if status >= 500:
        return _result("provider_error", email=email, http_status=status, request_count=request_count, retry_count=retry_count)
    if status not in {200, 201, 202}:
        return _result("request_rejected", email=email, http_status=status, request_count=request_count, retry_count=retry_count)

    result = body.get("result") if isinstance(body.get("result"), Mapping) else body.get("data")
    if not isinstance(result, Mapping):
        result = body
    raw_status = None
    for key in ("qualification", "status", "state", "result"):
        value = result.get(key) if isinstance(result, Mapping) else None
        if isinstance(value, str) and value.strip():
            raw_status = value.strip().casefold().replace("-", "_").replace(" ", "_")
            break
    if raw_status in {"valid", "deliverable", "verified", "safe"}:
        state, confidence = "verified", 0.98
    elif raw_status in {"invalid", "undeliverable", "rejected", "do_not_send"}:
        state, confidence = "invalid", 0.98
    elif raw_status in {"catch_all", "risky", "accept_all", "unknown"}:
        state, confidence = "risky", 0.55
    elif raw_status in {"unverifiable", "inconclusive"}:
        state, confidence = "inconclusive", 0.25
    else:
        return None
    return _result(
        state,
        email=email,
        http_status=status,
        request_count=request_count,
        retry_count=retry_count,
        provider_email_status=raw_status,
        confidence=confidence,
        provider_cost_microunits=_provider_cost(body),
        evidence={"source": "enrow_email_verifier"},
    )


def _apollo_evidence(person: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    organization = person.get("organization") if isinstance(person.get("organization"), Mapping) else {}
    organization_domain = _domain(organization.get("primary_domain"))
    provider_name = _bounded_text(person.get("name"), 160)
    return {
        "matched_domain": bool(identity["firm_domain"] and organization_domain and organization_domain == identity["firm_domain"]),
        "matched_name": bool(provider_name and provider_name.casefold() == identity["person_name"].casefold()),
        "matched_organization": bool(identity["firm_domain"] and organization_domain and organization_domain == identity["firm_domain"]),
        "provider_person_id": _bounded_text(person.get("id"), 160),
        "source": "apollo_people_enrichment",
    }


def _identity(partner: Mapping[str, Any], *, require_domain: bool = True) -> dict[str, Any]:
    if not isinstance(partner, Mapping):
        raise ContactProviderClientError("partner_invalid")
    try:
        partner_id = int(partner.get("id"))
    except (TypeError, ValueError) as exc:
        raise ContactProviderClientError("partner_invalid") from exc
    if partner_id <= 0:
        raise ContactProviderClientError("partner_invalid")
    person_name = _bounded_text(partner.get("partner_name"), 160)
    firm_name = _bounded_text(partner.get("firm"), 160)
    firm_domain = _domain(partner.get("firm_domain"))
    if not person_name or not firm_name or (require_domain and not firm_domain):
        raise ContactProviderClientError("partner_identity_incomplete")
    lowered_name = person_name.casefold()
    if "contact tbd" in lowered_name or "firm contact" in lowered_name:
        raise ContactProviderClientError("partner_identity_incomplete")
    return {
        "partner_id": partner_id,
        "person_name": person_name,
        "firm_name": firm_name,
        "firm_domain": firm_domain,
        "linkedin_url": _bounded_text(partner.get("linkedin_url"), 500),
    }


def _api_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or any(character.isspace() for character in value.strip()):
        raise ContactProviderClientError("provider_key_invalid")
    return value.strip()


def _required_email(value: Any) -> str:
    email = _optional_email(value)
    if email is None:
        raise ContactProviderClientError("email_invalid")
    return email


def _optional_email(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > 320
        or not _EMAIL_RE.fullmatch(normalized)
        or normalized.rpartition("@")[2].endswith(".invalid")
        or "needs-enrichment" in normalized
    ):
        return None
    return normalized


def _email_domain(value: str) -> str:
    return value.rpartition("@")[2].casefold()


def _domain(value: Any) -> str:
    from vcoutreach.web.enrichment.email_guess import is_free_email_domain
    if not isinstance(value, str):
        return ""
    normalized = value.strip().casefold()
    for prefix in ("https://", "http://"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    normalized = normalized.split("/", 1)[0].removeprefix("www.")
    if "@" in normalized or "." not in normalized or len(normalized) > 253 or is_free_email_domain(normalized):
        return ""
    return normalized


def _job_id(body: Mapping[str, Any]) -> str | None:
    for key in ("id", "search_id", "verification_id"):
        value = body.get(key)
        if isinstance(value, str) and value.strip() and len(value.strip()) <= 200:
            return value.strip()
    return None


def _provider_cost(body: Mapping[str, Any]) -> int:
    credits = body.get("credits")
    value = credits.get("cost") if isinstance(credits, Mapping) else 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(float(value) * 1_000_000)
    return 0


def _result(
    state: str,
    *,
    email: str | None = None,
    http_status: int,
    request_count: int = 1,
    retry_count: int = 0,
    provider_email_status: str | None = None,
    confidence: float | None = None,
    provider_cost_microunits: int = 0,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "email": email,
        "provider_email_status": provider_email_status,
        "confidence": confidence,
        "http_status": http_status,
        "request_count": request_count,
        "retry_count": retry_count,
        "provider_cost_microunits": provider_cost_microunits,
        "evidence": dict(evidence or {}),
        "delivery_authority": False,
        "send_gate_effect": "none",
    }


def _bounded_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]


def _stdlib_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any] | None,
    timeout: int,
) -> tuple[int, Mapping[str, Any]]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers=dict(headers), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310, fixed provider endpoints
            return response.status, _json_object(response.read(1048577))
    except HTTPError as exc:
        return exc.code, _json_object(exc.read(1048577))
    except URLError as exc:
        raise OSError("provider_network_error") from exc


def _json_object(raw: bytes) -> Mapping[str, Any]:
    if len(raw) > 1048576:
        raise ValueError("provider_response_invalid")
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("provider_response_invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError("provider_response_invalid")
    return value


__all__ = [
    "APOLLO_HEALTH_ENDPOINT",
    "APOLLO_PEOPLE_MATCH_ENDPOINT",
    "ContactProviderClientError",
    "ENROW_ACCOUNT_INFO_ENDPOINT",
    "ENROW_VERIFY_SINGLE_ENDPOINT",
    "run_apollo_person_enrichment",
    "run_enrow_email_find",
    "run_enrow_email_verification",
    "test_provider_connection",
]
