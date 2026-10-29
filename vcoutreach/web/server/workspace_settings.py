"""Local workspace configuration for the Publikreach operator console."""
from __future__ import annotations

import json
import os
import re
import shutil
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse


from vcoutreach.web.config import WorkspacePath, get_workspace
SETTINGS_PATH = WorkspacePath('data', 'workspace-settings.json')

from vcoutreach.web.keys import KEY_NAMES
CONNECTION_SETTING_NAMES = KEY_NAMES | {'SEARXNG_URL'}

def validate_http_url(value: str) -> str:
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise ValueError('URL must use http or https and include a valid host.') from None
    if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError('URL must use http or https and include a valid host.')
    return value.rstrip('/')

def save_connection_settings(values):
    from vcoutreach.web.keys import write_env
    from vcoutreach.web.server.runtime_secrets import store_searxng_url, validate_secret
    if set(values) - CONNECTION_SETTING_NAMES:
        raise ValueError('Unsupported setting name.')
    if values.get('SEARXNG_URL'):
        validate_http_url(values['SEARXNG_URL'])
        validate_secret(values['SEARXNG_URL'])
    write_env({key:value for key,value in values.items() if key != 'SEARXNG_URL'})
    if 'SEARXNG_URL' in values:
        store_searxng_url(values['SEARXNG_URL'])

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

from vcoutreach.web.server import example_pitch as _example

# The default configuration is a complete worked example (a redacted real Publik email), so a new
# user edits a believable pitch instead of filling blank boxes. See example_pitch.py.
DEFAULT_OUTREACH_BLURB = _example.EXAMPLE_BLURB_TEMPLATE

DEFAULT_SETTINGS = {'workspace_name': 'Publikreach', 'workspace_short_name': 'REACH', 'company_name': '', 'product_name': '', 'operator_name': '', 'operator_role': '', 'contact_label_singular': 'contact', 'contact_label_plural': 'contacts', 'campaign_goal': 'Build a grounded outreach pipeline', 'hero_primary': 'Your outreach workspace for', 'hero_emphasis': 'thoughtful conversations.', 'hero_support': 'Research, review, and send from one local workspace.', 'meeting_label': 'meetings', 'meeting_source': 'manual snapshot', 'email_subject_rule': _example.EXAMPLE_SUBJECT, 'personalization_rule': 'Use one verified fact and explain its relevance.', 'one_liner': _example.EXAMPLE_ONE_LINER, 'company_background': '', 'problem_statement': _example.EXAMPLE_PROBLEM, 'solution_statement': _example.EXAMPLE_SOLUTION, 'target_customer': '', 'business_model': '', 'competitive_advantage': '', 'founder_background': '', 'company_thesis': '', 'founder_line': _example.EXAMPLE_FOUNDER_LINE, 'traction_line': _example.EXAMPLE_TRACTION, 'ask_line': _example.EXAMPLE_ASK, 'call_to_action': 'Would you be open to a short conversation?', 'pitch_deck_path': '', 'pitch_deck_label': _example.EXAMPLE_DECK_LABEL, 'outreach_blurb_template': DEFAULT_OUTREACH_BLURB, 'proof_library': '', 'claim_guardrails': '', 'attachment_notes': '', 'schema_version': 2, 'initialized': False, 'website': '', 'audience_type': 'investors', 'discovery_source': 'manual_csv', 'meeting_count': 0, 'pitch_deck_url': '', 'publik_recommendation': 'pending', 'provider_preference': 'auto', 'mcp_last_self_test_ok': '', 'campaign_kind': ''}

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

# Example story fields may be cleared by the user. Blank must stay blank, not snap back to the example.
_BLANKABLE = {"one_liner", "problem_statement", "solution_statement", "founder_line", "traction_line", "ask_line"}

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


