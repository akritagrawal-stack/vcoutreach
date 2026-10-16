"""
Portfolio company extraction from PitchBook investor profile payloads.

Provides two functions:
  - extract_portfolio_companies: parses portfolio company names and domains
    from a raw PitchBook investor profile dict (as returned by
    pitchbook_api.get_investor_profile).
  - mine_portfolios: documented stub that guards against accidental live
    network calls during tests or offline runs.
"""
from __future__ import annotations

from typing import Optional


def extract_portfolio_companies(raw_profile: dict) -> list[dict]:
    """
    Parse portfolio company names and domains from a PitchBook investor profile.

    Accepts the dict returned by pitchbook_api.get_investor_profile(). Reads
    the 'investments' list and deduplicates by company name (case-insensitively),
    preserving the first-seen casing.

    Domain is always None: PitchBook investment records do not carry a domain
    field, so no inference is attempted.

    Args:
        raw_profile: dict with an optional 'investments' key whose value is a
            list of investment dicts. Each investment dict must have a 'company'
            key (str). Missing or empty 'investments' returns an empty list.

    Returns:
        List of dicts with keys:
            company (str): company name, casing preserved from first occurrence.
            domain (None): always None (PitchBook does not supply domain).
    """
    investments: list[dict] = raw_profile.get("investments") or []

    seen_lower: set[str] = set()
    results: list[dict] = []

    for inv in investments:
        name: Optional[str] = inv.get("company")
        if not name:
            continue
        key = name.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        results.append({"company": name, "domain": None})

    return results


def mine_portfolios(partner_ids: list[str]) -> list[dict]:
    """Fetch user-selected PitchBook profiles on an explicit sourcing action."""
    from vcoutreach.web.config import get_workspace
    if get_workspace().value('PITCHBOOK_ENABLED')!='1':
        raise ValueError('Enable PitchBook and connect your browser session before sourcing.')
    if len(partner_ids)>250:
        raise ValueError('Select at most 250 profiles per sourcing action.')
    from vcoutreach.web.enrichment.pitchbook_api import get_investor_profile
    merged=[];seen=set()
    import re
    for profile_id in partner_ids:
        if not re.fullmatch(r'[0-9]{3,8}-[0-9]{2}',str(profile_id)):
            raise ValueError('Invalid PitchBook profile id.')
        for company in extract_portfolio_companies(get_investor_profile(str(profile_id))):
            key=company['company'].casefold()
            if key not in seen:
                seen.add(key);merged.append(company)
    return merged
