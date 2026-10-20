"""Draft lint rules."""
from __future__ import annotations

import re
from typing import List, Optional, Set, Tuple

from .types import Contact, LintIssue, SenderProfile

EM_DASH = "\u2014"
EN_DASH = "\u2013"

MIN_WORDS = 50
MAX_WORDS = 130

FILLER_PHRASES: Tuple[str, ...] = (
    "just circling back",
    "bumping this",
    "excited to share",
    "quick question",
    "touch base",
    "leverage",
    "delve",
)

_CANNED_WELL_WISH = re.compile(
    r"\b(?:i\s+)?hope\s+(?:"
    r"this\s+(?:(?:message|note|email)\s+)?finds\s+you\s+(?:well|great|healthy)"
    r"|you(?:\s+are|'re)\s+(?:doing\s+well|well|having\s+a\s+great\s+week)"
    r"|all\s+is\s+well)(?![\w-])",
    re.IGNORECASE,
)


def canned_well_wishes(text: str) -> List[str]:
    """Match complete stock well-wishes, allowing case and punctuation variants."""
    return [match.group(0) for match in _CANNED_WELL_WISH.finditer(_flat(text))]

_FALSE_PARALLEL_PATTERNS: Tuple["re.Pattern[str]", ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:is|are) (?:exactly|precisely|close to|just like) what we\b",
        r"\bsame as what we\b",
        r"\bexactly what we(?:'re| are) (?:doing|building)\b",
        r"\bmaps directly to (?:what|how) we\b",
    )
)

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_NUMBER_TOKEN = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
_DIGIT_RUN = re.compile(r"\d+")
_GROUPED_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+")
_PLACEHOLDER = re.compile(
    r"\{\{\s*[A-Za-z0-9_][A-Za-z0-9_ ]*\s*\}\}"
    r"|(?<!\{)\{[A-Za-z0-9_][A-Za-z0-9_ ]*\}(?!\})"
    r"|\[\s*(?:company|name|first[ _]name|last[ _]name|tbd|placeholder|insert(?:\s+[^\]\n]*)?)\s*\]"
    r"|<(?:name|first[ _]name|last[ _]name|company|tbd|insert(?:\s+[^>\n]*)?)>"
    r"|%[A-Za-z_][A-Za-z0-9_ ]*%|\bTODO\b",
    re.IGNORECASE,
)


def unfilled_placeholders(text: str) -> List[str]:
    """Find template tokens outside inline or fenced backtick code."""
    prose = re.sub(r"(`+)[\s\S]*?\1", "", text or "")
    return list(dict.fromkeys(match.group(0) for match in _PLACEHOLDER.finditer(prose)))


