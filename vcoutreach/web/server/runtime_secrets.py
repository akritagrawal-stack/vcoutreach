"""Workspace credentials with environment and optional macOS Keychain reads."""
from __future__ import annotations

from pathlib import Path
import subprocess
import os
import sys
import tempfile
from vcoutreach.web.vendor import workspace_path, setting_value, credential_value
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
KEYCHAIN_HELPER = ROOT / "scripts" / "publik_macos_keychain.swift"
GEMINI_SERVICE = "outreach-signal.gemini-api-key.v1"
GEMINI_ACCOUNT = "local-dashboard"
MAX_SECRET_BYTES = 8192

MODEL_PROVIDERS = {
    "gemini": {
        "label": "Google Gemini",
        "service": GEMINI_SERVICE,
        "account": GEMINI_ACCOUNT,
        "env_var": "GEMINI_API_KEY",
        "default_model": "gemini-3.5-flash-lite",
    },
    "openai": {
        "label": "OpenAI",
        "service": "outreach-signal.openai-api-key.v1",
        "account": "local-dashboard",
        "env_var": "OPENAI_API_KEY",
        "default_model": "Set in your command",
    },
    "anthropic": {
        "label": "Anthropic",
        "service": "outreach-signal.anthropic-api-key.v1",
        "account": "local-dashboard",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "Set in your command",
    },
    "openrouter": {
        "label": "OpenRouter",
        "service": "outreach-signal.openrouter-api-key.v1",
        "account": "local-dashboard",
        "env_var": "OPENROUTER_API_KEY",
        "default_model": "Set in your command",
    },
}

DATA_PROVIDERS = {
    "enrow": {
        "label": "Enrow",
        "service": "outreach-signal.enrow-api-key.v1",
        "account": "local-dashboard",
        "env_var": "ENROW_API_KEY",
        "capability": "Named-contact email finding",
        "credit_note": "Each lookup may consume an Enrow credit.",
    },
    "apollo": {
        "label": "Apollo",
        "service": "outreach-signal.apollo-api-key.v1",
        "account": "local-dashboard",
        "env_var": "APOLLO_API_KEY",
        "capability": "Person enrichment and work-email evidence",
        "credit_note": "Access and credit use depend on the Apollo plan.",
    },
}


class RuntimeSecretError(ValueError):
    """A content-free secret boundary failure."""


def validate_secret(value: str) -> str:
    try:
        return credential_value(value)
    except ValueError as exc:
        raise RuntimeSecretError("api_key_invalid") from exc


def _run(
    command: list[str],
    *,
    input_value: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            input=input_value,
            stdin=subprocess.DEVNULL if input_value is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeSecretError("keychain_unavailable") from exc


def _provider_spec(provider: str) -> dict[str, str]:
    normalized = str(provider or "").strip().lower()
    spec = MODEL_PROVIDERS.get(normalized)
    if spec is None:
        raise RuntimeSecretError("provider_invalid")
    return spec


def _data_provider_spec(provider: str) -> dict[str, str]:
    normalized = str(provider or "").strip().lower()
    spec = DATA_PROVIDERS.get(normalized)
    if spec is None:
        raise RuntimeSecretError("provider_invalid")
    return spec


def _load_key(spec: dict[str, str], runner=None) -> str | None:
    value = setting_value(spec["env_var"])
    if not value and spec["env_var"] == "GEMINI_API_KEY":
        value = setting_value("GOOGLE_API_KEY")
    if value:
        return validate_secret(value)
    if runner is None and sys.platform != "darwin":
        return None
    result = (runner or _run)(["/usr/bin/swift", str(KEYCHAIN_HELPER), "load", spec["service"], spec["account"]])
    if result.returncode == 2 and result.stderr.strip() == b"keychain_item_missing":
        return None
    if result.returncode != 0:
        raise RuntimeSecretError("keychain_load_failed")
    try:
        return validate_secret(result.stdout.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise RuntimeSecretError("keychain_load_failed") from exc


def _store_key(spec: dict[str, str], value: str) -> None:
    key = validate_secret(value)
    path = workspace_path(".env")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    name = spec["env_var"]
    lines = [line for line in lines if line.split("=", 1)[0].strip() != name]
    lines.append(name + "=" + key)
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_provider_key(provider: str, *, runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None) -> str | None:
    return _load_key(_provider_spec(provider), runner)


def store_provider_key(provider: str, value: str, *, runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None) -> None:
    _store_key(_provider_spec(provider), value)


def load_data_provider_key(provider: str, *, runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None) -> str | None:
    return _load_key(_data_provider_spec(provider), runner)


def store_data_provider_key(provider: str, value: str, *, runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None) -> None:
    _store_key(_data_provider_spec(provider), value)


def provider_key_configured(provider: str) -> bool:
    try:
        return load_provider_key(provider) is not None
    except RuntimeSecretError:
        return False


def provider_statuses() -> list[dict[str, object]]:
    return [
        {
            "id": provider,
            "label": spec["label"],
            "env_var": spec["env_var"],
            "default_model": spec["default_model"],
            "configured": provider_key_configured(provider),
        }
        for provider, spec in MODEL_PROVIDERS.items()
    ]


def data_provider_key_configured(provider: str) -> bool:
    try:
        return load_data_provider_key(provider) is not None
    except RuntimeSecretError:
        return False


def data_provider_statuses() -> list[dict[str, object]]:
    return [
        {
            "id": provider,
            "label": spec["label"],
            "env_var": spec["env_var"],
            "capability": spec["capability"],
            "credit_note": spec["credit_note"],
            "configured": data_provider_key_configured(provider),
        }
        for provider, spec in DATA_PROVIDERS.items()
    ]


def load_gemini_key(
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> str | None:
    return load_provider_key("gemini", runner=runner)


def store_gemini_key(
    value: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
) -> None:
    store_provider_key("gemini", value, runner=runner)


def gemini_key_configured() -> bool:
    return provider_key_configured("gemini")


__all__ = [
    "DATA_PROVIDERS",
    "RuntimeSecretError",
    "MODEL_PROVIDERS",
    "data_provider_key_configured",
    "data_provider_statuses",
    "gemini_key_configured",
    "load_data_provider_key",
    "load_provider_key",
    "load_gemini_key",
    "provider_key_configured",
    "provider_statuses",
    "store_data_provider_key",
    "store_provider_key",
    "store_gemini_key",
    "validate_secret",
]
