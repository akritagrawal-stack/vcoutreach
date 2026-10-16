"""Workspace-backed outreach integration."""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_THIS_FILE = Path(__file__).resolve()
from vcoutreach.web.vendor import workspace_root, workspace_path, data_path, validated_url
from vcoutreach.web.config import WorkspacePath
PROJECT_ROOT = WorkspacePath()

_DEFAULT_PROFILE_DIR = str(data_path("data/.pitchbook_profile"))


def _resolve_profile_dir() -> str:
    return str(data_path(os.getenv("PITCHBOOK_PROFILE_DIR", "data/.pitchbook_profile")))


# ── Exceptions (re-exported for callers) ──────────────────────────────────────

class PitchBookBrowserError(Exception):
    """General browser-backend error."""


class PitchBookBrowserAuthRequired(PitchBookBrowserError):
    """Raised when persistent profile has no valid session."""


# ── Main class ─────────────────────────────────────────────────────────────────

class PitchBookBrowser:
    """Workspace-backed outreach integration."""

    def __init__(self, profile_dir: Optional[str] = None):
        self.profile_dir = str(data_path(profile_dir)) if profile_dir else _resolve_profile_dir()
        self._sess = None   # PitchBookSession instance (after __enter__)
        self._sess_ctx = None  # the original context-manager object (for __exit__)

    # ── Context manager ────────────────────────────────────────────────────────

    def __enter__(self) -> "PitchBookBrowser":
        from vcoutreach.web.enrichment.pitchbook import PitchBookSession  # type: ignore
        # Always launch headed to avoid Cloudflare/Turnstile detection.
        # PITCHBOOK_HEADED env var (default "1") controls this; set to "0" only
        # in environments where a display is guaranteed bot-safe.
        headed = os.getenv("PITCHBOOK_HEADED", "1") == "1"
        sess = PitchBookSession(profile_dir=self.profile_dir, headed=headed)
        self._sess_ctx = sess       # hold for __exit__
        self._sess = sess.__enter__()  # PitchBookSession.__enter__ returns self
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._sess_ctx is not None:
            try:
                self._sess_ctx.__exit__(exc_type, exc, tb)
            except Exception as ex:
                logger.warning("PitchBookBrowser: error during session close: %s", ex)

    # ── Auth check ─────────────────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """Return True if the persistent profile has a live PitchBook session."""
        if self._sess is None:
            raise PitchBookBrowserError("Must be used as a context manager")
        return self._sess.is_authenticated()

    # ── Search ─────────────────────────────────────────────────────────────────

    def search_investor_by_name(self, name: str) -> Optional[dict]:
        """
        Search PitchBook for an investor by name and return a dict matching
        the curl_cffi response shape from pitchbook_api.search_investor_by_name.

        Keys returned:
            id, name, description, location, email, linkedIn,
            investorStatus, totalInvestments, investmentsTtm,
            website, companyName, companyId, typeDescription

        Fields unavailable from HTML (API-only) are returned as '' or None.

        Returns None if no result found.
        """
        if self._sess is None:
            raise PitchBookBrowserError("Must be used as a context manager")

        # search_person returns {url, id, name, firm} or None
        search_result = self._sess.search_person(name)
        if search_result is None:
            logger.info("PitchBookBrowser: no search result for %r", name)
            return None

        profile_url = search_result.get("url", "")
        pb_id = search_result.get("id")

        # Scrape the profile page for richer data
        profile_data: dict = {}
        if profile_url:
            try:
                profile_data = self._sess.scrape_person_profile(profile_url)
            except Exception as exc:
                logger.warning(
                    "PitchBookBrowser: profile scrape failed for %s: %s", profile_url, exc
                )
                profile_data = {}

        # Map to curl_cffi output shape
        bio = profile_data.get("bio", "")
        current_role = profile_data.get("current_role", "")

        # Try to extract email from profile HTML (PB sometimes shows it in page)
        email = _extract_email_from_profile(profile_data)

        # LinkedIn from profile data
        linkedin = _extract_linkedin_from_profile(profile_data)

        # Firm from current_role (often "Title · FirmName" or just "FirmName")
        firm = _parse_firm_from_role(current_role)

        return {
            "id": pb_id,
            "name": name,
            "description": bio,
            "location": "",          # not reliably available in HTML
            "email": email,
            "linkedIn": linkedin,
            "investorStatus": "",    # API-only field
            "totalInvestments": None,  # API-only field
            "investmentsTtm": None,    # API-only field
            "website": "",           # API-only field
            "companyName": firm,
            "companyId": "",         # API-only field
            "typeDescription": "",   # API-only field
            # Browser-only extras (bonus fields not in curl_cffi response)
            "_profile_url": profile_url,
            "_recent_deals": profile_data.get("recent_deals", []),
            "_board_seats": profile_data.get("board_seats", []),
            "_sectors": profile_data.get("sectors", []),
            "_thesis_quote": profile_data.get("thesis_quote"),
        }

    # ── Profile fetch (Phase 2) ────────────────────────────────────────────────

    def get_investor_profile(self, profile_id: str) -> dict:
        """
        Fetch an investor profile by PitchBook ID.

        Constructs the profile URL from the ID and scrapes via Playwright.
        Returns a dict compatible with pitchbook_api.get_investor_profile where
        possible; fields unavailable in HTML default to '' or None.
        """
        if self._sess is None:
            raise PitchBookBrowserError("Must be used as a context manager")

        # PitchBook internal URL format: /profile/{id}/investor/profile
        profile_url = f"https://my.pitchbook.com/profile/{profile_id}/investor/profile"

        try:
            profile_data = self._sess.scrape_person_profile(profile_url)
        except Exception as exc:
            logger.warning(
                "PitchBookBrowser.get_investor_profile: scrape failed for %s: %s",
                profile_id, exc,
            )
            return {"id": profile_id, "_error": str(exc)}

        bio = profile_data.get("bio", "")
        current_role = profile_data.get("current_role", "")
        email = _extract_email_from_profile(profile_data)
        linkedin = _extract_linkedin_from_profile(profile_data)

        deals = profile_data.get("recent_deals", [])

        return {
            "id": profile_id,
            "bio": bio,
            "primaryType": "",
            "investorStatus": "",
            "totalInvestmentsCount": len(deals) or None,
            "totalExitsCount": None,
            "activeInvestedCompaniesCount": None,
            "investmentsLast12Months": None,
            "medianCheckSize": None,
            "medianValuation": None,
            "employeeCount": None,
            "primaryPosition": current_role,
            "gender": "",
            "education": [],
            "firstName": "",
            "lastName": "",
            "email": email,
            "phone": "",
            "linkedIn": linkedin,
            "officeCity": "",
            "officeAddress": [],
            "investments": [
                {
                    "company": d.get("company", "") if isinstance(d, dict) else str(d),
                    "companyId": "",
                    "dealDate": d.get("date", "") if isinstance(d, dict) else "",
                    "dealType": d.get("round", "") if isinstance(d, dict) else "",
                    "amount": None,
                    "currency": None,
                    "industry": None,
                    "formerInvestment": False,
                }
                for d in deals
            ],
            "totalInvestmentsFetched": len(deals),
            "_profile_url": profile_url,
            "_board_seats": profile_data.get("board_seats", []),
            "_sectors": profile_data.get("sectors", []),
            "_thesis_quote": profile_data.get("thesis_quote"),
        }