def _unix(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _flat(text: str) -> str:
    """Lower-case, straight apostrophes, single spaces: for phrase matching only."""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(text.lower().split())


def _split_paragraphs(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []
    return [p for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]


_SIGNATURE_MAX_LINES = 4
_SIGNATURE_MAX_LINE_LENGTH = 60

_NON_NAMES = frozenset(
    (
        "the", "team", "staff", "info", "sales", "hello", "hi", "dear", "sir", "madam",
        "phd", "md", "mba", "jr", "sr", "ii", "iii", "iv", "cpa", "cfa", "esq",
    )
)


def _looks_like_signature(paragraph: str) -> bool:
    """A short block of short lines, none of which ends like a sentence."""
    lines = [line.strip() for line in _unix(paragraph).strip().split("\n")]
    if not lines or len(lines) > _SIGNATURE_MAX_LINES:
        return False
    for line in lines:
        if len(line) > _SIGNATURE_MAX_LINE_LENGTH or line.endswith((".", "?", "!")):
            return False
    return True


def _signature_split(body: str, sender: SenderProfile) -> Tuple[List[str], Optional[str]]:
    """Split a body into (paragraphs before the signature, final paragraph if it is one).

    The sender's signature text counts when the body ends with it on its own paragraph.
    A body that is a single paragraph has no separate signature.
    """
    text = _unix(body).rstrip()
    signature = _unix(sender.signature).strip()
    if signature and text.endswith(signature):
        head = text[: len(text) - len(signature)]
        if not head.strip() or re.search(r"\n[ \t]*\n\s*$", head):
            return _split_paragraphs(head), signature
    paragraphs = _split_paragraphs(text)
    if len(paragraphs) > 1:
        return paragraphs[:-1], paragraphs[-1]
    return paragraphs, None


def _before_signature(body: str, sender: SenderProfile) -> List[str]:
    """Paragraphs counted as the body (word_count): the final paragraph is always the signature."""
    return _signature_split(body, sender)[0]


def _wrap_checked(body: str, sender: SenderProfile) -> List[str]:
    """Paragraphs checked for hard_wrap.

    The final paragraph is exempt as a signature only if it matches the sender's signature
    or looks like one (at most 4 lines, each at most 60 characters, none ending with a
    period, question mark or exclamation mark). Any other final paragraph is checked like
    the rest.
    """
    text = _unix(body).rstrip()
    signature = _unix(sender.signature).strip()
    head, last = _signature_split(body, sender)
    if last is None:
        return head
    if signature and text.endswith(signature) and last == signature:
        return head
    if _looks_like_signature(last):
        return head
    return head + [last]


def _number_forms(text: str) -> Set[str]:
    forms = set(_DIGIT_RUN.findall(text))
    for grouped in _GROUPED_NUMBER.findall(text):
        forms.add(grouped.replace(",", ""))
    return forms


def lint_draft(
    subject: str,
    body: str,
    sender: SenderProfile,
    contact: Optional[Contact],
    hook_text: str = "",
) -> List[LintIssue]:
    """Return lint issues for a draft. Never mutates anything.

    With contact None or hook_text "" (a send time re-lint) the hook only checks
    (false_parallel, unapproved_number, empty_hook) are skipped.
    """
    issues: List[LintIssue] = []
    subject = subject or ""
    body = body or ""

    for place, text in (("subject", subject), ("body", body)):
        for placeholder in unfilled_placeholders(text):
            issues.append(LintIssue("placeholder", 'unfilled placeholder in %s: %s' % (place, placeholder)))

    # em_dash
    for place, text in (("subject", subject), ("body", body)):
        if EM_DASH in text or EN_DASH in text:
            issues.append(
                LintIssue("em_dash", "the %s contains an em dash or en dash; use a comma, colon or period" % place)
            )

    # hard_wrap
    main = _before_signature(body, sender)
    for number, paragraph in enumerate(_wrap_checked(body, sender), start=1):
        if "\n" in _unix(paragraph).strip():
            issues.append(
                LintIssue(
                    "hard_wrap",
                    "paragraph %d contains a line break inside the paragraph; keep each paragraph on one line" % number,
                )
            )

    # filler
    flat_all = _flat(subject + "\n" + body)
    for phrase in canned_well_wishes(flat_all):
        issues.append(LintIssue("filler", 'filler phrase "%s"' % phrase))
    for phrase in FILLER_PHRASES:
        if phrase in flat_all:
            issues.append(LintIssue("filler", 'filler phrase "%s"' % phrase))

    # exclamation
    if "!" in body:
        issues.append(LintIssue("exclamation", "the body contains an exclamation mark"))

    # forbidden_phrase
    for phrase in sender.forbidden_phrases:
        needle = _flat(phrase)
        if needle and needle in flat_all:
            issues.append(LintIssue("forbidden_phrase", 'forbidden phrase "%s"' % phrase.strip()))

    check_hook = contact is not None and hook_text != ""
    if check_hook:
        assert contact is not None
        hook_flat = _flat(hook_text)

        # false_parallel
        for pattern in _FALSE_PARALLEL_PATTERNS:
            match = pattern.search(hook_flat)
            if match:
                issues.append(
                    LintIssue(
                        "false_parallel",
                        'the hook claims the recipient\'s work is what the sender does ("%s")' % match.group(0),
                    )
                )

        # unapproved_number
        allowed = _number_forms(" ".join(contact.facts))
        for claim in sender.approved_claims.values():
            allowed |= _number_forms(str(claim))
        reported: Set[str] = set()
        for token in _NUMBER_TOKEN.findall(hook_text):
            if token in reported:
                continue
            if "," in token:
                collapsed = token.replace(",", "")
                approved = collapsed in allowed or all(
                    run in allowed for run in _DIGIT_RUN.findall(token)
                )
            else:
                approved = token in allowed
            if not approved:
                reported.add(token)
                issues.append(
                    LintIssue(
                        "unapproved_number",
                        'the hook uses the number "%s", which is not in the contact facts or approved claims' % token,
                    )
                )

    # word_count (warning)
    words = sum(len(paragraph.split()) for paragraph in main)
    if words < MIN_WORDS or words > MAX_WORDS:
        issues.append(
            LintIssue(
                "word_count",
                "body has %d words before the signature; aim for %d to %d" % (words, MIN_WORDS, MAX_WORDS),
                severity="warning",
            )
        )

    # odd_greeting (warning)
    if contact is not None:
        greeting = contact.first_name
        letters = sum(1 for ch in greeting if ch.isalpha())
        odd = (
            letters < 2
            or any(not (ch.isalpha() or ch in ("'", "-", "\u2019")) for ch in greeting)
            or greeting.casefold() in _NON_NAMES
        )
        if odd:
            issues.append(
                LintIssue(
                    "odd_greeting",
                    "the greeting name %r looks unusual; set the first_name column for this contact" % greeting,
                    severity="warning",
                )
            )

    # empty_hook
    if check_hook and not hook_text.strip():
        issues.append(LintIssue("empty_hook", "the hook is empty"))

    return issues
