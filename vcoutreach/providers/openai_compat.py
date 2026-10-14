"""OpenAI-compatible bring-your-own-key provider."""
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
    normalize_base_url,
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

DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_TIMEOUT_SECONDS = 60.0
MISSING_MODEL_MESSAGE = (
    "OPENAI_MODEL is not set. Set it to the model name you want to use (see your provider's model list)."
)


def extract_chat_completion_text(body: bytes, label: str) -> str:
    """Return choices[0].message.content from a chat completions body, or raise ProviderResponseError."""
    data: Any = parse_json(body, label)
    content = None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        pass
    if content is None:
        raise ProviderResponseError(f"{label} returned a response without choices[0].message.content")
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        content = "".join(part for part in parts if isinstance(part, str))
    if not isinstance(content, str) or not content.strip():
        raise ProviderResponseError(f"{label} returned an empty response")
    return content.strip()


class OpenAICompatProvider(HttpProviderBase):
    """Personalization through any OpenAI-compatible chat completions endpoint."""

    name = "openai"
    label = "The OpenAI-compatible endpoint"
    auth_hint = "Check OPENAI_API_KEY."
    credit_default = "The OpenAI-compatible endpoint reported that the account has no remaining credit."

    def __init__(
        self,
        settings: "Settings",
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        raw_key = setting_text(settings, "openai_api_key")
        if not raw_key:
            raise ProviderConfigError(
                "OPENAI_API_KEY is not set. Add it to your .env file, or use --provider template for the free offline mode."
            )
        model = setting_text(settings, "openai_model")
        if not model:
            raise ProviderConfigError(MISSING_MODEL_MESSAGE)
        base = normalize_base_url(setting_text(settings, "openai_base_url", DEFAULT_BASE_URL), "OPENAI_BASE_URL")
        super().__init__(
            api_key=clean_api_key(raw_key, "OPENAI_API_KEY"),
            model=model,
            transport=transport,
            sleep=sleep,
            rng=rng,
            timeout=OPENAI_TIMEOUT_SECONDS,
            max_retries=DEFAULT_MAX_RETRIES,
        )
        self._url = base + "/chat/completions"

    def personalize(self, request: PersonalizationRequest) -> Hook:
        """Ask the endpoint for a hook grounded in the contact's facts."""
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
        return self._hook_from_text(text, self._model)