# ── Helper extractors ──────────────────────────────────────────────────────────

def _extract_email_from_profile(profile_data: dict) -> str:
    """
    Try to find an email in the profile data.
    PitchBook rarely renders emails in HTML  -  this is best-effort.
    """
    # Look in raw_html_excerpt for email pattern
    raw_html = profile_data.get("raw_html_excerpt", "")
    if raw_html:
        m = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', raw_html)
        if m:
            email = m.group(0)
            # Skip pitchbook's own asset/support emails
            if "pitchbook" not in email.lower() and "sentry" not in email.lower():
                return email
    return ""


def _extract_linkedin_from_profile(profile_data: dict) -> str:
    """Try to find a LinkedIn URL in the profile HTML."""
    raw_html = profile_data.get("raw_html_excerpt", "")
    if raw_html:
        m = re.search(r'https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+/?', raw_html)
        if m:
            return m.group(0)
    return ""


def _parse_firm_from_role(current_role: str) -> str:
    """Workspace-backed outreach integration."""
    if not current_role:
        return ""
    # LinkedIn / PitchBook style: "Title · Firm"
    if "·" in current_role:
        parts = [p.strip() for p in current_role.split("·")]
        return parts[-1] if len(parts) >= 2 else current_role
    # User-provided workspace configuration.
    if "," in current_role:
        parts = [p.strip() for p in current_role.split(",")]
        return parts[-1] if len(parts) >= 2 else current_role
    return current_role


# ── Module-level convenience functions ─────────────────────────────────────────

def search_investor_by_name(name: str) -> Optional[dict]:
    """
    Stateless convenience wrapper: open browser, search, close.

    Opens a fresh Playwright browser session for each call.
    For batch use, prefer the PitchBookBrowser context manager directly
    to avoid per-call browser launch overhead (~3-5s).

    Returns None if not found or session not authenticated.
    """
    profile_dir = _resolve_profile_dir()

    # Check profile exists before launching browser
    profile_path = Path(profile_dir)
    cookies_db = profile_path / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        raise PitchBookBrowserAuthRequired(
            f"PitchBook browser profile not seeded at {profile_dir}. "
            "Run: python run.py pitchbook-browser-auth"
        )

    with PitchBookBrowser(profile_dir=profile_dir) as pb:
        if not pb.is_authenticated():
            raise PitchBookBrowserAuthRequired(
                "PitchBook session expired or not logged in. "
                "Run: python run.py pitchbook-browser-auth"
            )
        return pb.search_investor_by_name(name)


def get_investor_profile(profile_id: str) -> dict:
    """
    Stateless convenience wrapper: open browser, fetch profile by ID, close.

    For batch use, prefer the PitchBookBrowser context manager.
    """
    profile_dir = _resolve_profile_dir()

    profile_path = Path(profile_dir)
    cookies_db = profile_path / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        raise PitchBookBrowserAuthRequired(
            f"PitchBook browser profile not seeded at {profile_dir}. "
            "Run: python run.py pitchbook-browser-auth"
        )

    with PitchBookBrowser(profile_dir=profile_dir) as pb:
        if not pb.is_authenticated():
            raise PitchBookBrowserAuthRequired(
                "PitchBook session expired or not logged in. "
                "Run: python run.py pitchbook-browser-auth"
            )
        return pb.get_investor_profile(profile_id)
