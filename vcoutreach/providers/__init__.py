"""Personalization provider resolution."""
from __future__ import annotations

from typing import TYPE_CHECKING

from vcoutreach.providers.anthropic_provider import AnthropicProvider
from vcoutreach.providers.http import (
    HttpResponse,
    Transport,
    TransportFailure,
    UrllibTransport,
    send_with_retries,
    setting_text,
)
from vcoutreach.providers.openai_compat import OpenAICompatProvider
from vcoutreach.providers.publik import PublikProvider
from vcoutreach.providers.template_provider import TemplateProvider
from vcoutreach.types import PersonalizationProvider, ProviderConfigError

if TYPE_CHECKING:
    from vcoutreach.config import Settings

__all__ = [
    "PROVIDER_NAMES",
    "AnthropicProvider",
    "HttpResponse",
    "OpenAICompatProvider",
    "PublikProvider",
    "TemplateProvider",
    "Transport",
    "TransportFailure",
    "UrllibTransport",
    "configured_providers",
    "missing_variable",
    "resolve_provider",
    "send_with_retries",
]

PROVIDER_NAMES = ("auto", "template", "publik", "openai", "anthropic")

_KEY_FIELDS = {
    "publik": "publik_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
}


_MODEL_FIELDS = {
    "openai": ("openai_model", "OPENAI_MODEL"),
    "anthropic": ("anthropic_model", "ANTHROPIC_MODEL"),
}
_KEY_VARIABLES = {
    "publik": "PUBLIK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def missing_variable(settings: "Settings", name: str) -> str:
    """Name of the first environment variable that keeps provider name unusable, or '' when it is usable.

    A key with no model (openai, anthropic) reports the model variable, since those models have no default.
    """
    if name not in _KEY_FIELDS:
        return ""
    if not setting_text(settings, _KEY_FIELDS[name]):
        return _KEY_VARIABLES[name]
    model_field, model_variable = _MODEL_FIELDS.get(name, ("", ""))
    if model_field and not setting_text(settings, model_field):
        return model_variable
    return ""


def configured_providers(settings: "Settings") -> list[str]:
    """Names of providers usable with these settings; template is always included."""
    names = [name for name in _KEY_FIELDS if not missing_variable(settings, name)]
    return ["template"] + names


def resolve_provider(
    settings: "Settings", name: str = "auto", transport: Transport | None = None
) -> PersonalizationProvider:
    """Build the provider called name; auto picks publik, then openai, then anthropic, then template."""
    choice = (name or "auto").strip().lower()
    if choice == "auto":
        for candidate in ("publik", "openai", "anthropic"):
            if setting_text(settings, _KEY_FIELDS[candidate]):
                choice = candidate
                break
        else:
            choice = "template"
    if choice == "template":
        return TemplateProvider()
    if choice == "publik":
        return PublikProvider(settings, transport)
    if choice == "openai":
        return OpenAICompatProvider(settings, transport)
    if choice == "anthropic":
        return AnthropicProvider(settings, transport)
    raise ProviderConfigError(
        f"Unknown provider {str(name)[:40]!r}. Valid names: " + ", ".join(PROVIDER_NAMES)
    )