# Campaign kind: who the owner is writing to and what they attach. '' means not chosen yet (Campaign
# then offers the two choices). Copy per kind is plain text; the credibility line is app-panel copy
# only and must never be put into text that can be pasted into an email or returned by MCP.
CAMPAIGN_KIND_VALUES = ("fundraising", "job_search")
DEFAULT_CAMPAIGN_KIND = "fundraising"
CAMPAIGN_KINDS = {
    "fundraising": {
        "label": "Fundraising",
        "choice_label": "I am raising money",
        "hero_line": "Set up your fundraising campaign.",
        "hero_support": ("Add investors, check your pitch, write the drafts, then review one and send "
                         "yourself a test."),
        "step2_heading": "Your pitch",
        "step2_hint": ("Your pitch is the email template plus the pitch deck attached to it. Read the "
                       "example, then make it yours in Settings."),
        "attachment_label": "Pitch deck",
        "audience_word": "investor",
        "audience_word_plural": "investors",
        "panel_credibility": ("This is our email template and pitch deck that has gotten 50+ meetings "
                              "and $600K raised in funding."),
    },
    "job_search": {
        "label": "Job search",
        "choice_label": "I am looking for a role",
        "hero_line": "Set up your job search campaign.",
        "hero_support": ("Add hiring managers, check your note, write the drafts, then review one and "
                         "send yourself a test."),
        "step2_heading": "Your note and resume",
        "step2_hint": ("Your note is the email template plus the resume attached to it. Read the "
                       "example, then make it yours in Settings."),
        "attachment_label": "Resume",
        "audience_word": "hiring manager",
        "audience_word_plural": "hiring managers",
        "panel_credibility": "",
    },
}


def normalize_campaign_kind(value: object) -> str:
    """'' (not chosen) or one of CAMPAIGN_KIND_VALUES; anything else becomes ''."""
    text = str(value or "").strip()
    return text if text in CAMPAIGN_KIND_VALUES else ""


def effective_campaign_kind(settings: dict | None = None) -> str:
    """The kind to render with: the stored choice, else 'fundraising' (the default example)."""
    stored = normalize_campaign_kind((settings if settings is not None else load_settings()).get("campaign_kind"))
    return stored or DEFAULT_CAMPAIGN_KIND


def campaign_kind_copy(kind: object = None) -> dict:
    """Copy for one kind. ``None`` uses the workspace's effective kind; unknown values use fundraising."""
    key = effective_campaign_kind() if kind is None else (normalize_campaign_kind(kind) or DEFAULT_CAMPAIGN_KIND)
    return dict(CAMPAIGN_KINDS[key], kind=key)


RECOMMENDATION_VALUES = ("pending", "skipped")
PROVIDER_PREFERENCES = ("auto", "template", "publik", "openai", "anthropic")
_PERSISTED_KEYS = ("publik_recommendation", "provider_preference", "mcp_last_self_test_ok", "campaign_kind")


def normalize_settings(payload: dict | None) -> dict:
    source = payload if isinstance(payload, dict) else {}
    normalized = deepcopy(DEFAULT_SETTINGS)
    for field, maximum in _FIELD_LIMITS.items():
        cleaner = _clean_multiline if field in _MULTILINE_FIELDS else _clean_text
        fallback = "" if field in _BLANKABLE and field in source else str(DEFAULT_SETTINGS[field])
        normalized[field] = cleaner(source.get(field), maximum=maximum, fallback=fallback)
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
    recommendation = str(source.get("publik_recommendation", "")).strip()
    normalized["publik_recommendation"] = recommendation if recommendation in RECOMMENDATION_VALUES else DEFAULT_SETTINGS["publik_recommendation"]
    preference = str(source.get("provider_preference", "")).strip()
    normalized["provider_preference"] = preference if preference in PROVIDER_PREFERENCES else DEFAULT_SETTINGS["provider_preference"]
    normalized["mcp_last_self_test_ok"] = _clean_text(
        source.get("mcp_last_self_test_ok"), maximum=40, fallback=DEFAULT_SETTINGS["mcp_last_self_test_ok"])
    normalized["campaign_kind"] = normalize_campaign_kind(source.get("campaign_kind"))
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
    if payload.get('campaign_kind') and normalize_campaign_kind(payload['campaign_kind']) == '':
        raise ValueError('Campaign kind must be fundraising or job_search.')
    for name in ('website','pitch_deck_url'):
        if payload.get(name):
            validate_http_url(payload[name])
    # Keys the settings form does not post (recommendation skip, provider preference,
    # MCP self-test stamp) keep their stored value instead of resetting to defaults.
    stored = load_settings()
    kept = {name: stored[name] for name in _PERSISTED_KEYS if name not in payload}
    normalized = normalize_settings({**payload, **kept, "initialized": True})
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


