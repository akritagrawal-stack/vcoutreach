"""Workspace-backed outreach integration."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from vcoutreach.web.vendor import setting_value, workspace_path
from typing import Optional

from vcoutreach.web.db import cursor_ctx, get_conn

logger = logging.getLogger(__name__)

# ── Firm/name normalization (mirrors scripts/discovery_100_v7.py) ───────────────
_FIRM_SUFFIXES = re.compile(
    r"\b(management|ventures|venture|capital|partners|partner|fund|"
    r"llc|inc|l\.p\.|lp|group|associates|advisors|advisory|"
    r"investments|investment|holdings|holding)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9 ]")


def _normalize_firm(text: str) -> str:
    t = (text or "").lower().strip()
    t = _PUNCT.sub(" ", t)
    t = _FIRM_SUFFIXES.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _normalize_name(text: str) -> str:
    t = (text or "").lower().strip()
    t = _PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_existing_index(db_path: Optional[Path] = None) -> dict:
    """
    Build dedup indexes from the existing partners table.

    Returns dict with:
      firm_ids: set of existing pitchbook_firm_id and pitchbook_id strings
      norm_firms: set of normalized firm names
      name_firm: set of (normalized_name, normalized_firm) tuples
    """
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT partner_name, firm, pitchbook_id, pitchbook_firm_id FROM partners"
        ).fetchall()
    finally:
        conn.close()

    firm_ids: set[str] = set()
    norm_firms: set[str] = set()
    name_firm: set[tuple[str, str]] = set()
    for r in rows:
        for col in ("pitchbook_id", "pitchbook_firm_id"):
            v = r[col]
            if v:
                firm_ids.add(str(v).strip())
        nf = _normalize_firm(r["firm"] or "")
        if nf:
            norm_firms.add(nf)
        name_firm.add((_normalize_name(r["partner_name"] or ""), nf))
    return {"firm_ids": firm_ids, "norm_firms": norm_firms, "name_firm": name_firm}


def _resolve_firm_contact(pb, session, firm_id: str) -> dict:
    """
    Resolve a firm id to its primaryContact via the contact-info endpoint.

    Returns dict {first_name, last_name, full_name, email, position, linkedin}
    (fields may be empty strings if PitchBook returns no primaryContact).

    Raises pitchbook_api.PitchBookAPIError on 429/403 (caller must STOP).
    Other non-200 / empty bodies are treated as "no contact" and return blanks.
    """
    path = f"/profile-platform-bff/profiles/{firm_id}/investor/contact-info"
    pb._throttle()
    try:
        data = pb._get(session, path)
    except pb.PitchBookAPIError as e:
        msg = str(e)
        # Hard stop on rate-limit / Cloudflare - re-raise so the run aborts.
        if "429" in msg or "403" in msg:
            raise
        logger.warning("contact-info failed for firm %s: %s", firm_id, e)
        return {}
    except ValueError:
        # Empty / non-JSON body - treat as no contact data.
        logger.warning("contact-info empty body for firm %s", firm_id)
        return {}

    pc = (data or {}).get("primaryContact") or {}
    first = (pc.get("firstName") or "").strip()
    last = (pc.get("lastName") or "").strip()
    full = (first + " " + last).strip()
    return {
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "email": (pc.get("email") or "").strip(),
        "position": (pc.get("primaryPosition") or "").strip(),
        "linkedin": (pc.get("linkedInLink") or "").strip(),
        "contact_pb_id": (pc.get("pbId") or "").strip(),
    }


def _placeholder_email(firm_id: str) -> str:
    """
    Synthetic, non-routable, unique-per-firm email used only to satisfy the
    partners.email NOT NULL UNIQUE constraint when no real contact email exists.
    The .invalid TLD (RFC 2606) can never deliver, so a row carrying this can
    never accidentally send. needs_enrichment=1 flags it for downstream resolve.
    """
    safe = re.sub(r"[^a-z0-9-]", "", (firm_id or "unknown").lower())
    return f"pb-firm-{safe}@needs-enrichment.invalid"


def discover_investors(
    pages: int = 1,
    page_size: int = 50,
    entry_point: str = "TOP_SEED_INVESTORS",
    dry_run: bool = False,
    resolve_contacts: bool = True,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Enumerate screener firms, resolve a primary partner + email per firm, dedup,
    and insert NEW rows (needs_enrichment=1, source='pitchbook_discover').

    Returns a summary dict:
      fetched, kept_after_filter, new, duplicates, inserted,
      with_real_email, placeholder_email, dry_run, candidates (list of preview dicts)

    Volume is operator-controlled via pages / page_size. Default is modest so an
    accidental run is small. STOPs (raises) on HTTP 429/403.
    """
    import vcoutreach.web.config  # noqa: F401  ensure PITCHBOOK_BACKEND resolves to curl_cffi
    from vcoutreach.web.enrichment import pitchbook_api as pb

    assert pb._PITCHBOOK_BACKEND == "curl_cffi", (
        f"discovery requires curl_cffi backend, got {pb._PITCHBOOK_BACKEND!r} - STOP"
    )

    session = pb._load_session()

    # 1. Enumerate firms from the screener (curl_cffi; not rebound to browser).
    firms = pb.screener_search(
        filters={"entry_point": entry_point},
        page_size=page_size,
        max_pages=pages,
        session=session,
    )
    fetched_count = len(firms)
    logger.info("discover: screener returned %d firms (after us/type filter)", fetched_count)

    existing = _build_existing_index(db_path)

    candidates: list[dict] = []
    new_rows: list[dict] = []
    dup_count = 0

    for f in firms:
        firm_id = str(f.get("profile_id") or "").strip()
        firm_name = f.get("name") or f.get("firm") or ""
        norm_firm = _normalize_firm(firm_name)

        # ── Dedup: exact firm id, then normalized firm name ──
        is_dup = False
        dup_reason = ""
        if firm_id and firm_id in existing["firm_ids"]:
            is_dup, dup_reason = True, f"firm_id {firm_id} already present"
        elif norm_firm and norm_firm in existing["norm_firms"]:
            is_dup, dup_reason = True, f"normalized firm '{norm_firm}' already present"

        if is_dup:
            dup_count += 1
            candidates.append({
                "firm_id": firm_id, "firm": firm_name, "status": "duplicate",
                "reason": dup_reason,
            })
            continue

        # ── Resolve partner + email via contact-info (firm id) ──
        contact = {}
        if resolve_contacts and firm_id:
            contact = _resolve_firm_contact(pb, session, firm_id)

        partner_name = contact.get("full_name") or ""
        email = contact.get("email") or ""
        has_real_email = bool(email)

        # Name+firm fuzzy dedup (only when we actually resolved a person).
        if partner_name:
            key = (_normalize_name(partner_name), norm_firm)
            if key in existing["name_firm"]:
                dup_count += 1
                candidates.append({
                    "firm_id": firm_id, "firm": firm_name, "partner": partner_name,
                    "status": "duplicate", "reason": f"name+firm '{key}' already present",
                })
                continue

        # Placeholder when no real contact resolved - firm-level row, flagged.
        if not partner_name:
            partner_name = f"{firm_name} (firm contact TBD)"
        if not has_real_email:
            email = _placeholder_email(firm_id)

        first_name = contact.get("first_name") or (partner_name.split()[0] if partner_name else "")

        row = {
            "firm_id": firm_id,
            "partner_name": partner_name,
            "first_name": first_name,
            "firm": firm_name,
            "email": email,
            "has_real_email": has_real_email,
            "linkedin": contact.get("linkedin", ""),
            "position": contact.get("position", ""),
            "investor_type": f.get("investor_type", ""),
            "location": f.get("location", ""),
        }
        new_rows.append(row)
        candidates.append({
            "firm_id": firm_id, "firm": firm_name, "partner": partner_name,
            "email": email, "real_email": has_real_email, "status": "new",
        })
        # Register so duplicates within this same batch are caught too.
        existing["firm_ids"].add(firm_id)
        existing["norm_firms"].add(norm_firm)
        existing["name_firm"].add((_normalize_name(partner_name), norm_firm))

    # 2. Insert NEW rows (unless dry-run).
    inserted = 0
    if not dry_run and new_rows:
        now = _now_iso()
        with cursor_ctx(db_path) as (conn, cur):
            for r in new_rows:
                # email is UNIQUE COLLATE NOCASE - INSERT OR IGNORE makes the
                # insert idempotent even if a concurrent/duplicate email slips in.
                cur.execute(
                    """
                    INSERT OR IGNORE INTO partners
                        (partner_name, first_name, firm, email, tier,
                         channel, linkedin_url, notes, source,
                         pitchbook_id, pitchbook_firm_id, needs_enrichment, created_at)
                    VALUES (?, ?, ?, ?, ?, 'email', ?, ?, 'pitchbook_discover', ?, ?, 1, ?)
                    """,
                    (
                        r["partner_name"], r["first_name"], r["firm"],
                        r["email"].strip().lower(), None,
                        r["linkedin"] or None,
                        (r["position"] or None),
                        r["firm_id"], r["firm_id"], now,
                    ),
                )
                if cur.rowcount:
                    inserted += 1

    real_email_count = sum(1 for r in new_rows if r["has_real_email"])
    placeholder_count = len(new_rows) - real_email_count

    return {
        "fetched": fetched_count,
        "kept_after_filter": fetched_count,  # screener already applied us/type filter
        "new": len(new_rows),
        "duplicates": dup_count,
        "inserted": inserted,
        "with_real_email": real_email_count,
        "placeholder_email": placeholder_count,
        "dry_run": dry_run,
        "entry_point": entry_point,
        "pages": pages,
        "page_size": page_size,
        "candidates": candidates,
    }


