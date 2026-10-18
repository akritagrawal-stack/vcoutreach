"""Local workspace configuration for the Publikreach operator console."""
from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse


from vcoutreach.web.config import WorkspacePath, get_workspace
SETTINGS_PATH = WorkspacePath('data', 'workspace-settings.json')

DISCOVERY_SOURCES = {
    "pitchbook": "PitchBook plus official sources",
    "manual_csv": "Manual CSV or spreadsheet",
    "web_research": "Official web research",
    "apollo": "Apollo named-contact workflow",
}

AUDIENCE_TYPES = {
    "investors": "Investors",
    "customers": "Customers",
    "partners": "Partners",
    "recruiting": "Candidates",
}

SAMPLE_BLURB = 'Describe your product using approved sender facts.'

DEFAULT_SETTINGS = {'workspace_name': 'Publikreach', 'workspace_short_name': 'REACH', 'company_name': '', 'product_name': '', 'operator_name': '', 'operator_role': '', 'contact_label_singular': 'contact', 'contact_label_plural': 'contacts', 'campaign_goal': 'Build a grounded outreach pipeline', 'hero_primary': 'Your outreach workspace for', 'hero_emphasis': 'thoughtful conversations.', 'hero_support': 'Research, review, and send from one local workspace.', 'meeting_label': 'meetings', 'meeting_source': 'manual snapshot', 'email_subject_rule': 'A quick introduction', 'personalization_rule': 'Use one verified fact and explain its relevance.', 'one_liner': '', 'company_background': '', 'problem_statement': '', 'solution_statement': '', 'target_customer': '', 'business_model': '', 'competitive_advantage': '', 'founder_background': '', 'company_thesis': '', 'founder_line': '', 'traction_line': '', 'ask_line': '', 'call_to_action': 'Would you be open to a short conversation?', 'pitch_deck_path': '', 'pitch_deck_label': 'Pitch deck', 'outreach_blurb_template': '{First name},\n\n{One verified sentence}\n\n{Company description}\n\n{Ask}\n\n{Sender name}', 'proof_library': '', 'claim_guardrails': '', 'attachment_notes': '', 'schema_version': 2, 'initialized': False, 'website': '', 'audience_type': 'investors', 'discovery_source': 'manual_csv', 'meeting_count': 0, 'pitch_deck_url': ''}

_FIELD_LIMITS = {
    "workspace_name": 60,
    "workspace_short_name": 16,
    "company_name": 60,
    "product_name": 60,
    "operator_name": 60,
    "operator_role": 80,
    "contact_label_singular": 24,
    "contact_label_plural": 32,
    "campaign_goal": 120,
    "hero_primary": 80,
    "hero_emphasis": 80,
    "hero_support": 240,
    "meeting_label": 60,
    "meeting_source": 80,
    "email_subject_rule": 100,
    "personalization_rule": 180,
    "one_liner": 500,
    "company_background": 1600,
    "problem_statement": 1200,
    "solution_statement": 1600,
    "target_customer": 1400,
    "business_model": 1400,
    "competitive_advantage": 1400,
    "founder_background": 1600,
    "company_thesis": 900,
    "founder_line": 600,
    "traction_line": 800,
    "ask_line": 400,
    "call_to_action": 240,
    "pitch_deck_path": 500,
    "pitch_deck_label": 160,
    "outreach_blurb_template": 3000,
    "proof_library": 2200,
    "claim_guardrails": 2200,
    "attachment_notes": 1200,
}

_SPACE_PATTERN = re.compile(r"[ \t]+")
_MULTILINE_FIELDS = {
    "company_background",
    "problem_statement",
    "solution_statement",
    "target_customer",
    "business_model",
    "competitive_advantage",
    "founder_background",
    "outreach_blurb_template",
    "proof_library",
    "claim_guardrails",
    "attachment_notes",
}


def _clean_text(value: object, *, maximum: int, fallback: str) -> str:
    cleaned = _SPACE_PATTERN.sub(" ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()
    return (cleaned or fallback)[:maximum]


def _clean_multiline(value: object, *, maximum: int, fallback: str) -> str:
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACE_PATTERN.sub(" ", line).strip() for line in raw.split("\n")]
    cleaned = "\n".join(lines).strip()
    return (cleaned or fallback)[:maximum]


def _clean_url(value: object, fallback: str) -> str:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate[:240]
    return fallback


