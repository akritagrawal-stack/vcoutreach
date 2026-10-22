"""The single redaction layer for the MCP server.

Every tool result, every JSON-RPC error message and every stderr line passes
through ``redact`` or ``redact_text`` before it leaves the process. Redaction is
defense in depth: no tool returns an environment value at all, so this layer only
matters when something unexpected leaks (an exception message that embeds a key,
a library that echoes a header, a handler bug).

What is removed:

* any string containing a currently registered secret value of 8 or more
  characters (values of the ``*_KEY``, ``*_TOKEN``, ``*_SECRET`` settings, the
  Flask secret, every secret-looking name in the workspace ``.env``, and the
  string contents of the Gmail token, OAuth client and service account files);
* any dict value whose key looks like ``api_key``, ``token``, ``secret``,
  ``password`` or ``authorization`` (booleans and ``None`` are kept, because they
  carry no secret and the tools report connection state as booleans);
* ``.env``-shaped text (``NAME=value`` for a name in ``KEY_NAMES``);
* a few well-known token shapes (Bearer headers, ``sk-`` keys, Google keys).

The registry is rebuilt on every call and never cached across calls.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

REDACTED = "[redacted]"
MIN_SECRET_LENGTH = 8
_MAX_DEPTH = 40
_MAX_SECRET_FILE_BYTES = 64 * 1024

SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)

_SECRET_FILES = (
    ("data", ".gmail_token.json"),
    ("data", ".gmail_credentials.json"),
    ("data", ".gcp_service_account.json"),
)
_SKIP_JSON_KEYS = frozenset({
    "token_uri", "auth_uri", "auth_provider_x509_cert_url", "client_x509_cert_url",
    "type", "universe_domain", "scopes", "scope", "expiry",
})
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET")

_SHAPES = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{30,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])ya29\.[0-9A-Za-z_-]{20,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])1//[0-9A-Za-z_-]{20,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),
)


def _key_names() -> frozenset:
    try:
        from vcoutreach.web.keys import KEY_NAMES
        return frozenset(KEY_NAMES) | {"FLASK_SECRET_KEY"}
    except Exception:  # pragma: no cover - the web package is always present
        return frozenset({"FLASK_SECRET_KEY"})


def _env_line_re(names: Iterable[str]) -> re.Pattern:
    alternation = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    return re.compile(r"(?:\bexport[ \t]+)?\b(?:%s)[ \t]*=[^\n]*" % alternation)


def _json_string_leaves(node: Any, out: set, depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in _SKIP_JSON_KEYS:
                continue
            _json_string_leaves(value, out, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _json_string_leaves(value, out, depth + 1)
    elif isinstance(node, str):
        text = node.strip()
        if (len(text) >= MIN_SECRET_LENGTH and not text.lower().startswith(("http://", "https://"))
                and "@" not in text):
            out.add(text)


def secret_values(workspace=None) -> tuple:
    """Return the current secret strings, longest first. Never raises."""
    found: set = set()
    try:
        if workspace is None:
            from vcoutreach.web.config import get_workspace
            workspace = get_workspace()
        names = _key_names()
        secret_names = {n for n in names if n.endswith(_SECRET_SUFFIXES)}
        try:
            env = dict(workspace.env())
        except Exception:
            env = {}
        for name, value in env.items():
            if isinstance(value, str) and (name in secret_names or SECRET_KEY_RE.search(name)):
                found.add(value.strip())
        for name in secret_names:
            value = os.environ.get(name)
            if value:
                found.add(value.strip())
        for parts in _SECRET_FILES:
            try:
                path = workspace.path(*parts)
                if not path.is_file() or path.stat().st_size > _MAX_SECRET_FILE_BYTES:
                    continue
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            try:
                _json_string_leaves(json.loads(raw), found)
            except ValueError:
                stripped = raw.strip()
                if stripped and "\n" not in stripped:
                    found.add(stripped)
    except Exception:
        pass
    values = [v for v in found if isinstance(v, str) and len(v) >= MIN_SECRET_LENGTH]
    return tuple(sorted(values, key=len, reverse=True))


def _scrub(text: str, secrets: tuple, env_re: re.Pattern) -> str:
    if not text:
        return text
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    text = env_re.sub(REDACTED, text)
    for pattern, replacement in _SHAPES:
        text = pattern.sub(replacement, text)
    return text


def _walk(obj: Any, secrets: tuple, env_re: re.Pattern, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(obj, str):
        return _scrub(obj, secrets, env_re)
    if isinstance(obj, (bytes, bytearray)):
        return _scrub(bytes(obj).decode("utf-8", errors="replace"), secrets, env_re)
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            safe_key = _scrub(key, secrets, env_re) if isinstance(key, str) else key
            if isinstance(key, str) and SECRET_KEY_RE.search(key) and not (value is None or isinstance(value, bool)):
                out[safe_key] = REDACTED
            else:
                out[safe_key] = _walk(value, secrets, env_re, depth + 1)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_walk(item, secrets, env_re, depth + 1) for item in obj]
    return obj


def redact(obj: Any, *, secrets: tuple | None = None) -> Any:
    """Return a copy of ``obj`` with secrets removed. Never raises, never mutates."""
    try:
        values = secret_values() if secrets is None else secrets
        return _walk(obj, values, _env_line_re(_key_names()), 0)
    except Exception:
        return REDACTED


def redact_text(text: Any, *, secrets: tuple | None = None) -> str:
    """Redact a string (log line, error message). Always returns ``str``."""
    result = redact(text if isinstance(text, str) else str(text), secrets=secrets)
    return result if isinstance(result, str) else REDACTED


def wrap_untrusted(text: Any, source: str) -> dict:
    """Mark scraped, imported or model-written text as data, not instructions."""
    return {
        "source": str(source),
        "untrusted": True,
        "text": "" if text is None else str(text),
    }


def untrusted_sources(obj: Any, _depth: int = 0) -> list:
    """Return the ordered, de-duplicated ``source`` of every wrapped value in ``obj``."""
    found: list = []
    if _depth > _MAX_DEPTH:
        return found
    if isinstance(obj, dict):
        if obj.get("untrusted") is True and isinstance(obj.get("source"), str):
            found.append(obj["source"])
        for value in obj.values():
            found.extend(untrusted_sources(value, _depth + 1))
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found.extend(untrusted_sources(value, _depth + 1))
    seen: list = []
    for source in found:
        if source not in seen:
            seen.append(source)
    return seen
