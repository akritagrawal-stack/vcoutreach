"""Grounding check for proper nouns and numbers in hooks."""
from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

from .types import Contact, LintIssue, SenderProfile

# Unicode aware: [^\W_] is any letter or digit in any script (str patterns are Unicode by default).
_TOKEN = re.compile(r"[^\W_]+(?:[.,'\u2019\-][^\W_]+)*")
_SENTENCE_BREAK = re.compile(r"[.!?\n]")
_CONTRACTION = re.compile(r"(?:'|\u2019)(?:s|m|ve|d|ll|re|t)$", re.IGNORECASE)

_ALLOWLIST = frozenset(
    {
        "i",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    }
)


def _candidates(hook_text: str) -> List[Tuple[str, bool]]:
    """Return (token, is_number) pairs that must be grounded, in order of appearance."""
    hook_text = unicodedata.normalize("NFC", hook_text)
    found: List[Tuple[str, bool]] = []
    previous_end = None
    for match in _TOKEN.finditer(hook_text):
        gap = "" if previous_end is None else hook_text[previous_end:match.start()]
        sentence_start = previous_end is None or bool(_SENTENCE_BREAK.search(gap))
        previous_end = match.end()

        word = _CONTRACTION.sub("", match.group(0))
        for position, part in enumerate(word.split("-")):
            if not part:
                continue
            has_digit = any(ch.isdigit() for ch in part)
            if has_digit:
                found.append((part, True))
                continue
            at_start = sentence_start and position == 0
            if part[0].isupper() and not at_start and part.lower() not in _ALLOWLIST:
                found.append((part, False))
    return found


def check_grounding(hook_text: str, contact: Contact, sender: SenderProfile) -> List[LintIssue]:
    """Flag proper nouns and numbers in the hook that appear nowhere in the known facts."""
    if not hook_text or not hook_text.strip():
        return []
    sources = [contact.name, contact.org, contact.role, *contact.facts]
    sources += [sender.name, sender.org, sender.title]
    haystack = unicodedata.normalize("NFC", "\n".join(s for s in sources if s))
    haystack = haystack.lower().replace("\u2019", "'")
    haystack_plain = haystack.replace(",", "")

    issues: List[LintIssue] = []
    reported = set()
    for token, is_number in _candidates(hook_text):
        needle = token.lower().replace("\u2019", "'")
        if needle in reported:
            continue
        if needle in haystack or (is_number and needle.replace(",", "") in haystack_plain):
            continue
        reported.add(needle)
        kind = "number" if is_number else "name"
        issues.append(
            LintIssue(
                "ungrounded_claim",
                'the hook mentions the %s "%s", which is not in the contact facts or sender profile' % (kind, token),
            )
        )
    return issues
