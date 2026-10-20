"""Validate outreach text without a fixed sender persona."""
from __future__ import annotations

import re
from vcoutreach.contacts import sanitize_text, is_http_url
from vcoutreach.lint import canned_well_wishes, unfilled_placeholders, FILLER_PHRASES
from vcoutreach.web.vendor import load_sender_context

# ---------------------------------------------------------------------------
# Banned filler phrases - hollow AI-speak that signals no real source was read
# Each entry is (substring_to_match, human_readable_rejection_reason)
# Case-insensitive substring match on hook.lower()
# ---------------------------------------------------------------------------

_BANNED_FILLER_PHRASES: list[tuple[str, str]] = [
    ("captures our philosophy",    "banned filler phrase: 'captures our philosophy' - hollow paraphrase"),
    ("captures what we believe",   "banned filler phrase: 'captures what we believe' - hollow paraphrase"),
    ("aligns with our",            "banned filler phrase: 'aligns with our' - hollow paraphrase"),
    ("perfectly resonates",        "banned filler phrase: 'perfectly resonates' - hollow filler"),
    ("deeply resonates",           "banned filler phrase: 'deeply resonates' - hollow filler"),
    ("speaks to us",               "banned filler phrase: 'speaks to us' - hollow filler"),
    ("passion drives engagement",  "banned filler phrase: 'passion drives engagement' - generic AI filler"),
    ("drives engagement",          "banned filler phrase: 'drives engagement' - generic AI filler"),
    ("fostering connections",      "banned filler phrase: 'fostering connections' - generic AI filler"),
    ("we believe in",              "banned filler phrase: 'we believe in' - hollow filler"),
    ("we're inspired by",          "banned filler phrase: 'we're inspired by' - hollow filler"),
    ("we are inspired by",         "banned filler phrase: 'we are inspired by' - hollow filler"),
    ("building genuine connections", "banned filler phrase: 'building genuine connections' - hollow filler"),
    ("authentic relationships",    "banned filler phrase: 'authentic relationships' - hollow filler"),
    ("resonates with",             "banned filler phrase: 'resonates with' - hollow paraphrase"),
]

# ---------------------------------------------------------------------------
# Placeholder / bracket detection
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(
    r'\bPLACEHOLDER\b'
    r'|\bINSERT\s+\w+'
    r'|(?-i:\bFIRM\b)',       # Case-sensitive to allow ordinary prose about a firm.
    re.IGNORECASE,
)

# Meta-preamble patterns the LLM sometimes emits before the actual hook text
_META_PREAMBLE_RE = re.compile(
    r'^(?:here\s+is\s+(?:the\s+)?(?:hook|subject|output|json)[:\s]*'
    r'|hook[:\s]+'
    r'|subject[:\s]+'
    r'|output[:\s]+)',
    re.IGNORECASE,
)

# Em-dash and en-dash unicode chars
_EM_DASH_CHARS = ('\u2014', '\u2013')


# ---------------------------------------------------------------------------
# postprocess
# ---------------------------------------------------------------------------

def postprocess(text: str) -> str:
    """Validate outreach text without a fixed sender persona."""
    if not text:
        return text
    # Replace em/en dash with a comma + space (to preserve sentence flow)
    for ch in _EM_DASH_CHARS:
        text = text.replace(ch, ', ')
    # Strip meta-preamble ("Here is the hook: ...")
    text = _META_PREAMBLE_RE.sub('', text).strip()
    # Collapse multiple spaces
    text = re.sub(r'  +', ' ', text)
    # Strip leading/trailing whitespace
    return text.strip()


# ---------------------------------------------------------------------------
# validate_hook
# ---------------------------------------------------------------------------

