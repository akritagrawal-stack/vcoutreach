"""Sender profile JSON loader."""
from __future__ import annotations

import json
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

from .types import SenderProfile, SenderProfileError

_SUBJECT_FIELDS = frozenset({"first_name", "org"})


@dataclass(frozen=True)
class SenderProfileWithSubject(SenderProfile):
    """SenderProfile plus the optional ``subject_template`` from the JSON file.

    load_sender returns this subclass only when the file sets the legacy
    ``subject_template`` field. Everything else receives a plain SenderProfile.
    """

    subject_template: str = ""


_DASHES = ("\u2014", "\u2013")


def _dash_path(value: Any, path: str) -> str:
    """Dotted path of the first string inside ``value`` (at any depth) holding an em dash or
    en dash, or '' when there is none. List items add no index: the list's own path is used."""
    if isinstance(value, str):
        return path if any(dash in value for dash in _DASHES) else ""
    if isinstance(value, list):
        for item in value:
            found = _dash_path(item, path)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        for key, item in value.items():
            found = _dash_path(item, "%s.%s" % (path, key) if path else str(key))
            if found:
                return found
    return ""


def _reject_dashes(data: Dict[str, Any], label: str) -> None:
    """Refuse an em dash or en dash in any string value, naming its dotted field path
    (for example ``approved_claims.metric``)."""
    field = _dash_path(data, "")
    if field:
        raise SenderProfileError(
            "sender file %s: field '%s' contains an em dash or en dash" % (label, field)
        )


def _text(data: Dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SenderProfileError("%s: %r must be a string" % (label, key))
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _text_list(data: Dict[str, Any], key: str, label: str) -> Tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SenderProfileError("%s: %r must be an array of strings" % (label, key))
    items = []
    for item in value:
        if not isinstance(item, str):
            raise SenderProfileError("%s: every entry of %r must be a string" % (label, key))
        item = item.strip()
        if item:
            items.append(item)
    return tuple(items)


def _claims(data: Dict[str, Any], label: str) -> Dict[str, str]:
    value = data.get("approved_claims")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SenderProfileError("%s: 'approved_claims' must be an object" % label)
    claims: Dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise SenderProfileError(
                "%s: approved_claims value for %r must be a string or number" % (label, key)
            )
        claims[str(key)] = str(item).strip()
    return claims


def _check_subject_template(template: str, label: str) -> None:
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise SenderProfileError("%s: 'subject_template' is malformed: %s" % (label, exc))
    for _literal, field_name, spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in _SUBJECT_FIELDS or spec or conversion:
            raise SenderProfileError(
                "%s: 'subject_template' may only use {first_name} and {org}" % label
            )


def _first_touch_subject(data: Dict[str, Any], label: str) -> str:
    value = data.get("first_touch_subject", "")
    if not isinstance(value, str):
        raise SenderProfileError("%s: 'first_touch_subject' must be a string" % label)
    if len(value) > 120:
        raise SenderProfileError("%s: 'first_touch_subject' must be at most 120 characters" % label)
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in "\x85\u2028\u2029" for ch in value):
        raise SenderProfileError("%s: 'first_touch_subject' must be a single line without control characters" % label)
    if "!" in value:
        raise SenderProfileError("%s: 'first_touch_subject' must not contain exclamation marks" % label)
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError as exc:
        raise SenderProfileError("%s: 'first_touch_subject' is malformed: %s" % (label, exc))
    for _literal, field_name, spec, conversion in parsed:
        if field_name is not None and (field_name != "first_name" or spec or conversion):
            raise SenderProfileError("%s: 'first_touch_subject' may only use {first_name}" % label)
    return value.strip()


def load_sender(path: Path) -> SenderProfile:
    """Read a sender profile JSON file. ``name`` is required."""
    path = Path(path)
    label = path.name
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise SenderProfileError("sender file not found: %s" % path)
    except IsADirectoryError:
        raise SenderProfileError("sender path is a directory, expected a JSON file: %s" % path)
    except UnicodeDecodeError:
        raise SenderProfileError("%s is not valid UTF-8 text" % label)
    except OSError as exc:
        raise SenderProfileError("cannot read %s: %s" % (path, exc.strerror or exc.__class__.__name__))
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SenderProfileError("%s is not valid JSON: %s" % (label, exc))
    if not isinstance(data, dict):
        raise SenderProfileError("%s must contain a JSON object" % label)

    _reject_dashes(data, label)

    name = _text(data, "name", label)
    if not name:
        raise SenderProfileError("%s: 'name' is required" % label)

    fields: Dict[str, Any] = dict(
        name=name,
        title=_text(data, "title", label),
        org=_text(data, "org", label),
        website=_text(data, "website", label),
        signature=_text(data, "signature", label),
        product_lines=_text_list(data, "product_lines", label),
        traction_lines=_text_list(data, "traction_lines", label),
        ask_line=_text(data, "ask_line", label),
        approved_claims=_claims(data, label),
        forbidden_phrases=_text_list(data, "forbidden_phrases", label),
        first_touch_subject=_first_touch_subject(data, label),
    )

    subject_template = _text(data, "subject_template", label)
    if subject_template:
        _check_subject_template(subject_template, label)
        return SenderProfileWithSubject(subject_template=subject_template, **fields)
    return SenderProfile(**fields)
