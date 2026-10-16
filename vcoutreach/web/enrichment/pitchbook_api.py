"""Workspace-backed outreach integration."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import random
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_THIS_FILE = Path(__file__).resolve()
from vcoutreach.web.vendor import workspace_root, workspace_path, data_path, validated_url, setting_value
from vcoutreach.web.config import WorkspacePath
PROJECT_ROOT = WorkspacePath()
STORAGE_FILE = WorkspacePath("data", ".pitchbook_session.json")

# ── Constants ──────────────────────────────────────────────────────────────────
BASE_URL = "https://my.pitchbook.com/web-api"

# Throttle between requests (seconds)  -  be conservative to avoid detection
_THROTTLE_MIN = 3.0
_THROTTLE_MAX = 7.0

# Chrome UA matching the MCP browser session
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

# ── Firm-name fuzzy matcher ────────────────────────────────────────────────────

# Known aliases: tuples are always (shorter/abbrev, longer/full).
# Lookup normalises both orderings so direction doesn't matter.
_FIRM_ALIASES = {}

# Generic words that add noise to token matching
_STOP_TOKENS = {"capital", "ventures", "venture", "partners", "fund", "vc",
                "management", "llc", "inc", "group", "co"}


def firm_matches(pb_firm: str, our_firm: str) -> tuple[bool, float]:
    """Workspace-backed outreach integration."""
    if not pb_firm or not our_firm:
        return True, 0.50          # accept if either is missing  -  can't reject blindly

    a = pb_firm.lower().strip()
    b = our_firm.lower().strip()

    if a == b:
        return True, 0.95

    # Alias lookup (both orderings)
    if (a, b) in _FIRM_ALIASES or (b, a) in _FIRM_ALIASES:
        return True, 0.85

    # Substring match
    if a in b or b in a:
        return True, 0.75

    # Token overlap (significant words only)
    a_tokens = set(re.findall(r"[a-z0-9]+", a)) - _STOP_TOKENS
    b_tokens = set(re.findall(r"[a-z0-9]+", b)) - _STOP_TOKENS

    if a_tokens and b_tokens:
        overlap = a_tokens & b_tokens
        # Require at least 1 shared token AND it covers ≥50% of the smaller set
        if overlap and len(overlap) >= min(len(a_tokens), len(b_tokens)) * 0.5:
            return True, 0.65

    # No string match at all  -  PB may be returning a portfolio company or board role
    # as primary employer.  Accept at low confidence and flag, don't hard-drop.
    return True, 0.55


# ── Lazy import curl_cffi (install: pip install curl_cffi) ─────────────────────
class PitchBookAPIError(Exception):
    """Raised when an API call fails or returns non-JSON / error status."""


def _load_session():
    """
    Build a curl_cffi Session from cookies in .pitchbook_storage.json.

    Raises:
        ImportError: if curl_cffi is not installed
        FileNotFoundError: if storage file is missing
        PitchBookAPIError: if no PitchBook cookies found
    """
    try:
        from curl_cffi import requests as _cffi_requests
    except ImportError as exc:
        raise ImportError("PitchBook needs curl-cffi. Run: pip install -e \".[integrations]\"") from exc

    session_file = data_path(setting_value("PITCHBOOK_SESSION_FILE", str(STORAGE_FILE)))
    state = json.loads(session_file.read_text(encoding="utf-8"))
    cookies = state.get("cookies", [])

    # Extract PitchBook-domain cookies only
    pb_cookies: dict[str, str] = {}
    for c in cookies:
        domain = c.get("domain", "")
        if domain.lower().lstrip(".") in {"pitchbook.com", "my.pitchbook.com"}:
            pb_cookies[c["name"]] = c["value"]

    if not pb_cookies.get("SESSION"):
        raise PitchBookAPIError(
            "No PitchBook SESSION cookie found in storage file. "
            "Run `python run.py pitchbook-auth` to re-authenticate."
        )

    session = _cffi_requests.Session(impersonate="chrome146")
    for name, value in pb_cookies.items():
        session.cookies.set(name, value, domain="my.pitchbook.com")

    session.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://my.pitchbook.com/",
            "x-requested-with": "XMLHttpRequest",
        }
    )

    return session


def _live_pitchbook_cookies(session) -> dict[str, str]:
    """
    Pull the current pitchbook-domain cookies out of a curl_cffi session.

    Returns a name -> value map. Tolerates a minimal fake session in tests:
    tries the cookie jar first (which exposes domain), then falls back to
    iterating the cookies mapping directly.
    """
    live: dict[str, str] = {}

    # curl_cffi Cookies object wraps a jar; jar entries expose .name/.value/.domain
    jar = getattr(getattr(session, "cookies", None), "jar", None)
    if jar is not None:
        try:
            for c in jar:
                domain = (getattr(c, "domain", "") or "")
                if domain.lower().lstrip(".") in {"pitchbook.com", "my.pitchbook.com"}:
                    live[c.name] = c.value
            if live:
                return live
        except Exception:  # noqa: BLE001 - fall through to mapping iteration
            pass

    # Fallback: iterate the cookies mapping (name -> value), no domain available
    try:
        for name, value in session.cookies.items():
            live[name] = value
    except Exception:  # noqa: BLE001
        pass
    return live


def _persist_session_cookies(session) -> None:
    """
    Write the live session's pitchbook cookies back to STORAGE_FILE.

    Cloudflare rotates __cf_bm / cf_clearance via Set-Cookie on each response.
    Persisting them here keeps SESSION, cf_clearance, __cf_bm, pbCust, etc. fresh
    across runs, so one export (that includes cf_clearance) lasts ~24h.

    Behaviour:
      - Preserves any non-pitchbook cookies already in the file untouched.
      - Updates existing pitchbook cookie values in place; inserts new ones.
      - Only writes if a pitchbook cookie value actually changed (cheap no-op
        otherwise).
      - Writes atomically (temp file then replace).
      - Never raises: logs and returns on any error.
    """
    try:
        live = _live_pitchbook_cookies(session)
        if not live:
            return

        # Load existing file (tolerate missing/corrupt)
        existing_cookies: list[dict] = []
        try:
            session_file = data_path(setting_value("PITCHBOOK_SESSION_FILE", str(STORAGE_FILE)))
            state = json.loads(session_file.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                existing_cookies = list(state.get("cookies", []) or [])
            else:
                state = {"cookies": existing_cookies, "origins": []}
        except (FileNotFoundError, ValueError):
            state = {"cookies": existing_cookies, "origins": []}

        changed = False
        seen_pb_names: set[str] = set()

        # Update existing pitchbook cookie entries in place
        for entry in existing_cookies:
            name = entry.get("name")
            domain = (entry.get("domain", "") or "")
            if name in live and "pitchbook" in domain.lower():
                seen_pb_names.add(name)
                if entry.get("value") != live[name]:
                    entry["value"] = live[name]
                    changed = True

        # Insert pitchbook cookies that were not already present
        for name, value in live.items():
            if name in seen_pb_names:
                continue
            existing_cookies.append({
                "name": name,
                "value": value,
                "domain": ".my.pitchbook.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            })
            changed = True

        if not changed:
            return

        state["cookies"] = existing_cookies
        state.setdefault("origins", [])

        # Authentication material must not become world-readable when a
        # rotated cookie triggers an atomic rewrite.  A fixed .tmp name also
        # risks colliding with another refresh process.
        import tempfile

        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".pitchbook-",
                suffix=".tmp", dir=session_file.parent, delete=False,
            ) as tmp_file:
                tmp_name = tmp_file.name
                tmp_file.write(json.dumps(state, indent=2))
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, session_file)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)
        logger.debug("Persisted %d pitchbook cookies to %s", len(live), STORAGE_FILE)
    except Exception as exc:  # noqa: BLE001 - must never break a request
        logger.warning("Failed to persist session cookies: %s", exc)


def _try_refresh_cookies_from_chrome() -> bool:
    """
    Attempt to refresh cookies from real Chrome via browser-cookie3.
    Returns True on success, False on failure.
    Called automatically on first 401 response.
    """
    logger.info("Attempting auto-refresh of PitchBook cookies from real Chrome...")
    try:
        import subprocess
        import sys
        script = Path(__file__).resolve().parents[1] / "scripts" / "pb_cookie_refresher.py"
        if not script.exists():
            logger.warning("Cookie extraction script not found at %s", script)
            return False

        result = subprocess.run(
            [sys.executable, str(script), "--once", "--no-navigate"],
            capture_output=True,
            env={**os.environ, "OUTREACH_HOME": str(workspace_root())},
            cwd=workspace_root(),
            text=True,
            timeout=30, allow_redirects=False,
        )
        if result.returncode == 0:
            logger.info("Cookie auto-refresh succeeded.")
            return True
        else:
            logger.warning("Cookie auto-refresh failed (exit %d): %s", result.returncode, result.stderr[:300])
            return False
    except Exception as exc:
        logger.warning("Cookie auto-refresh exception: %s", exc)
        return False


def _get(session, path: str, params: Optional[dict] = None, _retry_on_401: bool = True) -> dict:
    """GET a web-api endpoint, return parsed JSON. Raises PitchBookAPIError on failure.

    On first 401: auto-refresh cookies from real Chrome and retry once.
    """
    url = f"{BASE_URL}{path}"
    validated_url(url, hosts=("my.pitchbook.com",))
    resp = session.get(url, params=params, timeout=20, allow_redirects=False)

    if resp.status_code == 401 and _retry_on_401:
        logger.warning("401 from %s  -  attempting cookie auto-refresh", url)
        refreshed = _try_refresh_cookies_from_chrome()
        if refreshed:
            new_session = _load_session()
            for name, val in new_session.cookies.items():
                session.cookies.set(name, val, domain="my.pitchbook.com")
            resp = session.get(url, params=params, timeout=20, allow_redirects=False)
            if resp.status_code == 200:
                logger.info("Retry after cookie refresh succeeded.")
                _persist_session_cookies(session)
                return resp.json()
        raise PitchBookAPIError(
            f"401 Unauthorized from {url}. "
            "Run `python run.py pitchbook-cookies` to refresh from real Chrome."
        )

    if resp.status_code == 403:
        # Surface the response body. Not every 403 is a Cloudflare challenge:
        # PitchBook returns 403 with a JSON body {"type":"PROFILE_INVALID_PB_ID",...}
        # for a bad/stale pbId. Including the body lets callers (discovery.py)
        # distinguish an invalid-id 403 from a genuine Cloudflare HTML block and
        # self-heal instead of hard-stopping the whole run.
        raise PitchBookAPIError(
            f"403 on {url}. body={resp.text[:300]} "
            "(if Cloudflare: refresh cookies with `python run.py pitchbook-cookies` "
            "(real Chrome) or `python run.py pitchbook-auth`.)"
        )
    if resp.status_code != 200:
        raise PitchBookAPIError(
            f"HTTP {resp.status_code} from {url}: {resp.text[:300]}"
        )
    _persist_session_cookies(session)
    return resp.json()


def _post(session, path: str, body: dict, _retry_on_401: bool = True) -> dict:
    """POST a web-api endpoint with JSON body, return parsed JSON.

    On first 401: attempts to auto-refresh cookies from real Chrome, rebuilds
    the session, and retries ONCE. If retry also fails, raises PitchBookAPIError.
    """
    url = f"{BASE_URL}{path}"
    validated_url(url, hosts=("my.pitchbook.com",))
    resp = session.post(url, json=body, timeout=20, allow_redirects=False)

    if resp.status_code == 401 and _retry_on_401:
        logger.warning("401 from %s  -  attempting cookie auto-refresh", url)
        refreshed = _try_refresh_cookies_from_chrome()
        if refreshed:
            new_session = _load_session()
            # Copy cookies to existing session object for callers holding a reference
            for name, val in new_session.cookies.items():
                session.cookies.set(name, val, domain="my.pitchbook.com")
            resp = session.post(url, json=body, timeout=20, allow_redirects=False)
            if resp.status_code == 200:
                logger.info("Retry after cookie refresh succeeded.")
                _persist_session_cookies(session)
                return resp.json()
        raise PitchBookAPIError(
            f"401 Unauthorized from {url}. "
            "Cookies are stale or invalid. "
            "Run `python run.py pitchbook-cookies` to refresh from real Chrome."
        )

    if resp.status_code == 403:
        raise PitchBookAPIError(
            f"403 Cloudflare block on {url}. "
            "Refresh cookies with `python run.py pitchbook-cookies` (real Chrome) "
            "or `python run.py pitchbook-auth`."
        )
    if resp.status_code != 200:
        raise PitchBookAPIError(
            f"HTTP {resp.status_code} from {url}: {resp.text[:300]}"
        )
    _persist_session_cookies(session)
    return resp.json()


def _throttle():
    """Sleep a random interval between requests."""
    time.sleep(random.uniform(_THROTTLE_MIN, _THROTTLE_MAX))


# ── Public API ──────────────────────────────────────────────────────────────────

def search_investor_by_name(name: str, session=None) -> Optional[dict]:
    """
    Search PitchBook for an investor by name using the general search endpoint.

    Returns the top INVESTOR match as a dict with keys:
        id, name, description, location, email, investorStatus,
        totalInvestments, investmentsTtm, website, companyName, companyId

    Returns None if no INVESTOR result found.

    Uses 1 PitchBook view credit.
    """
    s = session or _load_session()
    body = {
        "savedConferenceSearchAllowed": True,
        "searchRequest": {"limit": 15, "offset": 0, "query": name},
        "timeZoneOffset": "-05:00",
    }
    data = _post(s, "/general-search/search/mixed", body)

    for item in data.get("items", []):
        if item.get("type") != "INVESTOR":
            continue
        val = item["value"]
        pr = val.get("profileResult", {}) or {}
        sparse = val.get("sparseData", {}) or {}
        related = val.get("relatedPerson") or {}
        return {
            "id": pr.get("id"),
            "name": pr.get("name"),
            "description": pr.get("description", ""),
            "location": pr.get("location", ""),
            "email": related.get("email", "") or "",
            "linkedIn": related.get("linkedInLink", "") or "",
            "investorStatus": sparse.get("investorStatus", ""),
            "totalInvestments": sparse.get("totalInvestments"),
            "investmentsTtm": sparse.get("investmentsTtm"),
            "website": val.get("website", ""),
            "companyName": related.get("companyName", ""),
            "companyId": related.get("companyId", ""),
            "typeDescription": pr.get("typeDescription", ""),
        }

    return None


def get_investor_profile(profile_id: str, session=None) -> dict:
    """
    Fetch full investor profile: bio, stats, contact info, and recent investments.

    Combines 3 endpoint calls:
      1. general-info (bio + stats)
      2. contact-info (email, phone, LinkedIn)
      3. investments?page=1&pageSize=25 (deal history)

    Returns a merged dict. Uses ~3 PitchBook view credits.
    """
    s = session or _load_session()

    # 1. General info
    _throttle()
    general = _get(s, f"/profile-platform-bff/profiles/{profile_id}/investor/general-info")

    # 2. Contact info (some profiles return empty body  -  treat as no contact data)
    _throttle()
    try:
        contact_data = _get(s, f"/profile-platform-bff/profiles/{profile_id}/investor/contact-info")
    except (ValueError, Exception) as _ce:
        import json as _json
        if isinstance(_ce, _json.JSONDecodeError):
            logger.warning("contact-info empty body for %s  -  skipping contact fields", profile_id)
            contact_data = {}
        else:
            raise
    primary = contact_data.get("primaryContact", {})
    office = contact_data.get("primaryOffice", {})

    # 3. Investments (first page)
    _throttle()
    inv_data = _get(
        s,
        f"/profile-platform-bff/profiles/{profile_id}/investor/investments",
        params={"page": 1, "pageSize": 25},
    )
    investments = []
    for inv in inv_data.get("investments", []):
        investments.append(
            {
                "company": inv["businessEntity"]["name"],
                "companyId": inv["businessEntity"]["pbId"],
                "dealDate": inv["round"].get("dealDate"),
                "dealType": inv["round"].get("shortDealType"),
                "amount": (inv["round"].get("amount") or {}).get("amount"),
                "currency": (inv["round"].get("amount") or {}).get("currency"),
                "industry": inv.get("pitchBookIndustry", {}).get("description"),
                "formerInvestment": inv.get("formerInvestment", False),
            }
        )

    # Extract angel-specific fields
    angel = general.get("angelDetails", {})

    return {
        "id": profile_id,
        "bio": general.get("description", ""),
        "primaryType": (general.get("primaryType") or {}).get("description", ""),
        "investorStatus": (general.get("status") or {}).get("description", ""),
        "totalInvestmentsCount": general.get("totalInvestmentsCount"),
        "totalExitsCount": general.get("totalExitsCount"),
        "activeInvestedCompaniesCount": general.get("activeInvestedCompaniesCount"),
        "investmentsLast12Months": general.get("investmentsCountInTheLast12Months"),
        "medianCheckSize": (general.get("medianInvestmentAmount") or {}).get("amount"),
        "medianValuation": (general.get("medianInvestmentValuation") or {}).get("amount"),
        "employeeCount": general.get("employeeCount"),
        "primaryPosition": angel.get("primaryPosition", ""),
        "gender": angel.get("gender", ""),
        "education": angel.get("educationDegrees", []),
        # Contact
        "firstName": primary.get("firstName", ""),
        "lastName": primary.get("lastName", ""),
        "email": primary.get("email", ""),
        "phone": primary.get("phone", ""),
        "linkedIn": primary.get("linkedInLink", ""),
        "officeCity": office.get("name", ""),
        "officeAddress": office.get("address", []),
        # Deals
        "investments": investments,
        "totalInvestmentsFetched": inv_data.get("total"),
    }


def get_company_investors(
    company_id: str,
    session=None,
    investor_types: Optional[list[str]] = None,
) -> list[dict]:
    """
    Get all investors who participated in a company's rounds.

    Endpoint (verified via network capture on koodos labs 437146-93):
      GET /profile-platform-bff/profiles/{companyId}/company/investors/{type}?page=1&pageSize=25

    Investor types available: ACTIVE, FORMER, NON, BIDDING, PENDING_BUY_SIDE,
    PENDING_SELL_SIDE, SECONDARY, ACQUIRERS, ADD_ON_SPONSORS, FAILED, OTHER.
    Defaults to ['ACTIVE', 'FORMER'] which covers the standard investor history.

    Returns list of dicts with: investorId, investorName, investorType,
    investorSince, roundDate, dealType, dealAmount.
    Uses 1 PitchBook view credit per type fetched.

    Note: company_id format is like "437146-93" (from search results).
    """
    s = session or _load_session()

    if investor_types is None:
        investor_types = ["ACTIVE", "FORMER"]

    seen_ids: set[str] = set()
    results: list[dict] = []

    for itype in investor_types:
        _throttle()
        try:
            data = _get(
                s,
                f"/profile-platform-bff/profiles/{company_id}/company/investors/{itype}",
                params={"page": 1, "pageSize": 25},
            )
        except PitchBookAPIError as e:
            logger.warning(
                "Company investors/%s endpoint failed for %s: %s", itype, company_id, e
            )
            continue

        for item in data.get("investors", []):
            inv = item.get("investor", {})
            iid = inv.get("pbId")
            if not iid or iid in seen_ids:
                continue
            seen_ids.add(iid)

            # Pull deal metadata from first round entry (most recent)
            rounds = item.get("rounds", [])
            first_round = rounds[0] if rounds else {}
            round_info = first_round.get("round", {})
            amount_info = round_info.get("amount") or {}

            results.append(
                {
                    "investorId": iid,
                    "investorName": inv.get("name", ""),
                    "investorType": item.get("investorType", ""),
                    "investorSince": item.get("investorSince"),
                    "roundDate": round_info.get("roundDate"),
                    "dealType": round_info.get("primaryTypeWithSeries", ""),
                    "dealAmount": amount_info.get("amount"),
                    "currency": amount_info.get("currency"),
                }
            )

    return results


def find_coinvestors_of_investor(investor_id: str, session=None, page_size: int = 25) -> list[dict]:
    """
    Get co-investors for a given PitchBook investor ID.

    Returns list of dicts with: investorId, investorName, sharedRounds (count), sharedCompanies (count).
    Uses 1 PitchBook view credit.
    """
    s = session or _load_session()
    _throttle()

    data = _post(
        s,
        f"/profiles/{investor_id}/investor/co-investors",
        {},
    )
    # Note: URL does not go through profile-platform-bff  -  different base path

    results = []
    for item in data.get("coInvestors", {}).get("content", []):
        inv = item.get("investor", {})
        results.append(
            {
                "investorId": inv.get("pbId"),
                "investorName": inv.get("name"),
                "sharedRounds": len(item.get("rounds", [])),
                "sharedCompanies": len(item.get("companies", [])),
            }
        )
    return results


def find_coinvestors_of_comps(comp_names: list[str], session=None) -> list[dict]:
    """Workspace-backed outreach integration."""
    s = session or _load_session()
    investor_counts: dict[str, dict] = {}

    for comp_name in comp_names:
        logger.info("Searching for company: %s", comp_name)
        _throttle()

        # Search for company
        body = {
            "savedConferenceSearchAllowed": False,
            "searchRequest": {"limit": 5, "offset": 0, "query": comp_name},
            "timeZoneOffset": "-05:00",
        }
        try:
            data = _post(s, "/general-search/search/mixed", body)
        except PitchBookAPIError as e:
            logger.warning("Search failed for %s: %s", comp_name, e)
            continue

        # Find top COMPANY result
        company_id = None
        for item in data.get("items", []):
            if item.get("type") == "COMPANY":
                company_id = item["value"]["profileResult"]["id"]
                break

        if not company_id:
            logger.warning("No company result found for: %s", comp_name)
            continue

        logger.info("Found company ID %s for %s", company_id, comp_name)

        # Get investors for this company
        try:
            investors = get_company_investors(company_id, session=s)
        except PitchBookAPIError as e:
            logger.warning("Failed to get investors for %s (%s): %s", comp_name, company_id, e)
            continue

        for inv in investors:
            iid = inv.get("investorId")
            if not iid:
                continue
            if iid not in investor_counts:
                investor_counts[iid] = {
                    "investorId": iid,
                    "investorName": inv.get("investorName", ""),
                    "companyCount": 0,
                    "companies": [],
                }
            investor_counts[iid]["companyCount"] += 1
            investor_counts[iid]["companies"].append(comp_name)

    # Sort by how many comp companies they invested in
    return sorted(investor_counts.values(), key=lambda x: x["companyCount"], reverse=True)


# ── Advanced Screener Bulk Discovery ───────────────────────────────────────────

# US state/region suffixes used to detect US-headquartered investors in the
# screener data (the hqLocation column is a free-text "City, ST" string).
_US_LOCATION_SUFFIXES = (
    ", CA", ", NY", ", MA", ", TX", ", CO", ", WA", ", IL", ", GA", ", FL",
    ", NC", ", OH", ", MI", ", MN", ", NV", ", UT", ", PA", ", AZ", ", OR",
    ", DC", ", MD", ", CT", ", VA", "United States",
)

# Investor types we accept from the screener (firm-level primaryInvestorType).
_ACCEPTED_INVESTOR_TYPES = frozenset([
    "Venture Capital",
    "Angel Group",
    "Corporate Venture Capital",
    "Family Office",
    "Micro VC",
    "Accelerator/Incubator",  # User-provided workspace configuration.
])


def _cv(column_values: dict, key: str) -> str:
    """Extract first string value from columnValues dict."""
    entries = column_values.get(key, [])
    if not entries:
        return ""
    entry = entries[0]
    return (
        entry.get("value")
        or entry.get("name")
        or ""
    )


def _screener_create_session(session, entry_point_key: str = "TOP_SEED_INVESTORS") -> str:
    """
    Create a PitchBook advanced-search session and run it.

    Returns searchId (e.g. "s649287850").

    Uses the entry_point_key to pre-filter the result set:
      - TOP_SEED_INVESTORS: ~13,000 investors focused on Seed stage
      - VC_INVESTORS:       ~46,000 VC firms

    No separate run() call needed  -  caller must run the search first if they
    want server-side filters; here we rely on the entry-point pre-filter and
    do client-side filtering for US/type.
    """
    body = {"entryPointKey": entry_point_key, "searchType": "INVESTOR"}
    resp = session.post(
        f"{BASE_URL}/advanced-search-api/searches?ignoreUserPreferences=true",
        json=body,
        timeout=20, allow_redirects=False,
    )
    if resp.status_code != 200:
        raise PitchBookAPIError(f"Screener session creation failed: HTTP {resp.status_code}: {resp.text[:200]}")
    search_id = resp.json().get("id")
    if not search_id:
        raise PitchBookAPIError(f"No searchId in response: {resp.text[:200]}")

    # Run the search to populate the result table
    run_resp = session.post(
        f"{BASE_URL}/advanced-search-api/searches/{search_id}/run"
        "?resetTrigger=PAGE_LOAD&resetActiveTab=false",
        json={},
        timeout=30, allow_redirects=False,
    )
    if run_resp.status_code not in (200, 204):
        raise PitchBookAPIError(f"Screener run failed: HTTP {run_resp.status_code}")

    return search_id


def _screener_fetch_page(session, search_id: str, page: int, page_size: int) -> list[dict]:
    """
    Fetch one page of investor rows from the screener table.

    Returns a list of raw dicts with keys:
        profile_id, name, investor_type, location, contact_name, contact_pb_id,
        contact_email, linkedin_url
    """
    resp = session.post(
        f"{BASE_URL}/advanced-search-api/tables/{search_id}.investor.data_set/data"
        f"?page={page}&pageSize={page_size}&alertMode=false&recentUpdatesMode=false",
        json={},
        timeout=60, allow_redirects=False,
    )
    if resp.status_code == 429:
        raise PitchBookAPIError("ACTUAL 429 from PitchBook  -  stop and report.")
    if resp.status_code != 200:
        raise PitchBookAPIError(
            f"Screener data fetch failed (page {page}): HTTP {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    rows = data.get("dataRows", [])

    parsed = []
    for row in rows:
        cv = row.get("columnValues", {})

        # Firm name + ID
        inv_name_entries = cv.get("investorName", [])
        name = inv_name_entries[0].get("name", "") if inv_name_entries else ""
        pb_id = row.get("pbId", "")

        investor_type = _cv(cv, "primaryInvestorType")
        location = _cv(cv, "hqLocation")

        # Primary contact (firm-level)  -  name + pbId
        contact_entries = cv.get("primaryContact", [])
        contact_name = contact_entries[0].get("name", "") if contact_entries else ""
        contact_pb_id = contact_entries[0].get("pbId", "") if contact_entries else ""

        # Email if exposed
        contact_email = _cv(cv, "primaryContactEmail")

        parsed.append(
            {
                "profile_id": pb_id,
                "name": name,
                "investor_type": investor_type,
                "location": location,
                "contact_name": contact_name,
                "contact_pb_id": contact_pb_id,
                "contact_email": contact_email,
                "linkedin_url": "",  # not in screener table; enriched separately
            }
        )

    return parsed


def screener_search(
    filters: Optional[dict] = None,
    page_size: int = 50,
    max_pages: int = 4,
    session=None,
) -> list[dict]:
    """
    Hit PitchBook advanced screener with filters. Returns dedup'd list of investor entries.

    Uses TOP_SEED_INVESTORS entry point (~13k results, pre-filtered to seed stage)
    then applies client-side filters for US location and accepted investor types.

    Each returned dict has keys:
        profile_id, name, firm, investor_type, location,
        contact_name, contact_pb_id, linkedin_url, last_deal_date

    filters (optional dict  -  unused in current implementation, reserved for future
    server-side criteria expansion):
        entry_point: str  -  one of TOP_SEED_INVESTORS, VC_INVESTORS (default: TOP_SEED_INVESTORS)
        us_only: bool (default True)  -  drop non-US locations
        accepted_types: list[str]  -  investor type whitelist (default: _ACCEPTED_INVESTOR_TYPES)

    Throttles 3s between page fetches. Caps at max_pages * page_size candidates
    before dedup.

    Raises PitchBookAPIError on HTTP 429 (real rate-limit  -  stop immediately).
    """
    filters = filters or {}
    s = session or _load_session()
    entry_point = filters.get("entry_point", "TOP_SEED_INVESTORS")
    us_only = filters.get("us_only", True)
    accepted_types = set(filters.get("accepted_types", _ACCEPTED_INVESTOR_TYPES))

    logger.info("screener_search: creating session (entry_point=%s)", entry_point)
    _throttle()
    search_id = _screener_create_session(s, entry_point_key=entry_point)
    logger.info("screener_search: searchId=%s", search_id)

    seen_ids: set[str] = set()
    all_results: list[dict] = []

    for page_num in range(1, max_pages + 1):
        logger.info("screener_search: fetching page %d/%d (page_size=%d)", page_num, max_pages, page_size)
        _throttle()
        rows = _screener_fetch_page(s, search_id, page=page_num, page_size=page_size)
        logger.info("screener_search: page %d returned %d rows", page_num, len(rows))

        if not rows:
            logger.info("screener_search: empty page %d, stopping", page_num)
            break

        for row in rows:
            pid = row["profile_id"]
            if not pid or pid in seen_ids:
                continue

            # Client-side: US filter
            if us_only:
                loc = row.get("location", "")
                if not any(loc.endswith(suf) or suf in loc for suf in _US_LOCATION_SUFFIXES):
                    continue

            # Client-side: investor type filter
            if accepted_types and row.get("investor_type") not in accepted_types:
                continue

            seen_ids.add(pid)
            all_results.append(
                {
                    "profile_id": pid,
                    "name": row["name"],
                    "firm": row["name"],  # screener returns firm name; individual resolved later
                    "investor_type": row["investor_type"],
                    "location": row["location"],
                    "contact_name": row.get("contact_name", ""),
                    "contact_pb_id": row.get("contact_pb_id", ""),
                    "linkedin_url": row.get("linkedin_url", ""),
                    "last_deal_date": "",  # not in screener; enriched from profile if needed
                }
            )

    logger.info("screener_search: done  -  %d unique US investors across %d pages", len(all_results), page_num)
    return all_results


# ── Backend feature flag ────────────────────────────────────────────────────────
#
# PITCHBOOK_BACKEND=browser (default) → use Playwright persistent-profile backend
# PITCHBOOK_BACKEND=curl_cffi         → use legacy curl_cffi / cookie-file backend
#
# The override replaces module-level names so callers that do:
#   from app.enrichment.pitchbook_api import search_investor_by_name
# inside function bodies (lazy imports) automatically get the Playwright version.
#
# To switch back temporarily:
#   PITCHBOOK_BACKEND=curl_cffi python run.py pitchbook-enrich --partner-id 3

_PITCHBOOK_BACKEND = os.getenv("PITCHBOOK_BACKEND", "browser").lower().strip()

if _PITCHBOOK_BACKEND == "browser":
    try:
        from vcoutreach.web.enrichment.pitchbook_browser import (  # type: ignore
            search_investor_by_name as _browser_search,
            get_investor_profile as _browser_get_profile,
        )
        # Re-bind module-level names to browser implementations.
        # The curl_cffi originals are preserved as _curl_search / _curl_get_profile
        # below for the fallback path and for direct import by advanced callers.
        _curl_search = search_investor_by_name
        _curl_get_profile = get_investor_profile
        search_investor_by_name = _browser_search  # type: ignore[assignment]
        get_investor_profile = _browser_get_profile  # type: ignore[assignment]
        logger.info(
            "pitchbook_api: PITCHBOOK_BACKEND=browser  -  using Playwright persistent-profile backend"
        )
    except ImportError as _browser_import_err:
        logger.warning(
            "pitchbook_api: PITCHBOOK_BACKEND=browser but pitchbook_browser import failed (%s) "
            " -  falling back to curl_cffi backend",
            _browser_import_err,
        )
else:
    logger.debug("pitchbook_api: PITCHBOOK_BACKEND=%s  -  using curl_cffi backend", _PITCHBOOK_BACKEND)


# ── CLI test ────────────────────────────────────────────────────────────────────