# ── Comparable-company discovery ────────────────────────────────────────────────
#
# Configure comparable companies in the workspace. Resolve company identities,
# aggregate their investors, then profile candidates and apply the configured fit
# filters. Exact known identifiers avoid a search; search results need validation.
KNOWN_COMP_PBIDS = {}

# Resolve names through search and reject results with mismatched identities.
SEARCH_COMPS = []

# Firms that must NOT be marked for outreach: insert with a manual-review marker.
# Matched against the normalized firm name (alias-aware via _conflict_firm_match).
CONFLICT_FIRMS = {}

MANUAL_REVIEW_MARKER = "manual_review=conflict_firm"

# ── Check-size fit scoring (tunable) ────────────────────────────────────────────
# Check sizes are raw USD. Workspace thresholds control the hard exclusion and
# the softer score adjustments. Missing medians remain eligible for review.
_CHECK_SIZE_HARD_EXCLUDE_USD = float(setting_value("DISCOVERY_MAX_CHECK_USD", "inf"))  # median > this (raw USD): hard-reject
# SOFT boost/penalty band (does NOT gate; folded into quality_score):
_CHECK_SIZE_SMALL_USD = float(setting_value("DISCOVERY_SMALL_CHECK_USD", "0"))     # at/below this median: small-check, boost
_CHECK_SIZE_LARGE_USD = float(setting_value("DISCOVERY_LARGE_CHECK_USD", "inf"))     # above this median: large-check, penalize
_CHECK_SIZE_SMALL_BOOST = 10          # quality_score points added for small checks
_CHECK_SIZE_LARGE_PENALTY = 10        # quality_score points removed for large medians
_PRESEED_HEAVY_BOOST = 5              # extra boost when pre-seed deals dominate


def _is_megafund_median(median_check) -> bool:
    """
    True if medianCheckSize is PRESENT and exceeds the hard-exclude threshold
    (clearly growth-stage / too large for a pre-seed/seed configured campaign).
    A missing/null/unparseable median returns False (kept; medians are sparse).
    """
    if median_check is None:
        return False
    try:
        return float(median_check) > _CHECK_SIZE_HARD_EXCLUDE_USD
    except (TypeError, ValueError):
        return False


def _check_size_fit_adjustment(median_check, seed_n: int, preseed_n: int) -> int:
    """
    Soft, tunable quality_score delta for check-size / stage fit. Positive favors
    small-check / pre-seed-heavy investors; negative penalizes very large medians.
    Never hard-excludes (callers add this to quality_score, they do not gate on it).

    median_check may be None (unknown) -> no median-based adjustment, but a
    pre-seed-heavy deal mix still earns a small boost.
    """
    delta = 0
    if median_check is not None:
        try:
            mc = float(median_check)
        except (TypeError, ValueError):
            mc = None
        if mc is not None:
            if mc <= _CHECK_SIZE_SMALL_USD:
                delta += _CHECK_SIZE_SMALL_BOOST
            elif mc > _CHECK_SIZE_LARGE_USD:
                delta -= _CHECK_SIZE_LARGE_PENALTY
    # Pre-seed-heavy deal mix is a fit signal independent of (noisy) medians.
    if preseed_n > 0 and preseed_n >= seed_n:
        delta += _PRESEED_HEAVY_BOOST
    return delta


