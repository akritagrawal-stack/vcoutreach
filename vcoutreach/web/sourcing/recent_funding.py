"""recent_funding: parse and ingest funding CSV rows into candidate-target dicts."""

from __future__ import annotations

import csv
import os
from typing import Any

from vcoutreach.web.sourcing.contact_picker import pick_contact_role

_CAMPAIGN = "workspace_sourcing"
_AUDIENCE = "startup"


def parse_funding_csv(path: str) -> list[dict[str, Any]]:
    """Read a funding CSV and return a list of raw row dicts.

    Parameters
    ----------
    path:
        Absolute or relative path to the CSV file. Must have columns:
        company, domain, stage, raised_at, source.

    Returns
    -------
    list[dict]
        One dict per data row. Keys are taken from the CSV header.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    """
    from vcoutreach.web.config import get_workspace
    path = get_workspace().path(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Funding CSV not found: {path}")

    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def ingest_recent_funding(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize raw funding rows into candidate-target dicts.

    Each output dict contains:
    - company, firm (same as company), domain
    - audience = "startup"
    - target_role (from pick_contact_role)
    - campaign = "workspace_sourcing"
    - stage, raised_at, source (passed through from input)

    Parameters
    ----------
    rows:
        List of dicts as returned by :func:`parse_funding_csv`.

    Returns
    -------
    list[dict]
        Normalized candidate-target dicts, one per input row.
    """
    results = []
    for row in rows:
        stage = row.get("stage", "") or ""
        target_role = pick_contact_role({"stage": stage, "headcount": None})

        results.append({
            "company": row["company"],
            "firm": row["company"],
            "domain": row.get("domain", ""),
            "audience": _AUDIENCE,
            "target_role": target_role,
            "campaign": _CAMPAIGN,
            "stage": stage,
            "raised_at": row.get("raised_at", ""),
            "source": row.get("source", ""),
        })

    return results
