"""Optional Gmail sending behind strict safeguards.

Names are re-exported lazily (PEP 562) so importing a submodule such as
``vcoutreach.send.mime`` never pulls in the rest of the package.
"""
from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "CONFIRM_LIVE_TOKEN": "gate",
    "MailTransport": "gate",
    "PlannedSend": "gate",
    "SendGateError": "gate",
    "SendReceipt": "gate",
    "SendReport": "gate",
    "SkippedSend": "gate",
    "plan_sends": "gate",
    "build_message": "mime",
    "GmailTransport": "gmail",
    "authorize": "gmail",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
