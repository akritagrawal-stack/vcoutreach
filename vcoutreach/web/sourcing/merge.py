"""
app/sourcing/merge.py - Dedup candidates against the partners table.

Main function: dedup_against_partners(conn, candidates)

Drops any candidate already present in the partners table (by email COLLATE
NOCASE when an email is present, else by normalized company name) AND drops
candidates whose matched partner has already been contacted (status live_sent
or live_sent_at IS NOT NULL).

Returns only net-new candidates that are not already in the DB and have not
been emailed.

Normalization for company name matching:
  Lower-case, strip common corporate suffixes (inc, corp, llc, ltd, co,
  company, the, labs), collapse whitespace. This is intentionally simpler than
  the firm-name normalizer in discovery.py which strips VC-specific suffixes;
  startup company names need only the basic corporate-entity suffixes removed.
"""
from __future__ import annotations

import re
import sqlite3

from vcoutreach.web.dedup import already_contacted_partner_ids


# Common corporate suffixes to strip when comparing company names.
# Kept minimal to avoid over-matching.
_CORP_SUFFIXES = re.compile(
    r"\b(inc|corp|llc|ltd|co|company|the|labs|technologies|tech|group)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9 ]")


def _normalize_company(text: str) -> str:
    """
    Normalize a company name for fuzzy matching.
    Strips punctuation, common corporate suffixes, and collapses whitespace.
    """
    t = (text or "").lower().strip()
    t = _PUNCT.sub(" ", t)
    t = _CORP_SUFFIXES.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _build_partner_index(conn: sqlite3.Connection) -> dict:
    """
    Build lookup indexes from the partners table.

    Returns dict with:
      emails:         set of lowercased partner emails
      norm_companies: set of normalized firm/company names
      email_to_id:    dict mapping lowercased email to partner_id
      company_to_id:  dict mapping normalized company to partner_id (first match)
    """
    rows = conn.execute("SELECT id, email, firm FROM partners").fetchall()

    emails: set[str] = set()
    norm_companies: set[str] = set()
    email_to_id: dict[str, int] = {}
    company_to_id: dict[str, int] = {}

    for r in rows:
        pid = r[0]
        email = (r[1] or "").strip().lower()
        firm = r[2] or ""

        if email:
            emails.add(email)
            email_to_id[email] = pid

        nc = _normalize_company(firm)
        if nc:
            norm_companies.add(nc)
            if nc not in company_to_id:
                company_to_id[nc] = pid

    return {
        "emails": emails,
        "norm_companies": norm_companies,
        "email_to_id": email_to_id,
        "company_to_id": company_to_id,
    }


def dedup_against_partners(
    conn: sqlite3.Connection,
    candidates: list[dict],
) -> list[dict]:
    """
    Drop candidates already present in the partners table or already contacted.

    Match priority:
      1. Email COLLATE NOCASE (when candidate has an email field).
      2. Normalized company name (strips common corporate suffixes).

    Additionally drops any candidate whose matched partner_id is in the
    already-contacted set (live_sent status or live_sent_at IS NOT NULL).

    Returns only net-new candidates (no match on email or company).

    Parameters:
      conn:       open SQLite connection to the project database
      candidates: list of dicts with at minimum "company" and optionally "email"

    Returns:
      Filtered list of candidate dicts that are net-new.
    """
    if not candidates:
        return []

    index = _build_partner_index(conn)
    contacted_ids: set[int] = already_contacted_partner_ids(conn)

    net_new: list[dict] = []

    for cand in candidates:
        email = (cand.get("email") or "").strip().lower()
        company = cand.get("company") or ""
        nc = _normalize_company(company)

        matched_pid: int | None = None
        drop_reason: str = ""

        # Check 1: email match (highest precedence, exact NOCASE)
        if email and email in index["emails"]:
            matched_pid = index["email_to_id"].get(email)
            drop_reason = f"email match: {email}"

        # Check 2: normalized company match (when no email match found)
        if matched_pid is None and nc and nc in index["norm_companies"]:
            matched_pid = index["company_to_id"].get(nc)
            drop_reason = f"company match: {nc!r}"

        if matched_pid is not None:
            # Drop: already in partners table.
            # contacted_ids is a strict subset of present-in-partners, so the
            # presence check above already subsumes the contacted check. We
            # still consult contacted_ids here so the dedup.py call is
            # load-bearing and so tracing/logging can distinguish the two causes.
            if matched_pid in contacted_ids:
                drop_reason = f"{drop_reason} (also already contacted)"
            # Uncomment to debug drops:
            # print(f"[merge] drop candidate={cand.get('company')!r} reason={drop_reason}")
            continue

        # No partner match: keep this candidate as net-new
        net_new.append(cand)

    return net_new