def _conflict_firm_match(firm_name: str) -> Optional[str]:
    """Workspace-backed outreach integration."""
    raw = (firm_name or "").lower().strip()
    norm = _normalize_firm(firm_name)
    conflicts = _discovery_config().get("conflict_firms", CONFLICT_FIRMS)
    for needle, tag in (conflicts.items() if isinstance(conflicts, dict) else []):
        if needle in raw or (norm and needle in norm) or norm == _normalize_firm(needle):
            return tag
    return None


# Trivial corporate/suffix tokens that do not change company identity. Stripped
# from the RETURNED name before checking it introduces no new significant tokens.
_TRIVIAL_COMPANY_TOKENS = frozenset({
    "inc", "llc", "ltd", "corp", "co", "company", "the", "app", "labs",
    "nas", "nyse", "otc",  # ignore exchange suffixes in returned names
})


def _company_name_matches(intent_name: str, returned_name: str) -> bool:
    """
    Pure predicate: does the PitchBook-returned company name confidently match the
    intended comp name? Rejects ticker matches and unrelated substring hits.

    Rule: exact token-set equality, OR the returned name's SIGNIFICANT tokens are a
    subset of the intent tokens (the returned name introduces nothing new beyond
    the intent, modulo trivial corporate/exchange suffix tokens). A trailing TLD on
    the intent (".so", ".app") is stripped first.
    """
    intent = re.sub(r"\.[a-z]{1,4}$", "", (intent_name or "").lower().strip())
    intent_tokens = set(re.findall(r"[a-z0-9]+", intent))
    ret_tokens_all = set(re.findall(r"[a-z0-9]+", (returned_name or "").lower()))
    ret_significant = ret_tokens_all - _TRIVIAL_COMPANY_TOKENS

    if not intent_tokens or not ret_tokens_all:
        return False

    # Exact equality on the full token sets.
    if intent_tokens == ret_tokens_all:
        return True

    # Returned name must introduce no significant token beyond the intent.
    # A domain suffix may be omitted, but unrelated significant words must fail.
    if ret_significant and ret_significant.issubset(intent_tokens):
        return True

    return False


def _resolve_comp_to_pbid(pb, session, comp_name: str) -> Optional[dict]:
    """
    Search PitchBook for a company by name and return the validated top COMPANY
    match, or None if no confident match.

    Validation: the returned company name must fuzzy-match the intended comp name
    after normalization, rejecting unrelated companies. Uses one view credit.

    Returns {"pbId": str, "name": str} on a confident match, else None.
    """
    body = {
        "savedConferenceSearchAllowed": False,
        "searchRequest": {"limit": 8, "offset": 0, "query": comp_name},
        "timeZoneOffset": "-05:00",
    }
    data = pb._post(session, "/general-search/search/mixed", body)

    # Scan ALL COMPANY items, take the FIRST that passes tight validation. The real
    # comp may rank below an unrelated ticker match.
    for item in data.get("items", []):
        if item.get("type") != "COMPANY":
            continue
        val = item.get("value", {}) or {}
        pr = val.get("profileResult", {}) or {}
        pbid = pr.get("id")
        ret_name = (pr.get("name") or "").strip()
        if not pbid or not ret_name:
            continue

        if _company_name_matches(comp_name, ret_name):
            return {"pbId": str(pbid), "name": ret_name}
        logger.warning(
            "comp '%s' COMPANY hit '%s' (%s) failed name validation - skipping",
            comp_name, ret_name, pbid,
        )

    return None


# Marker PitchBook puts in a 403 body for a bad/stale pbId. This is NOT a
# Cloudflare block, so the harvest self-heals (re-resolve via search) instead of
# hard-stopping the run. Surfaced into the error message by pitchbook_api._get's
# 403 branch (body=... is included there).
_INVALID_PBID_MARKER = "PROFILE_INVALID_PB_ID"


def _is_invalid_pbid_error(err: Exception) -> bool:
    """
    True if this error is a PitchBook 'invalid pbId' 403 (body carries
    PROFILE_INVALID_PB_ID), as opposed to a genuine Cloudflare HTML 403 or a 429.
    Defensive: matches case-insensitively on the stringified error.
    """
    return _INVALID_PBID_MARKER.lower() in str(err).lower()


def _harvest_comp_investors(pb, session, company_id: str) -> list[dict]:
    """
    Pull investors for one comp company and, while doing so, scan the raw round
    items for any lead-partner / person field that PitchBook may expose.

    Returns a list of dicts: {investorId, investorName, leadPartner} where
    leadPartner is {name, pbId} if a lead person was found on a round, else None.
    Spends 2 view credits (ACTIVE + FORMER).
    """
    seen: set[str] = set()
    out: list[dict] = []

    for itype in ("ACTIVE", "FORMER"):
        pb._throttle()
        try:
            data = pb._get(
                session,
                f"/profile-platform-bff/profiles/{company_id}/company/investors/{itype}",
                params={"page": 1, "pageSize": 25},
            )
        except pb.PitchBookAPIError as e:
            # Invalid-pbId 403: re-raise so the CALLER self-heals (re-resolve via
            # search). Checked FIRST so it is never mistaken for a Cloudflare stop.
            if _is_invalid_pbid_error(e):
                raise
            # Genuine Cloudflare 403 / 429: re-raise so the caller STOPS the run.
            if "403" in str(e) or "429" in str(e):
                raise
            logger.warning("investors/%s failed for %s: %s", itype, company_id, e)
            continue

        for item in data.get("investors", []):
            inv = item.get("investor", {}) or {}
            iid = inv.get("pbId")
            if not iid or iid in seen:
                continue
            seen.add(iid)

            # Best-effort lead-partner detection on the round items. PitchBook may
            # not expose this; if a person/lead field exists we capture it, else
            # the caller falls back to the firm primaryContact.
            lead = _extract_lead_partner(item)

            out.append({
                "investorId": str(iid),
                "investorName": inv.get("name", "") or "",
                "leadPartner": lead,
            })

    return out


