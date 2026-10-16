"""contact_picker: pure function that maps a company signal to a target role."""

from __future__ import annotations

import re

# Stages that always map to "founder" regardless of headcount.
_SEED_PATTERN = re.compile(r"^(pre[-_]?seed|seed)$", re.IGNORECASE)

# Series A variants.
_SERIES_A_PATTERN = re.compile(r"^series[-_ ]?a$", re.IGNORECASE)

# Series B and above, plus late/growth labels.
_LATE_PATTERN = re.compile(
    r"^(series[-_ ]?[b-z]|growth|late)$", re.IGNORECASE
)

_HEADCOUNT_THRESHOLD = 20


def pick_contact_role(signal: dict) -> str:
    """Return the best contact role for a company based on funding signal.

    Parameters
    ----------
    signal:
        Dict with keys ``stage`` (str or None) and ``headcount`` (int or None).

    Returns
    -------
    str
        One of ``"founder"``, ``"head_of_growth"``, or ``"head_of_marketing"``.
    """
    stage: str = (signal.get("stage") or "").strip()
    headcount = signal.get("headcount")

    # Seed / pre-seed always returns founder.
    if _SEED_PATTERN.match(stage):
        return "founder"

    # Small headcount (below threshold) returns founder for non-seed stages.
    if headcount is not None and headcount < _HEADCOUNT_THRESHOLD:
        return "founder"

    # Series A maps to head_of_growth.
    if _SERIES_A_PATTERN.match(stage):
        return "head_of_growth"

    # Series B and higher (including growth/late) maps to head_of_marketing.
    if _LATE_PATTERN.match(stage):
        return "head_of_marketing"

    # Unknown/empty stage with no strong signal defaults to founder.
    return "founder"