def normalize_settings(payload: dict | None) -> dict:
    source = payload if isinstance(payload, dict) else {}
    normalized = deepcopy(DEFAULT_SETTINGS)
    for field, maximum in _FIELD_LIMITS.items():
        cleaner = _clean_multiline if field in _MULTILINE_FIELDS else _clean_text
        normalized[field] = cleaner(
            source.get(field),
            maximum=maximum,
            fallback=str(DEFAULT_SETTINGS[field]),
        )
    normalized["website"] = _clean_url(source.get("website"), DEFAULT_SETTINGS["website"])
    pitch_deck_url = str(source.get("pitch_deck_url") or "").strip()
    normalized["pitch_deck_url"] = (
        _clean_url(pitch_deck_url, DEFAULT_SETTINGS["pitch_deck_url"])
        if pitch_deck_url
        else ""
    )
    audience = str(source.get("audience_type", "")).strip()
    normalized["audience_type"] = audience if audience in AUDIENCE_TYPES else DEFAULT_SETTINGS["audience_type"]
    discovery = str(source.get("discovery_source", "")).strip()
    normalized["discovery_source"] = discovery if discovery in DISCOVERY_SOURCES else DEFAULT_SETTINGS["discovery_source"]
    try:
        normalized["meeting_count"] = max(0, min(50, int(source.get("meeting_count", DEFAULT_SETTINGS["meeting_count"]))))
    except (TypeError, ValueError):
        normalized["meeting_count"] = DEFAULT_SETTINGS["meeting_count"]
    if normalized.get('pitch_deck_path'):
        normalized['pitch_deck_path'] = str(get_workspace().path(normalized['pitch_deck_path']))
    worker_url = str(source.get('tracking_worker_url', '')).strip()
    if worker_url:
        from vcoutreach.web.keys import validate_worker_url
        worker_url = validate_worker_url(worker_url)
    normalized['tracking_worker_url'] = worker_url
    normalized["initialized"] = bool(source.get("initialized", False))
    normalized["schema_version"] = 2
    return normalized


def load_settings() -> dict:
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return deepcopy(DEFAULT_SETTINGS)
    return normalize_settings(payload)


def save_settings(payload: dict) -> dict:
    normalized = normalize_settings({**payload, "initialized": True})
    return _persist_settings(normalized)


def _persist_settings(normalized: dict) -> dict:
    SETTINGS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    SETTINGS_PATH.parent.chmod(0o700)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(SETTINGS_PATH)
    SETTINGS_PATH.chmod(0o600)
    return normalized


def reset_settings() -> dict:
    return _persist_settings(deepcopy(DEFAULT_SETTINGS))


def setup_instructions(settings: dict, *, pitchbook_ready: bool, calendar_live: bool) -> list[dict]:
    discovery = settings["discovery_source"]
    source_instructions = {
        "pitchbook": (
            "Open the visible run from Overview, sign in inside the dedicated Chrome window, "
            "then wait for SESSION READY. Use official firm pages to verify every hook."
        ),
        "manual_csv": (
            "Open Overview and import a CSV with one named person per organization. Keep name, "
            "organization, role, domain, and source URL. Verify the mailbox before drafting."
        ),
        "web_research": (
            "Start in Research. Capture a current official thesis, the accountable person, and "
            "one dated investment or authored insight before a contact can be drafted."
        ),
        "apollo": (
            "Build a named-contact list first, then use Apollo only to resolve those people. "
            "Bring the results back through Overview and run mailbox verification."
        ),
    }
    return [
        {
            "index": "01",
            "title": "Define the workspace",
            "state": "complete" if settings["initialized"] else "required",
            "body": "Set the audience, goal, company story, and proof points below. These values drive the Overview and sample email.",
        },
        {
            "index": "02",
            "title": f"Connect {DISCOVERY_SOURCES[discovery]}",
            "state": "complete" if discovery == "pitchbook" and pitchbook_ready else "next",
            "body": source_instructions[discovery],
        },
        {
            "index": "03",
            "title": "Keep delivery in test mode",
            "state": "guarded",
            "body": "Authenticate Gmail only through the installed-app OAuth flow. Never paste a token or secret into this page. Test sends remain routed to the configured test mailbox.",
        },
        {
            "index": "04",
            "title": "Connect the calendar or keep a snapshot",
            "state": "complete" if calendar_live else "optional",
            "body": "Without a Calendly token, the dashboard shows the manual meeting count below and labels it as a snapshot. A live connection replaces that count automatically.",
        },
        {
            "index": "05",
            "title": "Run one review-only record",
            "state": "next",
            "body": "Confirm the source, fit, named mailbox, personalization, and sample copy. Only after review should a separately approved delivery command be used.",
        },
    ]
