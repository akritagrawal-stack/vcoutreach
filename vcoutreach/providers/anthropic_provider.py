"""Anthropic bring-your-own-key provider."""
from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any, Callable

from vcoutreach.prompts import build_messages
from vcoutreach.providers.http import (
    DEFAULT_MAX_RETRIES,
    USER_AGENT,
    HttpProviderBase,
    Transport,
    clean_api_key,
    parse_json,
    setting_text,
)
from vcoutreach.types import (
    Hook,
    PersonalizationRequest,
    ProviderConfigError,
    ProviderResponseError,
)

if TYPE_CHECKING:
    from vcoutreach.config import Settings

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
ANTHROPIC_TIMEOUT_SECONDS = 60.0
MISSING_MODEL_MESSAGE = (
    "ANTHROPIC_MODEL is not set. Set it to the model name you want to use (see your provider's model list)."
)


def _extract_text(body: bytes, label: str) -> str:
    """Return the first text block of a Messages API response, or raise ProviderResponseError."""
    data: Any = parse_json(body, label)
    blocks = data.get("content") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        raise ProviderResponseError(f"{label} returned a response without a content list")
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text = block["text"].strip()
            if text:
                return text
    raise ProviderResponseError(f"{label} returned an empty response")


class AnthropicProvider(HttpProviderBase):
    """Personalization through the Anthropic Messages API with the user's own key."""

    name = "anthropic"
    label = "Anthropic"
    auth_hint = "Check ANTHROPIC_API_KEY."
    credit_default = "Anthropic reported that the account has no remaining credit."

    def __init__(
        self,
        settings: "Settings",
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        raw_key = setting_text(settings, "anthropic_api_key")
        if not raw_key:
            raise ProviderConfigError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file, or use --provider template for the free offline mode."
            )
        model = setting_text(settings, "anthropic_model")
        if not model:
            raise ProviderConfigError(MISSING_MODEL_MESSAGE)
        super().__init__(
            api_key=clean_api_key(raw_key, "ANTHROPIC_API_KEY"),
            model=model,
            transport=transport,
            sleep=sleep,
            rng=rng,
            timeout=ANTHROPIC_TIMEOUT_SECONDS,
            max_retries=DEFAULT_MAX_RETRIES,
        )

    def personalize(self, request: PersonalizationRequest) -> Hook:
        """Ask Claude for a hook grounded in the contact's facts."""
        messages = build_messages(request)
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        chat = [m for m in messages if m["role"] != "system"]
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        payload = {
            "model": self._model,
            "max_tokens": 300,
            "system": system,
            "messages": chat,
        }
        response = self._post(API_URL, headers, payload)
        self._raise_for_status(response)
        return self._hook_from_text(_extract_text(response.body, self.label), self._model)
