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
# Facts written as a verb phrase about the contact ("Wrote an essay on X", "Hosts a weekly podcast"). Pasting
# these after "I came across" is ungrammatical, so they are quoted instead. Noun phrases keep the original stem.
_VERB_LEAD = re.compile(
    r"(?:wrote|writes|hosts?|hosted|leads?|led|runs?|ran|founded|founds?|co-?founded|built|builds?|launch(?:ed|es)|"
    r"joined|joins|raised|raises|published|publishes|spoke|speaks|manages?|managed|invested|invests|authored|authors|"
    r"created|creates|started|starts|sold|acquired|mentors?|mentored|teach(?:es)?|taught|maintains?|maintained|"
    r"posted|posts|shared|shares|announced|announces|serves?|served|works?|worked|previously|currently|recently)\b",
    re.IGNORECASE,
)
_VERB_THEN_ARTICLE = re.compile(
    r"(?!its\b|this\b|his\b|was\b|has\b|is\b)\w+(?:ed|s)\s+(?:a|an|the|their|his|her|our|over|with|at|in|on|to|for|from)\b",
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
            if stem and (_VERB_LEAD.match(stem) or _VERB_THEN_ARTICLE.match(stem)):
                shown = stem if '"' in stem else '"%s"' % stem
                return Hook(
                    text=(
                        "I was reading about you and came across this line, which stayed with me: %s. "
                        "I wanted to reach out directly rather than send a generic note, "
                        "and I would value your thoughts on what we are building." % shown
                    ),
                    provider=self.name,
                    model="none",
                )
            if stem:
                return Hook(
                    text=f"I came across {stem}, and I found it interesting.",
                    provider=self.name,
                    model="none",
                )
        raise InsufficientFacts(f"No facts are available for {request.contact.email or request.contact.id}.")
