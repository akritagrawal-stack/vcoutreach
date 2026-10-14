"""Publik API provider."""
from __future__ import annotations

import random
import re
import time
from typing import TYPE_CHECKING, Callable, Mapping

from vcoutreach.prompts import build_messages
from vcoutreach.providers.http import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    USER_AGENT,
    HttpProviderBase,
    Transport,
    clean_api_key,
    normalize_base_url,
    retries_setting,
    setting_text,
    timeout_setting,
)
from vcoutreach.providers.openai_compat import extract_chat_completion_text
from vcoutreach.types import (
    Hook,
    MetaValue,
    PersonalizationRequest,
    ProviderConfigError,
)

if TYPE_CHECKING:
    from vcoutreach.config import Settings

DASHBOARD_URL = "https://publikhq.com/dashboard/api"
DEFAULT_BASE_URL = "https://publikhq.com/api/v1"
DEFAULT_MODEL = "publik-balanced"
ALLOWED_MODELS = ("publik-fast", "publik-balanced", "publik-smart", "publik-default")

MISSING_KEY_MESSAGE = (
    "PUBLIK_API_KEY is not set. Create an API key at " + DASHBOARD_URL + " and add PUBLIK_API_KEY=... "
    "to your .env file, or use --provider template for the free offline mode."
)

_PLAIN_INTEGER = re.compile(r"[0-9]{1,30}")
_META_HEADERS = (
    ("x-publik-balance", "balance_micros"),
    ("x-publik-charge-micros", "charge_micros"),
)


def _meta_from_headers(headers: Mapping[str, str]) -> dict[str, MetaValue]:
    """Parse x-publik-* headers; anything but a plain non-negative integer string is ignored."""
    lowered = {str(key).lower(): value for key, value in headers.items()}
    meta: dict[str, MetaValue] = {}
    for header, field_name in _META_HEADERS:
        value = lowered.get(header)
        if isinstance(value, str) and _PLAIN_INTEGER.fullmatch(value):
            meta[field_name] = int(value)
    return meta


class PublikProvider(HttpProviderBase):
    """Personalization through the Publik API (OpenAI-style chat completions)."""

    name = "publik"
    label = "Publik"
    auth_hint = "Check PUBLIK_API_KEY or create a new key at " + DASHBOARD_URL
    credit_default = "Publik has no remaining credit for this request. Add credit at " + DASHBOARD_URL

    def __init__(
        self,
        settings: "Settings",
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        raw_key = setting_text(settings, "publik_api_key")
        if not raw_key:
            raise ProviderConfigError(MISSING_KEY_MESSAGE)
        model = setting_text(settings, "publik_model", DEFAULT_MODEL)
        if model not in ALLOWED_MODELS:
            raise ProviderConfigError(
                f"PUBLIK_MODEL {model!r} is not supported. Use one of: " + ", ".join(ALLOWED_MODELS)
            )
        base = normalize_base_url(
            setting_text(settings, "publik_api_base_url", DEFAULT_BASE_URL), "PUBLIK_API_BASE_URL"
        )
        super().__init__(
            api_key=clean_api_key(raw_key, "PUBLIK_API_KEY"),
            model=model,
            transport=transport,
            sleep=sleep,
            rng=rng,
            timeout=timeout_setting(settings, "publik_timeout_seconds", "PUBLIK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            max_retries=retries_setting(settings, "publik_max_retries", "PUBLIK_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )
        self._url = base + "/chat/completions"

    def personalize(self, request: PersonalizationRequest) -> Hook:
        """Ask Publik for a hook grounded in the contact's facts."""
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        payload = {
            "model": self._model,
            "messages": build_messages(request),
            "temperature": 0.4,
            "max_tokens": 300,
        }
        response = self._post(self._url, headers, payload)
        self._raise_for_status(response)
        text = extract_chat_completion_text(response.body, self.label)
        lowered = {str(key).lower(): value for key, value in response.headers.items()}
        served_model = lowered.get("x-publik-model")
        model = served_model.strip() if isinstance(served_model, str) and served_model.strip() else self._model
        return self._hook_from_text(text, model, _meta_from_headers(response.headers))