EXAMPLE_DECK_NAME = 'Example_Pitch_Deck.pdf'
_EXAMPLE_DECK_MARKER = ('data', '.example_deck_seeded')


def bundled_example_deck() -> Path:
    from importlib.resources import files
    return Path(str(files('vcoutreach').joinpath('data', 'examples', 'Publik_Pitch_Deck_Example.pdf')))


#: md5 of every previously bundled example deck (kept for tests and audits). The file name
#: Example_Pitch_Deck.pdf is owned by the app: uploads that collide with it get a suffixed name, so any
#: copy under that exact name whose bytes differ from the bundled deck is a stale seed and is refreshed.
KNOWN_OLD_EXAMPLE_DECK_MD5 = (
    '558bb3abc414862536936ef6329d1aad',
    '33fabacc25dff7baa41fc967f27cc53a',  # pre-release 8 page build that still showed real TAM and SAM
    '30589ce65165e964b296cbc057e5ffab',  # 9 page build without the redacted founder profile slide
)


def _file_md5(path) -> str:
    import hashlib
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _refresh_old_example_deck(workspace, source: Path) -> bool:
    """Replace the workspace Example_Pitch_Deck.pdf with the bundled one whenever the bytes differ."""
    target = workspace.path('attachments', EXAMPLE_DECK_NAME)
    try:
        if not target.is_file() or not source.is_file():
            return False
        if _file_md5(source) in KNOWN_OLD_EXAMPLE_DECK_MD5:
            return False
        if _file_md5(target) == _file_md5(source):
            return False
        temporary = target.with_name(target.name + '.refresh.tmp')
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def ensure_example_deck() -> bool:
    """Seed the bundled example deck once per workspace. Never overwrites a user path.

    Returns True only when the deck was copied and saved as pitch_deck_path.
    A marker file stops re-seeding after the user clears or replaces the path.
    A stale Example_Pitch_Deck.pdf (any bytes other than the bundled deck) is refreshed on every call, so an
    older, less redacted seed can never linger; the user's own decks keep their own names and are never touched.
    """
    workspace = get_workspace()
    marker = workspace.path(*_EXAMPLE_DECK_MARKER)
    source = bundled_example_deck()
    _refresh_old_example_deck(workspace, source)
    if marker.exists():
        return False
    if not source.is_file():
        return False
    settings = load_settings()
    if settings.get('pitch_deck_path'):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('user deck present\n', encoding='utf-8')
        return False
    target = workspace.path('attachments', EXAMPLE_DECK_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    settings['pitch_deck_path'] = str(target)
    _persist_settings(normalize_settings(settings))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('example deck seeded\n', encoding='utf-8')
    return True


def is_bundled_example_deck(path) -> bool:
    """True when ``path`` is the seeded copy of the bundled Publik investor example deck (same name and bytes)."""
    import filecmp
    try:
        candidate = Path(str(path))
        if candidate.name != EXAMPLE_DECK_NAME or not candidate.is_file():
            return False
        source = bundled_example_deck()
        return filecmp.cmp(str(candidate), str(source), shallow=False) if source.is_file() else True
    except (OSError, ValueError):
        return False


def blurb_for_kind(blurb: object, kind: object = None) -> str:
    """The email note to show. Job search with the untouched investor default gets the job search default."""
    text = str(blurb or "").strip()
    key = effective_campaign_kind() if kind is None else (normalize_campaign_kind(kind) or DEFAULT_CAMPAIGN_KIND)
    if key == "job_search" and text in ("", DEFAULT_OUTREACH_BLURB.strip()):
        return _example.JOB_BLURB_TEMPLATE
    return text


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
