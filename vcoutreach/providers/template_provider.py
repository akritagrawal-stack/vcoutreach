"""Offline deterministic template provider."""
from __future__ import annotations

import re

from vcoutreach.types import Hook, InsufficientFacts, PersonalizationRequest

# A fact that opens like a sentence: a pronoun then a space, or one of these contractions
# (straight or curly apostrophe), matched case-insensitively.
_SENTENCE_START = re.compile(
    r"(?:(?:you|i|we|they|he|she|it)\s"
    r"|(?:you['’](?:ve|re|ll|d)"
    r"|i['’](?:m|ve|ll|d)"
    r"|we['’](?:re|ve|ll)"
    r"|they['’](?:re|ve)"
    r"|(?:he|she|it)['’]s)(?!\w))",
    re.IGNORECASE,
)
SENTENCE_FACT_MESSAGE = (
    "the offline template needs facts written as noun phrases "
    '(for example "your 2024 essay on X"), not sentences'
)


class TemplateProvider:
    """Builds a fixed-shape hook from the contact's first fact; never touches the network."""

    name = "template"

    def __init__(self) -> None:
        pass

    def personalize(self, request: PersonalizationRequest) -> Hook:
        """Return a deterministic hook built from the first non-blank fact."""
        for fact in request.contact.facts:
            cleaned = fact.strip()
            if not cleaned:
                continue
            if _SENTENCE_START.match(cleaned):
                raise InsufficientFacts(SENTENCE_FACT_MESSAGE)
            stem = cleaned.rstrip(".").rstrip()
            if stem.startswith("Your "):
                stem = "your " + stem[len("Your "):]
            if stem:
                return Hook(
                    text=f"I came across {stem}, and I found it interesting.",
                    provider=self.name,
                    model="none",
                )
        raise InsufficientFacts(f"No facts are available for {request.contact.email or request.contact.id}.")