def validate_hook(hook: str, *, sender_profile: dict | None = None, facts: dict | None = None) -> tuple[bool, str | None]:
    """Validate outreach text without a fixed sender persona."""
    if not hook or not hook.strip():
        return False, 'hook is empty'

    # Em/en dash check
    for ch in _EM_DASH_CHARS:
        if ch in hook:
            return False, f'hook contains em/en-dash character (\\u{ord(ch):04x})'

    # Word count (25-50 per HARD RULE 5 in prompts.py)
    words = hook.split()
    wc = len(words)
    if wc < 25:
        return False, f'hook too short: {wc} words (minimum 25)'
    if wc > 50:
        return False, f'hook too long: {wc} words (maximum 50)'

    if '\n' in hook or len(re.split(r'(?<=[.!?])\s+(?=[A-Z])', hook)) > 2:
        return False, 'hook must be an opener of 1 to 2 sentences, without a greeting, body or signature'

    # Banned filler phrase check
    well_wishes = canned_well_wishes(hook)
    if well_wishes:
        return False, 'filler phrase: ' + well_wishes[0]
    hook_lower = hook.lower()
    for phrase, reason in _BANNED_FILLER_PHRASES:
        if phrase in hook_lower:
            return False, reason
    for phrase in FILLER_PHRASES:
        if phrase in hook_lower.replace('\u2019', "'"):
            return False, 'filler phrase: ' + phrase

    # Placeholder detection
    ph = unfilled_placeholders(hook)
    if ph:
        return False, f'hook contains placeholder text: {ph[0]!r}'
    ph_token = _PLACEHOLDER_RE.search(hook)
    if ph_token:
        return False, f'hook contains placeholder text: {ph_token.group(0)!r}'

    # Consecutive question mark check (two '?' back-to-back or ??)
    if '??' in hook:
        return False, 'hook ends with consecutive question marks'

    sender, context = load_sender_context(sender_profile, facts)
    for phrase in list(sender.get("forbidden_phrases", [])) + list(context.get("forbidden_phrases", [])):
        if isinstance(phrase, str) and phrase and phrase.casefold() in hook.casefold():
            return False, "text contains a forbidden sender phrase"
    return True, None


# ---------------------------------------------------------------------------
# validate_subject
# ---------------------------------------------------------------------------

def validate_subject(subject: str, *, sender_profile: dict | None = None, facts: dict | None = None) -> tuple[bool, str | None]:
    """Validate outreach text without a fixed sender persona."""
    if not subject or not subject.strip():
        return False, 'subject is empty'
    ph = unfilled_placeholders(subject)
    if ph:
        return False, f'subject contains placeholder text: {ph[0]!r}'

    # Em/en dash check
    for ch in _EM_DASH_CHARS:
        if ch in subject:
            return False, f'subject contains em/en-dash character (\\u{ord(ch):04x})'

    # Word count: tightened from 3-12 to 4-8 to match subject_generator.py
    words = subject.split()
    wc = len(words)
    if wc < 4:
        return False, f'subject too short: {wc} words (minimum 4)'
    if wc > 8:
        return False, f'subject too long: {wc} words (maximum 8)'

    # Character count
    if len(subject) < 25:
        return False, f'subject too short: {len(subject)} chars (minimum 25)'
    if len(subject) > 50:
        return False, f'subject too long: {len(subject)} chars (maximum 50)'

    # Banned prefixes
    low = subject.lower().strip()
    banned_starts = ('re:', 'fwd:', 'fw:', 'urgent', 'action required')
    for prefix in banned_starts:
        if low.startswith(prefix):
            return False, f'subject starts with banned prefix: {prefix!r}'

    # Banned phrases - dead patterns that signal generic/drifted output
    bad_phrases = [
        'saw your',
        'raising',
        'funding',
    ]
    for phrase in bad_phrases:
        if phrase in low:
            return False, f'subject contains banned phrase: {phrase!r}'

    # Reject bare $ signs
    if '$' in subject:
        return False, 'subject contains banned character: $'

    sender, context = load_sender_context(sender_profile, facts)
    for phrase in list(sender.get("forbidden_phrases", [])) + list(context.get("forbidden_phrases", [])):
        if isinstance(phrase, str) and phrase and phrase.casefold() in subject.casefold():
            return False, "text contains a forbidden sender phrase"
    return True, None