def _extract_lead_partner(investor_item: dict) -> Optional[dict]:
    """
    Best-effort scan of a company-investors item for a lead-partner / person whom
    PitchBook attributes to the deal. Returns {name, pbId} or None.

    The exact JSON shape is unverified (the live probe was blocked by a missing
    cf_clearance cookie), so this walks the item looking for a nested object that
    carries a person name and a flag/field implying a lead or deal contact. It is
    deliberately conservative: it only returns a hit when a name is present.
    """
    found: dict = {}

    def looks_like_person(d: dict) -> Optional[dict]:
        first = (d.get("firstName") or "").strip()
        last = (d.get("lastName") or "").strip()
        name = (d.get("name") or "").strip()
        pid = (d.get("pbId") or d.get("id") or "")
        full = (first + " " + last).strip() or name
        # Require a two-token human-looking name to avoid firm names.
        if full and len(full.split()) >= 2:
            return {"name": full, "pbId": str(pid) if pid else ""}
        return None

    def walk(obj, key_path=""):
        if found:
            return
        if isinstance(obj, dict):
            kl = key_path.lower()
            if ("lead" in kl or "partner" in kl or "person" in kl
                    or "dealteam" in kl.replace("_", "").replace(".", "")):
                person = looks_like_person(obj)
                if person:
                    found.update(person)
                    return
            for k, v in obj.items():
                walk(v, key_path + "." + k)
        elif isinstance(obj, list):
            for it in obj:
                walk(it, key_path)

    walk(investor_item)
    return found or None


def _count_stage_deals(investments: list[dict]) -> tuple[int, int]:
    """
    Count Seed and Pre-Seed deals in an investor's investments[].
    Returns (seed_count, preseed_count). dealType is round.shortDealType.
    """
    seed = preseed = 0
    for inv in investments or []:
        dt = (inv.get("dealType") or "").lower()
        if "pre-seed" in dt or "preseed" in dt or "pre seed" in dt:
            preseed += 1
        elif "seed" in dt:
            seed += 1
    return seed, preseed


