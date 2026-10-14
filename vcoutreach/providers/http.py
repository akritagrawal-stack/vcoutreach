"""Shared HTTP transport and retry layer for providers."""
from __future__ import annotations

import http.client
import json
import random
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from vcoutreach import __version__
from vcoutreach.types import (
    Hook,
    InsufficientFacts,
    MetaValue,
    PersonalizationRequest,
    ProviderAuthError,
    ProviderConfigError,
    ProviderCreditError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
)

__all__ = [
    "HttpResponse",
    "Transport",
    "UrllibTransport",
    "TransportFailure",
    "send_with_retries",
    "HttpProviderBase",
    "is_no_hook",
    "RETRY_STATUSES",
    "MAX_REQUEST_BYTES",
]

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 30.0
MAX_BACKOFF_BASE_SECONDS = 16
MAX_REQUEST_BYTES = 4_000_000
MAX_RESPONSE_BYTES = 8_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 3
USER_AGENT = "vcoutreach/" + __version__

_NUMERIC_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_KEY_CHARS = re.compile(r"[\x21-\x7e]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MIN_REDACT_KEY_LENGTH = 8
_QUOTE_CHARS = "\"'`\u201c\u201d\u2018\u2019"


@dataclass(frozen=True)
class HttpResponse:
    """A completed HTTP exchange with lower-cased header names."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class TransportFailure(Exception):
    """Raised when a request could not complete (connect error, timeout, reset)."""


class Transport(Protocol):
    """Anything that can perform one HTTP request."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an Authorization header is never forwarded elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _lower_headers(message: Any) -> dict[str, str]:
    """Lower-case header names, joining repeated headers with a comma."""
    out: dict[str, str] = {}
    for name, value in message.items():
        key = str(name).lower()
        text = str(value)
        out[key] = out[key] + ", " + text if key in out else text
    return out


def _describe_failure(exc: BaseException) -> str:
    """Short description of a transport error that never echoes request data."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "the request timed out"
    if isinstance(reason, (str, OSError, http.client.HTTPException)):
        text = _CONTROL.sub(" ", str(reason)).strip()
        return text[:200] or type(reason).__name__
    if isinstance(reason, ValueError):
        return "the request was invalid"
    return type(reason).__name__


class UrllibTransport:
    """Stdlib transport: never raises on HTTP status codes, maps connect errors to TransportFailure."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        """Perform one request and return the response whatever its status."""
        failure: str | None = None
        try:
            host = (urlsplit(url).hostname or "").lower()
            opener = self._direct_opener if host in _LOOPBACK_HOSTS else self._opener
            req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
            try:
                with opener.open(req, timeout=timeout) as resp:
                    return HttpResponse(
                        status=int(resp.status),
                        headers=_lower_headers(resp.headers),
                        body=resp.read(MAX_RESPONSE_BYTES),
                    )
            except urllib.error.HTTPError as err:
                try:
                    data = err.read(MAX_RESPONSE_BYTES)
                except (OSError, http.client.HTTPException):
                    data = b""
                finally:
                    err.close()
                return HttpResponse(status=int(err.code), headers=_lower_headers(err.headers), body=data)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            failure = _describe_failure(exc)
        # Raised outside the except block so the original exception is not kept as __context__.
        raise TransportFailure(failure)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def retries_phrase(count: int) -> str:
    """Return '1 retry' or '<n> retries' for error text."""
    return "1 retry" if count == 1 else f"{count} retries"


def _retry_delay(response: HttpResponse | None, attempt: int, rng: Callable[[], float]) -> float:
    """Seconds to wait before retry number attempt + 1."""
    if response is not None:
        raw = _header(response.headers, "retry-after")
        if raw is not None and _NUMERIC_SECONDS.fullmatch(raw.strip()):
            return min(float(raw.strip()), MAX_RETRY_AFTER_SECONDS)
    base = min(2 ** min(attempt, 10), MAX_BACKOFF_BASE_SECONDS)
    return base * (1 + 0.1 * rng())


def send_with_retries(
    transport: Transport,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    *,
    timeout: float,
    max_retries: int,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> HttpResponse:
    """Send a request, retrying transport failures and 429/500/502/503/504 with backoff."""
    retries = max(0, int(max_retries))
    attempt = 0
    while True:
        try:
            response = transport.request(method, url, headers, body, timeout)
        except TransportFailure:
            if attempt >= retries:
                raise
            sleep(_retry_delay(None, attempt, rng))
            attempt += 1
            continue
        if response.status in RETRY_STATUSES and attempt < retries:
            sleep(_retry_delay(response, attempt, rng))
            attempt += 1
            continue
        return response


# ---------------------------------------------------------------------------
# Setting helpers shared by the providers
# ---------------------------------------------------------------------------


def setting_text(settings: Any, field_name: str, default: str = "") -> str:
    """Read a string setting, treating None and blank as the default."""
    value = getattr(settings, field_name, None)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def clean_api_key(raw: str, env_name: str) -> str:
    """Validate an API key shape without ever echoing it."""
    if not _KEY_CHARS.fullmatch(raw):
        raise ProviderConfigError(
            f"{env_name} contains spaces, line breaks or non-ASCII characters. "
            "Re-copy the key into your .env file without quotes or extra whitespace."
        )
    return raw


def normalize_base_url(raw: str, env_name: str) -> str:
    """Require an http(s) URL with a host and strip trailing slashes."""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ProviderConfigError(f"{env_name} must be a full http(s) URL such as https://host/v1")
    return raw.rstrip("/")


def timeout_setting(settings: Any, field_name: str, env_name: str, default: float) -> float:
    """Read a positive timeout in seconds."""
    value = getattr(settings, field_name, None)
    if value is None or value == "":
        return float(default)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ProviderConfigError(f"{env_name} must be a number of seconds") from None
    if not seconds > 0 or seconds != seconds or seconds == float("inf"):
        raise ProviderConfigError(f"{env_name} must be a positive number of seconds")
    return seconds


def retries_setting(settings: Any, field_name: str, env_name: str, default: int) -> int:
    """Read a non-negative retry count."""
    value = getattr(settings, field_name, None)
    if value is None or value == "":
        return int(default)
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ProviderConfigError(f"{env_name} must be a whole number") from None
    if count < 0:
        raise ProviderConfigError(f"{env_name} must be zero or greater")
    return count


def is_no_hook(text: str) -> bool:
    """True when the whole reply is NO_HOOK, ignoring whitespace, surrounding quotes and one trailing period."""
    cleaned = text.strip().strip(_QUOTE_CHARS).strip()
    if cleaned.endswith("."):
        cleaned = cleaned[:-1]
    return cleaned.strip().strip(_QUOTE_CHARS).strip() == "NO_HOOK"


def parse_json(body: bytes, label: str) -> Any:
    """Decode a JSON body or raise ProviderResponseError."""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        pass
    raise ProviderResponseError(f"{label} returned a response that is not valid JSON")


def error_details(body: bytes) -> tuple[dict, str]:
    """Best-effort (error object, message) from an error body; never raises."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}, ""
    if not isinstance(data, dict):
        return {}, ""
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        return err, msg if isinstance(msg, str) else ""
    if isinstance(err, str):
        return {}, err
    msg = data.get("message")
    return {}, msg if isinstance(msg, str) else ""


class HttpProviderBase:
    """Shared request, retry, error mapping and key hygiene for HTTP providers."""

    name = ""
    # Starts a sentence in most messages, so a leading article is capitalized here;
    # _inline_label gives the mid-sentence form ("Could not reach the ...").
    label = "The provider"
    auth_hint = ""
    credit_default = "The provider reported that the account has no remaining credit."

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transport: Transport | None,
        sleep: Callable[[float], None],
        rng: Callable[[], float],
        timeout: float,
        max_retries: int,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._transport: Transport = transport if transport is not None else UrllibTransport()
        self._sleep = sleep
        self._rng = rng
        self._timeout = timeout
        self._max_retries = max_retries

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model!r}, api_key='***')"

    @property
    def _inline_label(self) -> str:
        """The label for use inside a sentence: a leading "The " becomes "the "."""
        label = self.label
        return "the " + label[4:] if label.startswith("The ") else label

    @property
    def model(self) -> str:
        """The model name sent with each request."""
        return self._model

    # -- text hygiene ------------------------------------------------------

    def _redact(self, text: str) -> str:
        """Replace the API key with *** wherever it appears."""
        key = self._api_key
        if key and len(key) >= _MIN_REDACT_KEY_LENGTH:
            text = text.replace(key, "***")
        return text

    def _clean(self, text: str, limit: int = 300) -> str:
        """Redact the key, drop control characters and bound the length."""
        text = _CONTROL.sub(" ", self._redact(text or "")).strip()
        text = re.sub(r" {2,}", " ", text)
        return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."

    # -- request / response ------------------------------------------------

    def _post(self, url: str, headers: Mapping[str, str], payload: Mapping[str, Any]) -> HttpResponse:
        """POST a JSON payload with retries; maps connection failures to ProviderTransportError."""
        body = json.dumps(payload).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ProviderConfigError(
                f"The request body is {len(body)} bytes, over the {MAX_REQUEST_BYTES} byte limit. "
                "Shorten the contact facts or the sender instructions."
            )
        try:
            return send_with_retries(
                self._transport,
                "POST",
                url,
                headers,
                body,
                timeout=self._timeout,
                max_retries=self._max_retries,
                sleep=self._sleep,
                rng=self._rng,
            )
        except TransportFailure as exc:
            detail = self._clean(str(exc), 200)
        # Raised outside the except block so the unredacted TransportFailure is never chained.
        raise ProviderTransportError(
            f"Could not reach {self._inline_label} ({detail}) after {retries_phrase(self._max_retries)}."
        )

    def _credit_error(self, body: bytes) -> ProviderCreditError:
        """Build the 402 error with the optional top-up URL."""
        err, message = error_details(body)
        top_up = ""
        for field_name in ("top_up_url", "claim_url", "add_credit_url"):
            value = err.get(field_name)
            if isinstance(value, str) and value.strip():
                top_up = self._clean(value, 500)
                break
        return ProviderCreditError(self._clean(message) or self.credit_default, top_up)

    def _raise_for_status(self, response: HttpResponse) -> None:
        """Map a non-2xx response to the matching ProviderError subclass."""
        status = response.status
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise ProviderAuthError(f"{self.label} rejected the API key (HTTP {status}). {self.auth_hint}".strip())
        if status == 402:
            raise self._credit_error(response.body)
        _, message = error_details(response.body)
        detail = self._clean(message)
        suffix = f": {detail}" if detail else ""
        if status == 429:
            raise ProviderRateLimitError(
                f"{self.label} is rate limiting requests (HTTP 429) after {retries_phrase(self._max_retries)}{suffix}. "
                "Wait a little and rerun."
            )
        if status >= 500:
            retried = f" after {retries_phrase(self._max_retries)}" if status in RETRY_STATUSES else ""
            raise ProviderTransportError(f"{self.label} returned HTTP {status}{retried}{suffix}")
        raise ProviderError(f"{self.label} returned HTTP {status}{suffix}")

    def _hook_from_text(self, text: str, model: str, meta: Mapping[str, MetaValue] | None = None) -> Hook:
        """Validate model output and wrap it as a Hook."""
        text = self._redact(text or "").strip()
        if not text:
            raise ProviderResponseError(f"{self.label} returned an empty response")
        if is_no_hook(text):
            raise InsufficientFacts(f"{self.label} found the facts too thin for a sincere hook (NO_HOOK)")
        return Hook(text=text, provider=self.name, model=self._redact(model), meta=dict(meta or {}))

    def personalize(self, request: PersonalizationRequest) -> Hook:  # pragma: no cover - overridden
        """Return a hook for the request."""
        raise NotImplementedError
