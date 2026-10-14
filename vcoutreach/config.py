"""Settings loading from the environment and an optional .env file."""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from .types import OutreachError

DEFAULT_PUBLIK_BASE_URL = "https://publikhq.com/api/v1"
DEFAULT_PUBLIK_MODEL = "publik-balanced"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"

_SECRET_FIELDS = ("publik_api_key", "openai_api_key", "anthropic_api_key")
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXPORT_RE = re.compile(r"^export\s+")


def _home_path(*parts: str) -> Path:
    try:
        return Path("~").expanduser().joinpath(*parts)
    except RuntimeError:  # no resolvable home directory
        return Path(*parts)


def _default_token_path() -> Path:
    return _home_path(".config", "vcoutreach", "token.json")


def _default_client_secrets_path() -> Path:
    return _home_path(".config", "vcoutreach", "client_secret.json")


@dataclass(frozen=True, repr=False)
class Settings:
    """Runtime settings. Keys are never shown by repr or str."""

    send_mode: str = "test"
    test_recipient_email: str = ""
    daily_send_cap: int = 20
    sender_email: str = ""
    publik_api_key: str = ""
    publik_api_base_url: str = DEFAULT_PUBLIK_BASE_URL
    publik_model: str = DEFAULT_PUBLIK_MODEL
    publik_timeout_seconds: float = 30.0
    publik_max_retries: int = 3
    openai_api_key: str = ""
    openai_base_url: str = DEFAULT_OPENAI_BASE_URL
    openai_model: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    workdir: Path = Path(".")
    gmail_token_path: Path = field(default_factory=_default_token_path)
    gmail_client_secrets_path: Path = field(default_factory=_default_client_secrets_path)

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):
            value = "***" if f.name in _SECRET_FIELDS else getattr(self, f.name)
            parts.append("%s=%r" % (f.name, value))
        return "Settings(%s)" % ", ".join(parts)

    def __str__(self) -> str:
        return self.__repr__()


def parse_dotenv(text: str, warn: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, blank lines, quoted values and a leading export.

    ``warn`` receives one message per malformed line (never the value).
    """
    values: Dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _EXPORT_RE.sub("", line, count=1)
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            if warn is not None:
                warn(".env line %d ignored: not KEY=VALUE" % number)
            continue
        values[key] = _clean_dotenv_value(value)
    return values


def _clean_dotenv_value(raw: str) -> str:
    # Leading whitespace is kept for the unquoted path so that `KEY= # note` is an empty value
    # followed by a comment, not the value "# note".
    value = raw.lstrip() if raw.lstrip()[:1] in ("'", '"') else raw
    if value and value[0] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            return value[1:].strip()
        return value[1:end]
    # Unquoted: an inline comment starts at whitespace followed by '#'.
    return re.sub(r"\s+#.*$", "", value).strip()


def _read_dotenv(path: Path, warn: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise OutreachError("cannot read %s: %s" % (path, exc.__class__.__name__))
    return parse_dotenv(text, warn)


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = None,
    warn: Optional[Callable[[str], None]] = None,
) -> Settings:
    """Build Settings from a .env file plus the environment (environment wins).

    With dotenv_path None the file ``.env`` in the current directory is used when it
    exists. Nothing is exported into os.environ.
    """
    path = Path(".env") if dotenv_path is None else Path(dotenv_path)
    merged: Dict[str, str] = dict(_read_dotenv(path, warn))
    source = os.environ if env is None else env
    for k, v in source.items():
        merged[str(k)] = str(v)

    def text(name: str, default: str = "") -> str:
        raw = merged.get(name, "").strip()
        return raw if raw else default

    def integer(name: str, default: int) -> int:
        raw = merged.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise OutreachError("%s must be a whole number, got %r" % (name, raw))
        if value < 0:
            raise OutreachError("%s must not be negative, got %r" % (name, raw))
        return value

    def number(name: str, default: float) -> float:
        raw = merged.get(name, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise OutreachError("%s must be a number, got %r" % (name, raw))
        if not math.isfinite(value) or value <= 0:
            raise OutreachError("%s must be greater than zero, got %r" % (name, raw))
        return value

    def filepath(name: str, default: Path) -> Path:
        raw = text(name)
        return Path(raw).expanduser() if raw else default

    send_mode = text("SEND_MODE", "test")
    if send_mode not in ("test", "live"):
        raise OutreachError("SEND_MODE must be 'test' or 'live', got %r" % send_mode)

    return Settings(
        send_mode=send_mode,
        # Passed through raw (no strip) so the send gate can reject whitespace or line breaks.
        test_recipient_email=merged.get("TEST_RECIPIENT_EMAIL", ""),
        daily_send_cap=integer("DAILY_SEND_CAP", 20),
        sender_email=text("SENDER_EMAIL"),
        publik_api_key=text("PUBLIK_API_KEY"),
        publik_api_base_url=text("PUBLIK_API_BASE_URL", DEFAULT_PUBLIK_BASE_URL),
        publik_model=text("PUBLIK_MODEL", DEFAULT_PUBLIK_MODEL),
        publik_timeout_seconds=number("PUBLIK_TIMEOUT_SECONDS", 30.0),
        publik_max_retries=integer("PUBLIK_MAX_RETRIES", 3),
        openai_api_key=text("OPENAI_API_KEY"),
        openai_base_url=text("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
        openai_model=text("OPENAI_MODEL"),
        anthropic_api_key=text("ANTHROPIC_API_KEY"),
        anthropic_model=text("ANTHROPIC_MODEL"),
        workdir=Path(text("WORKDIR") or text("VCOUTREACH_WORKDIR") or "."),
        gmail_token_path=filepath("GMAIL_TOKEN_PATH", _default_token_path()),
        gmail_client_secrets_path=filepath(
            "GMAIL_CLIENT_SECRETS_PATH", _default_client_secrets_path()
        ),
    )