def discover_by_comparable_companies(
    profile_budget: int = 9,
    credit_cap: int = 60,
    dry_run: bool = False,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Comparable-company discovery: comps -> their investors -> fit-filtered partners.

    Workflow:
      1. Resolve each comp to a pbId (known map, or search + name validation).
      2. Harvest investors per comp; aggregate by companyCount (comps each backed).
      3. Conflict firms are flagged at harvest (manual_review) and not profiled.
      4. Dedup against existing partners by pbId + normalized firm BEFORE profiling.
      5. Profile companyCount>=2 first, then fill with companyCount=1 until the
         credit cap is reached. Apply fit filters: is_consumer_relevant + stage/
         check-size signal + is_target_profile.
      6. Right-partner resolution: leadPartner if PitchBook exposed one on the deal,
         else firm primaryContact via contact-info. Record which method was used.
      7. Insert survivors (needs_enrichment=1, source=pitchbook_discover_comps).

    Credit accounting (1 view credit per API call):
      search per search-comp (1 each) + harvest per comp (2 each) + profile
      (3 each: general-info, contact-info, investments) + contact-info resolution
      (1 each) for firms without a leadPartner.

    Hard stops: any HTTP 403/429 aborts the run; the summary still reports
    credits_spent and where it stopped. Volume is bounded by credit_cap.

    Returns a summary dict with candidates, qualified survivors, conflicts,
    failed comps, credits_spent, and stop info.
    """
    import vcoutreach.web.config  # noqa: F401  ensure PITCHBOOK_BACKEND resolves to curl_cffi
    from vcoutreach.web.enrichment import pitchbook_api as pb

    assert pb._PITCHBOOK_BACKEND == "curl_cffi", (
        f"comp discovery requires curl_cffi backend, got {pb._PITCHBOOK_BACKEND!r} - STOP"
    )

    # Reuse the proven gates from the v7 discovery script (lazy import: importing
    # the module runs logging.basicConfig and creates a log file at module top).

    session = pb._load_session()

    # Retroactively ensure ALL existing partner rows whose firm matches a conflict
    # firm (alias-aware) carry the manual-review marker. Idempotent. Subsumes the
    # old one-off Plug-and-Play (260) flagger. Excludes false-positive id 173
    # User-provided workspace configuration.
    conflict_sweep = _sweep_existing_conflict_rows(dry_run, db_path)

    # Retroactively exclude already-inserted comp rows whose recorded median check
    # User-provided workspace configuration.
    # Idempotent: marks needs_enrichment=0, appends excluded note, sets
    # emailable_checked_at + dropped_reason so the row cannot flow into outreach.
    megafund_sweep = _exclude_existing_megafund_rows(dry_run, db_path)

    summary: dict = {
        "credits_spent": 0,
        "credit_cap": credit_cap,
        "stopped": False,
        "stop_reason": None,
        "comps_resolved": {},      # comp_name -> {"pbId":..., "name":...}
        "comps_failed": [],        # comp names that did not resolve
        "aggregated_investors": 0,
        "conflicts": [],           # flagged conflict firms
        "conflict_sweep": {},      # retroactive existing-row conflict flagging
        "existing_rows_conflict_flagged": 0,  # existing rows updated this run
        "qualified": [],           # survivors marked for outreach
        "rejected": [],            # profiled but filtered out
        "inserted": 0,
        "dry_run": dry_run,
    }
    summary["conflict_sweep"] = conflict_sweep
    summary["existing_rows_conflict_flagged"] = conflict_sweep.get("flagged", 0)
    summary["megafund_sweep"] = megafund_sweep

    def spend(n: int):
        summary["credits_spent"] += n

    def cap_reached() -> bool:
        return summary["credits_spent"] >= credit_cap

    # ── Step 1: resolve comps to pbIds ──────────────────────────────────────────
    resolved: dict[str, str] = {}  # comp_name -> pbId
    config = _discovery_config()
    known_comps = config.get("known_comp_pbids", KNOWN_COMP_PBIDS)
    search_comps = config.get("search_comps", SEARCH_COMPS)
    if not isinstance(known_comps, dict) or not isinstance(search_comps, list):
        raise ValueError("Discovery comparables must be a map and a list")
    for name, pbid in known_comps.items():
        resolved[name] = pbid
        summary["comps_resolved"][name] = {"pbId": pbid, "name": name, "method": "known"}

    try:
        for comp in search_comps:
            if cap_reached():
                summary["stopped"] = True
                summary["stop_reason"] = "credit cap reached during comp resolution"
                break
            pb._throttle()
            try:
                hit = _resolve_comp_to_pbid(pb, session, comp)
            except pb.PitchBookAPIError as e:
                if "403" in str(e) or "429" in str(e):
                    summary["stopped"] = True
                    summary["stop_reason"] = f"HTTP block during search '{comp}': {e}"
                    return _finalize_comp_summary(summary)
                logger.warning("search failed for comp '%s': %s", comp, e)
                summary["comps_failed"].append(comp)
                spend(1)
                continue
            spend(1)
            if hit:
                resolved[comp] = hit["pbId"]
                summary["comps_resolved"][comp] = {
                    "pbId": hit["pbId"], "name": hit["name"], "method": "search",
                }
            else:
                summary["comps_failed"].append(comp)

        # ── Step 2: harvest investors per comp, aggregate by companyCount ──────
        investor_agg: dict[str, dict] = {}  # investorId -> aggregated
        # Iterate a snapshot: a self-heal may rewrite resolved[comp_name].
        for comp_name, pbid in list(resolved.items()):
            if cap_reached():
                summary["stopped"] = True
                summary["stop_reason"] = "credit cap reached during harvest"
                break
            try:
                investors = _harvest_comp_investors(pb, session, pbid)
            except pb.PitchBookAPIError as e:
                # SELF-HEAL: an invalid-pbId 403 (stale/wrong known id) is NOT a
                # Cloudflare block. Checked BEFORE the 403/429 hard-stop so it can
                # never be mistaken for one. Re-resolve the comp by name via
                # search+validate, then retry the harvest with the resolved id.
                if _is_invalid_pbid_error(e):
                    spend(2)  # the failed harvest attempt still cost view credits
                    logger.warning(
                        "comp %s pbId %s invalid, falling back to search",
                        comp_name, pbid,
                    )
                    if cap_reached():
                        summary["stopped"] = True
                        summary["stop_reason"] = (
                            "credit cap reached before invalid-pbId self-heal search")
                        break
                    try:
                        pb._throttle()
                        hit = _resolve_comp_to_pbid(pb, session, comp_name)
                        spend(1)
                    except pb.PitchBookAPIError as se:
                        if "403" in str(se) or "429" in str(se):
                            summary["stopped"] = True
                            summary["stop_reason"] = (
                                f"HTTP block during self-heal search of "
                                f"'{comp_name}': {se}")
                            return _finalize_comp_summary(summary)
                        logger.warning(
                            "self-heal search failed for '%s': %s", comp_name, se)
                        summary["comps_failed"].append(comp_name)
                        continue
                    if not hit:
                        logger.warning(
                            "self-heal could not resolve comp '%s'; skipping",
                            comp_name)
                        summary["comps_failed"].append(comp_name)
                        continue
                    new_pbid = hit["pbId"]
                    resolved[comp_name] = new_pbid
                    summary["comps_resolved"][comp_name] = {
                        "pbId": new_pbid, "name": hit["name"],
                        "method": "search (self-healed from invalid known id)",
                    }
                    if summary["credits_spent"] + 2 > credit_cap:
                        summary["stopped"] = True
                        summary["stop_reason"] = (
                            "credit cap reached before invalid-pbId retry harvest")
                        break
                    try:
                        investors = _harvest_comp_investors(pb, session, new_pbid)
                        spend(2)
                    except pb.PitchBookAPIError as re_e:
                        if "403" in str(re_e) or "429" in str(re_e):
                            summary["stopped"] = True
                            summary["stop_reason"] = (
                                f"HTTP block during retry harvest of "
                                f"'{comp_name}': {re_e}")
                            return _finalize_comp_summary(summary)
                        # Still failing (invalid again or other): skip this comp.
                        logger.warning(
                            "retry harvest failed for '%s' (%s): %s; skipping",
                            comp_name, new_pbid, re_e)
                        summary["comps_failed"].append(comp_name)
                        continue
                    # Self-heal succeeded: `investors` is set and all credits for
                    # this comp (failed attempt + search + retry) are already
                    # accounted; fall through to aggregation WITHOUT spending again.
                elif "403" in str(e) or "429" in str(e):
                    summary["stopped"] = True
                    summary["stop_reason"] = f"HTTP block during harvest of '{comp_name}': {e}"
                    return _finalize_comp_summary(summary)
                else:
                    logger.warning("harvest failed for '%s' (%s): %s", comp_name, pbid, e)
                    spend(2)
                    continue
            else:
                # No exception: normal harvest cost 2 view credits.
                spend(2)
            for inv in investors:
                iid = inv["investorId"]
                if iid not in investor_agg:
                    investor_agg[iid] = {
                        "investorId": iid,
                        "investorName": inv["investorName"],
                        "companyCount": 0,
                        "comps": [],
                        "leadPartner": None,
                    }
                investor_agg[iid]["companyCount"] += 1
                investor_agg[iid]["comps"].append(comp_name)
                if inv.get("leadPartner") and not investor_agg[iid]["leadPartner"]:
                    investor_agg[iid]["leadPartner"] = inv["leadPartner"]

        summary["aggregated_investors"] = len(investor_agg)

        # ── Step 3: dedup + conflict flag BEFORE profiling ─────────────────────
        existing = _build_existing_index(db_path)
        now = _now_iso()

        # Sort: companyCount desc (>=2 strong signal first), then name for stability.
        ranked = sorted(
            investor_agg.values(),
            key=lambda x: (-x["companyCount"], x["investorName"].lower()),
        )

        conflict_rows: list[dict] = []
        to_profile: list[dict] = []
        for inv in ranked:
            firm_name = inv["investorName"]
            firm_id = inv["investorId"]
            norm_firm = _normalize_firm(firm_name)

            conflict_tag = _conflict_firm_match(firm_name)
            if conflict_tag:
                conflict_rows.append({**inv, "conflict_tag": conflict_tag, "norm_firm": norm_firm})
                continue

            # Dedup by pbId or normalized firm (no person name yet -> name+firm later).
            if firm_id in existing["firm_ids"]:
                summary["rejected"].append({
                    "firm": firm_name, "investorId": firm_id,
                    "reason": "duplicate (pbId already in partners)",
                    "companyCount": inv["companyCount"],
                })
                continue
            if norm_firm and norm_firm in existing["norm_firms"]:
                summary["rejected"].append({
                    "firm": firm_name, "investorId": firm_id,
                    "reason": "duplicate (normalized firm already in partners)",
                    "companyCount": inv["companyCount"],
                })
                continue

            to_profile.append({**inv, "norm_firm": norm_firm})

        # ── Insert conflict-firm rows (flagged, not profiled) ──────────────────
        new_rows: list[dict] = []
        for c in conflict_rows:
            comps_str = ", ".join(c["comps"])
            notes = (
                f"{MANUAL_REVIEW_MARKER}; conflict={c['conflict_tag']}; "
                f"companyCount={c['companyCount']} (comps: {comps_str}); "
                f"source=comparable_companies. Do not send; manual review."
            )
            summary["conflicts"].append({
                "firm": c["investorName"], "investorId": c["investorId"],
                "conflict_tag": c["conflict_tag"], "companyCount": c["companyCount"],
                "comps": c["comps"],
            })
            # Already-exists path: a partner row for this conflict firm is already
            # in the DB. Do NOT insert a duplicate, but UPDATE the existing row(s)
            # to carry the manual-review marker so a surfaced conflict actually
            # protects them from outreach. Idempotent (no double-append).
            if c["investorId"] in existing["firm_ids"] or (
                c["norm_firm"] and c["norm_firm"] in existing["norm_firms"]
            ):
                if not dry_run:
                    with cursor_ctx(db_path) as (conn, cur):
                        n = _flag_existing_conflict_rows(
                            cur, c["conflict_tag"], c["norm_firm"], c["investorId"]
                        )
                    summary["existing_rows_conflict_flagged"] += n
                continue
            new_rows.append({
                "firm_id": c["investorId"],
                "partner_name": f"{c['investorName']} (conflict - manual review)",
                "first_name": "",
                "firm": c["investorName"],
                "email": _placeholder_email(c["investorId"]),
                "linkedin": "",
                "position": "",
                "notes": notes,
                "needs_enrichment": 1,
            })
            existing["firm_ids"].add(c["investorId"])
            if c["norm_firm"]:
                existing["norm_firms"].add(c["norm_firm"])

        # ── Step 4-6: profile shortlist, fit-filter, resolve partner ───────────
        profiled = 0
        for cand in to_profile:
            if profiled >= profile_budget:
                break
            # Need ~3 (profile) + up to 1 (contact-info fallback) credits headroom.
            if summary["credits_spent"] + 4 > credit_cap:
                summary["stopped"] = True
                summary["stop_reason"] = "credit cap reached before next profile"
                break

            firm_id = cand["investorId"]
            firm_name = cand["investorName"]
            try:
                profile = pb.get_investor_profile(firm_id, session=session)
            except pb.PitchBookAPIError as e:
                if "403" in str(e) or "429" in str(e):
                    summary["stopped"] = True
                    summary["stop_reason"] = f"HTTP block profiling '{firm_name}': {e}"
                    # Flush what we have so far before returning.
                    _insert_comp_rows(new_rows, dry_run, db_path, now, summary)
                    return _finalize_comp_summary(summary)
                logger.warning("profile failed for '%s' (%s): %s", firm_name, firm_id, e)
                spend(3)
                profiled += 1
                continue
            spend(3)
            profiled += 1

            bio = profile.get("bio", "")
            ptype = profile.get("primaryType", "")
            investments = profile.get("investments", [])
            median_check = profile.get("medianCheckSize")
            seed_n, preseed_n = _count_stage_deals(investments)

            # FIT FILTER 0 (HARD EXCLUDE): megafund / growth-stage median check.
            # medianCheckSize is raw USD. A median above the configured
            # hard-exclude threshold is outside this workspace campaign fit.
            # Only gates on a PRESENT median (sparse/null medians are kept).
            if _is_megafund_median(median_check):
                summary["rejected"].append({
                    "firm": firm_name, "investorId": firm_id,
                    "reason": (
                        f"megafund: medianCheckSize={median_check} raw USD "
                        f"> hard-exclude {_CHECK_SIZE_HARD_EXCLUDE_USD}"
                    ),
                    "companyCount": cand["companyCount"],
                    "medianCheckSize": median_check,
                })
                continue

            # FIT FILTER 1: consumer/social relevance.
            if not is_consumer_relevant(firm_name, bio + " " + ptype):
                if cand["companyCount"] < 2:
                    summary["rejected"].append({
                        "firm": firm_name, "investorId": firm_id,
                        "reason": "not consumer-relevant", "companyCount": cand["companyCount"],
                    })
                    continue

            # FIT FILTER 2: target profile (US, VC/angel/micro, not PE/growth/FO).
            passes, reason = is_target_profile(
                firm_name=firm_name, description=bio,
                investor_type=ptype, location=profile.get("officeCity", ""),
            )
            if not passes:
                summary["rejected"].append({
                    "firm": firm_name, "investorId": firm_id,
                    "reason": f"target_profile: {reason}", "companyCount": cand["companyCount"],
                })
                continue

            # FIT FILTER 3: stage / check-size. Tolerant: require SOME seed/pre-seed
            # signal OR a present median check size (PB medians skew high, so we do
            # NOT hard-gate on a low value).
            stage_signal = (seed_n + preseed_n) > 0
            if not stage_signal and median_check is None:
                summary["rejected"].append({
                    "firm": firm_name, "investorId": firm_id,
                    "reason": "no seed/pre-seed signal and no median check size",
                    "companyCount": cand["companyCount"],
                })
                continue

            # ── RIGHT-PARTNER resolution ──
            lead = cand.get("leadPartner")
            resolution_method = ""
            partner_name = ""
            first_name = ""
            email = ""
            linkedin = ""
            position = ""
            partner_pb_id = ""

            # The profile itself may already be a person (angel) carrying contact.
            prof_first = (profile.get("firstName") or "").strip()
            prof_last = (profile.get("lastName") or "").strip()
            prof_full = (prof_first + " " + prof_last).strip()

            if lead and lead.get("name"):
                partner_name = lead["name"]
                partner_pb_id = lead.get("pbId", "")
                resolution_method = "leadPartner"
                # We have a name but maybe no email; fall through to contact-info if
                # a pbId is available and budget allows. Otherwise leave email blank.
            elif prof_full:
                # Profile is an individual investor (angel) - use its own contact.
                partner_name = prof_full
                first_name = prof_first
                email = profile.get("email", "") or ""
                linkedin = profile.get("linkedIn", "") or ""
                position = profile.get("primaryPosition", "") or ""
                resolution_method = "primaryContact" if email else "profileSelf"

            # Firm-level (or leadPartner without email): resolve firm primaryContact.
            if not email:
                if summary["credits_spent"] + 1 <= credit_cap:
                    contact = _resolve_firm_contact(pb, session, firm_id)
                    spend(1)
                    if contact.get("full_name") and not partner_name:
                        partner_name = contact["full_name"]
                        first_name = contact.get("first_name", "")
                        resolution_method = "primaryContact"
                    if contact.get("email"):
                        email = contact["email"]
                        if resolution_method in ("", "leadPartner") and not partner_name:
                            resolution_method = "primaryContact"
                        elif resolution_method == "":
                            resolution_method = "primaryContact"
                    linkedin = linkedin or contact.get("linkedin", "")
                    position = position or contact.get("position", "")
                    if not resolution_method:
                        resolution_method = "primaryContact" if (partner_name or email) else "unresolved"

            if not partner_name:
                partner_name = f"{firm_name} (firm contact TBD)"
                resolution_method = resolution_method or "unresolved"
            if not first_name:
                first_name = partner_name.split()[0] if partner_name else ""

            has_real_email = bool(email)
            if not has_real_email:
                email = _placeholder_email(firm_id)

            # Post-profile name+firm dedup.
            nf_key = (_normalize_name(partner_name), cand["norm_firm"])
            if nf_key in existing["name_firm"]:
                summary["rejected"].append({
                    "firm": firm_name, "investorId": firm_id,
                    "reason": "duplicate (name+firm already in partners)",
                    "companyCount": cand["companyCount"],
                })
                continue

            base_quality = partner_quality_score(
                name=partner_name, bio=bio, linkedin_url=linkedin,
                company_count=cand["companyCount"],
            )
            # Soft check-size / stage fit adjustment (tunable; see constants above).
            # Boosts small-check / pre-seed-heavy investors, penalizes large medians.
            # Does NOT gate: a high-median investor can still qualify, just ranks lower.
            check_size_delta = _check_size_fit_adjustment(median_check, seed_n, preseed_n)
            quality = base_quality + check_size_delta

            comps_str = ", ".join(cand["comps"])
            notes = (
                f"source=comparable_companies; companyCount={cand['companyCount']} "
                f"(comps: {comps_str}); quality_score={quality} "
                f"(base={base_quality}, check_size_delta={check_size_delta:+d}); "
                f"stage_signal=seed:{seed_n}/preseed:{preseed_n}; "
                f"medianCheckSize={median_check}; "
                f"partner_resolution={resolution_method}."
            )

            row = {
                "firm_id": firm_id,
                "partner_name": partner_name,
                "first_name": first_name,
                "firm": firm_name,
                "email": email,
                "linkedin": linkedin,
                "position": position,
                "notes": notes,
                "needs_enrichment": 1,
            }
            new_rows.append(row)
            existing["firm_ids"].add(firm_id)
            existing["norm_firms"].add(cand["norm_firm"])
            existing["name_firm"].add(nf_key)

            summary["qualified"].append({
                "partner_name": partner_name,
                "firm": firm_name,
                "investor_type": ptype,
                "companyCount": cand["companyCount"],
                "comps": cand["comps"],
                "seed_deals": seed_n,
                "preseed_deals": preseed_n,
                "medianCheckSize": median_check,
                "email": email if has_real_email else "(placeholder)",
                "resolution_method": resolution_method,
                "quality_score": quality,
            })

        # ── Step 7: insert all rows ────────────────────────────────────────────
        _insert_comp_rows(new_rows, dry_run, db_path, now, summary)

    except pb.PitchBookAPIError as e:
        # Any uncaught HTTP block: report credits + where it stopped.
        summary["stopped"] = True
        summary["stop_reason"] = f"PitchBookAPIError: {e}"

    return _finalize_comp_summary(summary)


def _insert_comp_rows(new_rows, dry_run, db_path, now, summary):
    """Idempotently insert comp-discovery rows (source=pitchbook_discover_comps)."""
    if dry_run or not new_rows:
        return
    with cursor_ctx(db_path) as (conn, cur):
        for r in new_rows:
            cur.execute(
                """
                INSERT OR IGNORE INTO partners
                    (partner_name, first_name, firm, email, tier,
                     channel, linkedin_url, notes, source,
                     pitchbook_id, pitchbook_firm_id, needs_enrichment, created_at)
                VALUES (?, ?, ?, ?, ?, 'email', ?, ?, 'pitchbook_discover_comps', ?, ?, ?, ?)
                """,
                (
                    r["partner_name"], r["first_name"], r["firm"],
                    r["email"].strip().lower(), None,
                    r["linkedin"] or None,
                    r["notes"],
                    r["firm_id"], r["firm_id"], r["needs_enrichment"], now,
                ),
            )
            if cur.rowcount:
                summary["inserted"] += 1


def _append_conflict_marker_to_notes(notes: str, conflict_tag: str) -> Optional[str]:
    """
    Pure helper: append the manual-review conflict marker to an existing notes
    string. Idempotent - returns None if the marker is ALREADY present (caller
    should skip the write), else returns the new notes string with the marker
    appended (existing notes preserved, not clobbered).
    """
    notes = notes or ""
    if MANUAL_REVIEW_MARKER in notes:
        return None
    return (notes + "; " if notes else "") + (
        f"{MANUAL_REVIEW_MARKER}; conflict={conflict_tag}. Do not send; manual review."
    )


def _flag_existing_conflict_rows(
    cur,
    conflict_tag: str,
    norm_firm: str,
    firm_id: str = "",
) -> int:
    """Workspace-backed outreach integration."""
    flagged = 0
    rows = cur.execute(
        "SELECT id, firm, notes, pitchbook_id, pitchbook_firm_id FROM partners"
    ).fetchall()
    for r in rows:
        # User-provided workspace configuration.
        # User-provided workspace configuration.
        if r["id"] in _CONFLICT_SWEEP_EXCLUDE_IDS:
            continue
        match = False
        if firm_id and (
            str(r["pitchbook_id"] or "").strip() == firm_id
            or str(r["pitchbook_firm_id"] or "").strip() == firm_id
        ):
            match = True
        elif _conflict_firm_match(r["firm"] or "") == conflict_tag:
            match = True
        if not match:
            continue
        new_notes = _append_conflict_marker_to_notes(r["notes"] or "", conflict_tag)
        if new_notes is None:
            continue  # already flagged - idempotent skip
        cur.execute(
            "UPDATE partners SET notes = ? WHERE id = ?",
            (new_notes, r["id"]),
        )
        flagged += 1
        logger.info(
            "flagged existing partner %s (%s) with %s conflict=%s",
            r["id"], r["firm"], MANUAL_REVIEW_MARKER, conflict_tag,
        )
    return flagged


# Existing partner ids that LOOK like a conflict alias-match but are NOT the
# conflict firm and must be excluded from the retroactive sweep. id 173 is
# User-provided workspace configuration.
# User-provided workspace configuration.
# User-provided workspace configuration.
_CONFLICT_SWEEP_EXCLUDE_IDS = frozenset()


def _sweep_existing_conflict_rows(dry_run: bool, db_path: Optional[Path]) -> dict:
    """Workspace-backed outreach integration."""
    result = {"flagged": 0, "by_tag": {}, "skipped_excluded": [], "already_flagged": 0}
    if dry_run:
        return result
    with cursor_ctx(db_path) as (conn, cur):
        rows = cur.execute(
            "SELECT id, firm, notes FROM partners"
        ).fetchall()
        for r in rows:
            tag = _conflict_firm_match(r["firm"] or "")
            if not tag:
                continue
            if r["id"] in _CONFLICT_SWEEP_EXCLUDE_IDS:
                result["skipped_excluded"].append(r["id"])
                continue
            if MANUAL_REVIEW_MARKER in (r["notes"] or ""):
                result["already_flagged"] += 1
                continue
            new_notes = _append_conflict_marker_to_notes(r["notes"] or "", tag)
            cur.execute(
                "UPDATE partners SET notes = ? WHERE id = ?",
                (new_notes, r["id"]),
            )
            result["flagged"] += 1
            result["by_tag"][tag] = result["by_tag"].get(tag, 0) + 1
            logger.info(
                "retroactive: flagged partner %s (%s) conflict=%s",
                r["id"], r["firm"], tag,
            )
    return result


# Pulls the recorded raw-USD median out of a comp-row notes string, e.g.
# "...; medianCheckSize=103999995.0; ..." -> 103999995.0
_MEDIAN_NOTE_RE = re.compile(r"medianCheckSize=([0-9][0-9.eE+]*)")
_MEGAFUND_EXCLUDE_NOTE = "excluded=megafund_median_{m}"


def _exclude_existing_megafund_rows(dry_run: bool, db_path: Optional[Path]) -> dict:
    """
    Re-evaluate already-inserted comp rows (source=pitchbook_discover_comps) under
    the megafund hard-exclude rule. Any row whose recorded medianCheckSize (parsed
    from notes) exceeds the hard-exclude threshold is marked excluded:
      - needs_enrichment = 0      (drops it from the auto-enrich queue)
      - notes += "; excluded=megafund_median_<short>"  (auditable)
      - emailable_checked_at = now (load-bearing: run.py send-gate requires
        emailable_checked_at IS NULL, so a non-null value removes the row from the
        send pool even though ne=0 would otherwise make it send-eligible)
      - dropped_reason = "excluded: megafund median ... (> configured hard-exclude)"

    Idempotent: rows already carrying an excluded= marker are skipped.
    Returns {"excluded": [ {id, firm, median} ], "skipped_already": n}.
    """
    out = {"excluded": [], "skipped_already": 0}
    if dry_run:
        return out
    with cursor_ctx(db_path) as (conn, cur):
        rows = cur.execute(
            "SELECT id, firm, notes FROM partners "
            "WHERE source = 'pitchbook_discover_comps'"
        ).fetchall()
        now = _now_iso()
        for r in rows:
            notes = r["notes"] or ""
            if "excluded=megafund_median_" in notes:
                out["skipped_already"] += 1
                continue
            m = _MEDIAN_NOTE_RE.search(notes)
            if not m:
                continue
            try:
                median = float(m.group(1))
            except ValueError:
                continue
            if median <= _CHECK_SIZE_HARD_EXCLUDE_USD:
                continue
            short = f"{median/1_000_000:.0f}M"
            new_notes = (notes + "; " if notes else "") + (
                _MEGAFUND_EXCLUDE_NOTE.format(m=short)
            )
            dropped = (
                f"excluded: megafund median {median} raw USD "
                f"(>{_CHECK_SIZE_HARD_EXCLUDE_USD} hard-exclude)"
            )
            cur.execute(
                "UPDATE partners SET needs_enrichment = 0, notes = ?, "
                "emailable_checked_at = ?, dropped_reason = ? WHERE id = ?",
                (new_notes, now, dropped, r["id"]),
            )
            out["excluded"].append(
                {"id": r["id"], "firm": r["firm"], "median": median}
            )
            logger.info(
                "retroactive megafund exclude: partner %s (%s) median=%s",
                r["id"], r["firm"], median,
            )
    return out


def _finalize_comp_summary(summary: dict) -> dict:
    """No-op hook for any final normalization; keeps return paths uniform."""
    return summary


def is_consumer_relevant(firm_name: str, description: str, screener_pass: bool = False) -> bool:
    # Discovery is sector neutral; callers choose their own comparable companies.
    return bool(firm_name.strip() or description.strip())


def is_target_profile(firm_name: str, description: str, investor_type: str = "", location: str = "", last_deal_date: str = "", aum_millions: float = 0) -> tuple[bool, str]:
    return (bool(firm_name.strip()), "" if firm_name.strip() else "missing_firm")


def partner_quality_score(name: str, bio: str, linkedin_url: str, twitter_handle: str = "", last_deal_date: str = "", company_count: int = 0) -> int:
    return min(100, (20 if name else 0) + (30 if bio else 0) + (20 if linkedin_url else 0) + (10 if twitter_handle else 0) + (10 if last_deal_date else 0) + min(10, max(0, company_count) * 2))


def _discovery_config() -> dict:
    """Optional data/discovery.json: known_comp_pbids, search_comps, conflict_firms.

    No comparable companies or exclusion roster are bundled. The user supplies
    their own names, IDs and conflicts in this confined workspace file.
    """
    import json
    path = workspace_path("data/discovery.json")
    try:
        if path.stat().st_size > 65536:
            raise ValueError("Discovery config is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return value if isinstance(value, dict) else {}
