"""Small integration helpers with no optional dependencies or import-time I/O."""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ENROW_SINGLE_ENDPOINT = "https://api.enrow.io/email/find/single"
MAX_HTTP_REQUESTS = 3  # one creation request plus at most two status checks
MAX_POLL_RETRIES = 2
REQUEST_TIMEOUT_SECONDS = 10
POLL_INTERVAL_SECONDS = 3
BACKOFF_SECONDS = 60


class EnrowLiveAdapterError(ValueError):
    """Raised for a malformed or unsafe Enrow request."""


HttpRequest = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None, int], tuple[int, Mapping[str, Any]]]


def enrow_resolver_from_environment(*, request_fn: HttpRequest | None = None) -> Callable[[Mapping[str, str]], Mapping[str, Any]]:
    """Build a resolver from ``ENROW_API_KEY`` without logging the credential."""

    api_key = os.environ.get("ENROW_API_KEY")
    if not isinstance(api_key, str) or not api_key.strip():
        raise EnrowLiveAdapterError("enrow_api_key_not_configured")
    return EnrowSingleEmailResolver(api_key=api_key, request_fn=request_fn).resolve


class EnrowSingleEmailResolver:
    """Single-lookup Enrow resolver with fixed request, retry, and timeout caps."""

    def __init__(
        self,
        *,
        api_key: str,
        request_fn: HttpRequest | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise EnrowLiveAdapterError("enrow_api_key_invalid")
        self._api_key = credential_value(api_key)
        self._request = request_fn or _stdlib_request
        self._sleep = sleep_fn or time.sleep

    def resolve(self, request: Mapping[str, str]) -> Mapping[str, Any]:
        candidate = _request(request)
        headers = {"x-api-key": self._api_key, "content-type": "application/json", "accept": "application/json"}
        payload = {
            "fullname": candidate["person_name"],
            "company_name": candidate["firm_name"],
            "company_domain": candidate["firm_domain"],
            "custom": {"candidate_id": candidate["candidate_id"]},
        }
        try:
            status, body = self._request("POST", ENROW_SINGLE_ENDPOINT, headers, payload, REQUEST_TIMEOUT_SECONDS)
        except (OSError, ValueError):
            return _result("timeout", http_status=0, retry_count=0, next_retry_seconds=BACKOFF_SECONDS)
        terminal = _terminal(status, body, candidate["firm_domain"], retry_count=0)
        if terminal is not None:
            return terminal
        job_id = _job_id(body)
        if job_id is None:
            return _result("timeout", http_status=0, retry_count=0, next_retry_seconds=BACKOFF_SECONDS)
        for retry_count in range(1, MAX_POLL_RETRIES + 1):
            # Enrow's single-find endpoint is asynchronous.  Do not burn the
            # bounded polls immediately after creation, because a transient
            # empty result is not a completed no-result.
            self._sleep(POLL_INTERVAL_SECONDS)
            try:
                status, body = self._request(
                    "GET", f"{ENROW_SINGLE_ENDPOINT}?{urlencode({'id': job_id})}", headers, None, REQUEST_TIMEOUT_SECONDS
                )
            except (OSError, ValueError):
                return _result("timeout", http_status=0, retry_count=retry_count, next_retry_seconds=BACKOFF_SECONDS)
            terminal = _terminal(status, body, candidate["firm_domain"], retry_count=retry_count)
            if terminal is not None:
                return terminal
        return _result("timeout", http_status=0, retry_count=MAX_POLL_RETRIES, next_retry_seconds=BACKOFF_SECONDS)


def _request(value: Mapping[str, str]) -> dict[str, str]:
    required = {"candidate_id", "person_name", "firm_name", "firm_domain"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise EnrowLiveAdapterError("enrow_request_invalid")
    normalized: dict[str, str] = {}
    for field in required:
        item = value[field]
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 160:
            raise EnrowLiveAdapterError("enrow_request_invalid")
        normalized[field] = item.strip()
    if "." not in normalized["firm_domain"] or "@" in normalized["firm_domain"]:
        raise EnrowLiveAdapterError("enrow_request_invalid")
    normalized["firm_domain"] = normalized["firm_domain"].casefold()
    return normalized


def _terminal(status: int, body: Mapping[str, Any], domain: str, *, retry_count: int) -> dict[str, Any] | None:
    if status in {401, 403}:
        return _result("unauthorized", http_status=status, retry_count=retry_count)
    if status == 402:
        # Payment or credit exhaustion is an operator boundary, never a
        # definitive address miss. Callers must pause the provider and preserve
        # the candidate for a later credited replay.
        return _result("payment_required", http_status=status, retry_count=retry_count)
    if status == 429:
        return _result("rate_limited", http_status=status, retry_count=retry_count, next_retry_seconds=BACKOFF_SECONDS)
    if status >= 500:
        return _result("timeout", http_status=0, retry_count=retry_count, next_retry_seconds=BACKOFF_SECONDS)
    if status not in {200, 201, 202}:
        return _result("no_result", http_status=200, retry_count=retry_count)
    result = body.get("result") if isinstance(body.get("result"), Mapping) else body
    qualification = result.get("qualification") if isinstance(result, Mapping) else None
    email = result.get("email") if isinstance(result, Mapping) else None
    if qualification == "valid" and isinstance(email, str) and email.rpartition("@")[2].casefold() == domain:
        return _result("confirmed", email=email, http_status=200, retry_count=retry_count, cost_micro_usd=_cost(body))
    # Only an explicit invalid qualification is a definitive miss.  A transient
    # empty 200 has been observed before the asynchronous search is ready.
    if status == 200 and qualification in {"invalid", "not_found"}:
        return _result("no_result", http_status=200, retry_count=retry_count, cost_micro_usd=_cost(body))
    return None


def _job_id(body: Mapping[str, Any]) -> str | None:
    for key in ("id", "search_id"):
        value = body.get(key)
        if isinstance(value, str) and value.strip() and len(value) <= 200:
            return value.strip()
    return None


def _cost(body: Mapping[str, Any]) -> int:
    credits = body.get("credits")
    value = credits.get("cost") if isinstance(credits, Mapping) else 0
    if isinstance(value, int) and value >= 0:
        return value * 1_000_000
    return 0


def _result(state: str, *, email: str | None = None, http_status: int, retry_count: int, next_retry_seconds: int | None = None, cost_micro_usd: int = 0) -> dict[str, Any]:
    return {
        "state": state,
        "email": email,
        "provenance": "provider_confirmed" if state == "confirmed" else None,
        "request_count": 1 + retry_count,
        "retry_count": retry_count,
        "cost_micro_usd": cost_micro_usd,
        "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "http_status": http_status,
        "next_retry_seconds": next_retry_seconds,
    }


def _stdlib_request(method: str, url: str, headers: Mapping[str, str], payload: Mapping[str, Any] | None, timeout: int) -> tuple[int, Mapping[str, Any]]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers=dict(headers), method=method)
    try:
        with fixed_urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed Enrow endpoint
            raw = response.read(1048577)
            return response.status, _json_object(raw)
    except HTTPError as exc:
        raw = exc.read(1048577)
        return exc.code, _json_object(raw)
    except URLError as exc:
        raise OSError("enrow_network_error") from exc


def _json_object(raw: bytes) -> Mapping[str, Any]:
    if len(raw) > 1048576:
        raise ValueError("enrow_response_invalid")
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("enrow_response_invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError("enrow_response_invalid")
    return value


__all__ = [
    "BACKOFF_SECONDS", "ENROW_SINGLE_ENDPOINT", "EnrowLiveAdapterError",
    "EnrowSingleEmailResolver", "MAX_HTTP_REQUESTS", "MAX_POLL_RETRIES",
    "POLL_INTERVAL_SECONDS", "REQUEST_TIMEOUT_SECONDS", "enrow_resolver_from_environment",
]


# Workspace helpers stay independent of Flask and the optional integrations.
import hashlib
import ipaddress
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, build_opener


def workspace_root() -> Path:
    # Consult active app/context without creating a workspace while importing.
    from vcoutreach.web import config
    try:
        from flask import current_app, has_app_context
        if has_app_context() and "workspace" in current_app.extensions:
            return current_app.extensions["workspace"].root.resolve()
    except ImportError:
        pass
    context = getattr(config, "_current", None)
    active = context.get() if context is not None else None
    return active.root.resolve() if active is not None else Path(os.environ.get("OUTREACH_HOME") or Path.cwd()).expanduser().resolve()


def workspace_path(relative: str | Path) -> Path:
    root = workspace_root()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path must stay inside the workspace")
    return path


def data_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    base = workspace_path("data")
    path = (workspace_root() / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(base):
        raise ValueError("Session files must stay in workspace data/")
    return path


def setting_value(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is not None:
        return value
    env = workspace_path(".env")
    if env.is_file() and env.stat().st_size <= 131072:
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, item = line.strip().partition("=")
            if sep and key.strip() == name:
                return item.strip().strip("\"'")
    return default


def credential_value(value: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 8192:
        raise ValueError("api_key_invalid")
    if any(c.isspace() or c in "\"'#$`()" or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("api_key_invalid")
    return value


def normalized_email(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("email identity is malformed")
    value = unicodedata.normalize("NFC", value).strip().casefold()
    if value.count("@") != 1 or any(c.isspace() for c in value):
        raise ValueError("email identity is malformed")
    local, domain = value.split("@")
    domain = domain.encode("idna").decode("ascii")
    if not local or "." not in domain:
        raise ValueError("email identity is malformed")
    return local + "@" + domain


def email_identity_hash(email: str) -> str:
    payload = "publik-suppression:v1:email:" + normalized_email(email)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def validated_url(value: str, *, origin_only: bool = False, loopback: bool = False, hosts: tuple[str, ...] = ()) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("URL is invalid")
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    local = host in {"localhost", "127.0.0.1", "::1"}
    if url.username or url.password or url.fragment or not host:
        raise ValueError("URL is invalid")
    try:
        port = url.port
    except ValueError:
        raise ValueError("URL port is invalid") from None
    if loopback and local:
        if url.scheme not in {"http", "https"}:
            raise ValueError("URL scheme is invalid")
    else:
        if url.scheme != "https" or local or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("URL must use public HTTPS")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host or re.fullmatch(r"[0-9.]+", host):
                raise ValueError("URL host is invalid")
        else:
            if not ip.is_global:
                raise ValueError("URL must use a public host")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is invalid")
    if hosts and host not in hosts:
        raise ValueError("URL host is not allowed")
    if origin_only and (url.path not in {"", "/"} or url.query):
        raise ValueError("URL must be an origin")
    return urlunsplit((url.scheme, url.netloc, "" if origin_only else url.path, url.query, ""))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fixed_urlopen(request, timeout=10):
    # Reject redirects so provider credentials never travel to another host.
    url = request.full_url if hasattr(request, "full_url") else str(request)
    validated_url(url, loopback=True)
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def load_sender_context(sender_profile=None, facts=None):
    if sender_profile is None:
        try:
            sender_profile = json.loads(workspace_path("sender.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sender_profile = {}
    if facts is None:
        try:
            from vcoutreach.web.facts import load_facts
            facts = load_facts(workspace_root())
        except (ImportError, OSError, ValueError, TypeError):
            facts = {}
    return (sender_profile if isinstance(sender_profile, dict) else {}, facts if isinstance(facts, dict) else {})


def require_dependency(name: str):
    import importlib
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ImportError('Integration needs ' + name + '. Run: pip install -e ".[integrations]"') from exc


def public_requests_get(url: str, **kwargs):
    validated_url(url)
    requests = require_dependency("requests")
    kwargs["allow_redirects"] = False
    response = requests.get(url, **kwargs)
    # Do not silently treat a redirect as a successful empty page.
    if 300 <= response.status_code < 400:
        raise ValueError("Website redirects require an explicit destination URL")
    return response
