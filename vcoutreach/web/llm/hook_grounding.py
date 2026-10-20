"""Deterministic grounding gate for generated hooks.

A hook is grounded only when every specific claim about the partner (quoted
phrases, capitalized titles, things they wrote, said or back) matches a stored
fact snippet or partner field, and every number matches the sender's approved
wording exactly (for example "10M" never becomes "10M+").
"""
from __future__ import annotations

import json
import re

_STOP = frozenset(
    "that this with from have been were your their about they them what when which would could should into "
    "also more most than then there here just like very some such only over under while where those these "
    "will does done being each other another because through between around across stood caught resonated landed struck".split()
)
_TRIGGER = re.compile(
    r"\b(?:you(?:'ve|’ve|\s+have)?\s+(?:back(?:ed|ing)?|wrote|written|write|said|say|argued?|published|"
    r"talked|spoke|believe[sd]?|champion(?:ed)?|support(?:ed)?|fund(?:ed)?|led|lead|invest(?:ed|ing)?|built|build|"
    r"focus(?:ed)?|bet|care)|written about|your\s+(?:essay|post|writing|piece|article|thesis|podcast|interview|"
    r"talk|book|investments?|portfolio|work|take|view))\b",
    re.IGNORECASE,
)
_ANALOGY = re.compile(
    r"\b(?:applying (?:that|the|this) same|same \w+(?: \w+)? (?:principle|logic|approach|playbook)|"
    r"just as|just like|much like|similar to how)\b",
    re.IGNORECASE,
)
_QUOTE = re.compile(r"[\"“]([^\"”]{4,})[\"”]")
_NUMBER = re.compile(r"(?<![\w.])\$?(\d[\d,]*(?:\.\d+)?)\s?(%|[MKBmkb]\b|million|billion|thousand)?(\+)?")
_CAPS = re.compile(r"\b[A-Z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*)+")

UNGROUNDED_APPROVAL_MESSAGE = ('This hook makes claims that are not in the saved research, so it cannot be approved. '
                               'Regenerate the hook or edit it, then try again.')


def grounding_state(draft) -> str:
    draft = dict(draft)
    if not (draft.get('hook') or '').strip():
        return 'no hook'
    check = draft.get('hallucination_check') or ''
    if check == 'grounded':
        return check
    return check if check.startswith('ungrounded:') else 'ungrounded: hook has not been checked against the saved research'


def hook_is_ungrounded(draft) -> bool:
    draft = dict(draft)
    return bool((draft.get('hook') or '').strip()) and draft.get('hallucination_check') != 'grounded'


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("’", "'").replace("“", '"').replace("”", '"')).strip()


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z][a-z'-]+", _norm(text))
    return {w.replace("'s", "")[:5] for w in words if len(w) >= 4 and w not in _STOP}


def _flatten(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _flatten(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _flatten(v)]
    return []


def _partner_texts(partner: dict, enrichment_facts: list[dict] | None, person_specific: bool = False) -> list[str]:
    texts = []
    for fact in enrichment_facts or []:
        if not isinstance(fact, dict):
            continue
        source = fact.get('provider') or fact.get('source') or ''
        ftype = fact.get('type') or fact.get('source') or ''
        generic_firm = source == 'firm_website' or ftype.startswith('firm_')
        if person_specific and generic_firm and not (fact.get('person_specific') or ftype in {'partner_bio', 'partner_title'}):
            continue
        texts.append(str(fact.get('text') or fact.get('value') or ''))
    raw = partner.get("facts")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    for entry in raw if isinstance(raw, (list, tuple)) else [raw]:
        if isinstance(entry, dict) and ('text' in entry or 'value' in entry):
            texts += _partner_texts({}, [entry], person_specific)
        else:
            texts += _flatten(entry)
    return [t for t in texts if t.strip()]


def _sender_texts(sender_profile: dict | None, facts: dict | None) -> list[str]:
    return _flatten(sender_profile or {}) + _flatten(facts or {})


def _number_ok(value: str, suffix: str, plus: str, corpus: str) -> bool:
    """Exact wording match: a trailing plus in the hook needs a plus in the source, and the reverse."""
    pattern = r"(?<![\w.])\$?" + re.escape(value) + (r"\s?" + re.escape(suffix.lower()) if suffix else "") + (r"\+" if plus else r"(?!\+)")
    return re.search(pattern, corpus) is not None


def check_hook_grounding(hook: str, partner: dict, enrichment_facts: list[dict] | None = None,
                         sender_profile: dict | None = None, facts: dict | None = None) -> tuple[bool, str]:
    """Return (grounded, reason). Reason is empty when grounded."""
    snippets = [_norm(t) for t in _partner_texts(partner, enrichment_facts)]
    person_snippets = [_norm(t) for t in _partner_texts(partner, enrichment_facts, person_specific=True)]
    sender_texts = _sender_texts(sender_profile, facts)
    own_names = " ".join(str(partner.get(k) or "") for k in ("partner_name", "name", "firm", "company", "role", "title"))
    partner_corpus = " ".join(snippets) + " " + _norm(own_names)
    sender_corpus = _norm(" ".join(sender_texts))
    everything = partner_corpus + " " + sender_corpus
    text = hook or ""

    for quoted in _QUOTE.findall(text):
        if _norm(quoted) not in everything:
            return False, f'quoted phrase "{quoted.strip()[:60]}" is not in the stored facts'

    if _ANALOGY.search(text):
        match = _ANALOGY.search(text).group(0)
        if _norm(match) not in everything:
            return False, f'invented analogy "{match}"'

    for number in _NUMBER.finditer(text):
        value, suffix, plus = number.group(1), number.group(2), number.group(3)
        if len(value.replace(",", "")) < 2 and not suffix:
            continue
        if not (_number_ok(value, suffix, plus, sender_corpus) or _number_ok(value, suffix, plus, partner_corpus)):
            return False, f'number "{number.group(0).strip()}" does not match the stored wording'

    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        for caps in _CAPS.finditer(sentence):
            phrase = caps.group(0)
            words = phrase.split()
            if caps.start() == 0 or sentence[: caps.start()].strip() == "":
                words = words[1:]
            if len(words) >= 2 and _norm(" ".join(words)) not in everything:
                return False, f'title or name "{" ".join(words)[:60]}" is not in the stored facts'
        trigger = _TRIGGER.search(sentence)
        if not trigger:
            continue
        claim = sentence[trigger.start():]
        needed = _stems(claim) - _stems(trigger.group(0)) - _stems(own_names) - _stems(" ".join(sender_texts))
        if not person_snippets:
            return False, f'claim "{claim.strip()[:80]}" needs a person-specific stored snippet'
        if not needed:
            continue
        best = max((len(needed & _stems(s)) / len(needed) for s in person_snippets), default=0.0)
        if best < 0.75:
            return False, f'claim "{claim.strip()[:80]}" has no matching stored snippet'
    return True, ""
