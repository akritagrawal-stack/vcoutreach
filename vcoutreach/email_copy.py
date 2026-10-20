"""Keep personalized openers separate from reusable email copy."""
from __future__ import annotations

import re

from .contacts import sanitize_text


def safe_greeting_name(first_name: str) -> str:
    """Use a plain name in generated greetings and personalized subjects."""
    name = sanitize_text(first_name, 200).strip()
    if not any(ch.isalpha() for ch in name) or any(
        not (ch.isalpha() or ch in " '-.\u2019") for ch in name
    ):
        return 'there'
    return name


def clean_opener(text: str, first_name: str = '') -> str:
    text = (text or '').strip()
    text = re.sub(r'^(?:hi|hello|dear)\s+[^,\n]+,\s*', '', text, flags=re.IGNORECASE)
    if first_name:
        text = re.sub(r'^' + re.escape(first_name) + r'\s*[,.:]\s*', '', text, flags=re.IGNORECASE)
    return text.strip()


def opener_from_body(body: str, first_name: str = '') -> str:
    """Draft files contain the full email; the database hook contains only its opener."""
    text = clean_opener(body, first_name)
    text = re.sub(r'^Following up in case you missed my previous email\.\s*', '', text)
    return re.split(r'\n\s*\n', text, maxsplit=1)[0].strip()


def _claims(text: str) -> set[str]:
    lower = text.casefold()
    claims = set()
    if re.search(r'\b10m\b.*\bviews\b', lower):
        claims.add('organic_views')
    if (not re.search(r'\b(?:you|your)\b', lower)
            and (re.search(r'\bopen[ -]source\b', lower) and re.search(r'\b(?:software|alternatives?|install\w*|deploy\w*)\b', lower)
                 or re.search(r'\balternatives?\s+to\s+paid\s+software\b', lower))):
        claims.add('product')
    return claims


def unique_paragraphs(paragraphs: list[str]) -> list[str]:
    """Keep the first occurrence of sender claims, including claims already in the opener."""
    result, seen, claims = [], set(), set()
    for paragraph in paragraphs:
        if not paragraph or not paragraph.strip():
            continue
        paragraph = paragraph.strip()
        kept = []
        for sentence in re.split(r'(?<=[.!?])\s+(?=[A-Z])', paragraph):
            key = re.sub(r'\s+', ' ', sentence).casefold()
            current = _claims(sentence)
            if key in seen or current & claims:
                continue
            seen.add(key)
            claims.update(current)
            kept.append(sentence)
        if kept:
            result.append(' '.join(kept))
    return result


def replace_opener(body: str, hook: str, first_name: str = '') -> str:
    """Replace an existing opener and remove sender claims repeated later in the email."""
    paragraphs = re.split(r'\n\s*\n', (body or '').strip())
    greeting = paragraphs.pop(0) if paragraphs and re.match(r'^(?:hi|hello|dear)\b', paragraphs[0], re.IGNORECASE) else ''
    if paragraphs:
        paragraphs.pop(0)
    return '\n\n'.join(unique_paragraphs([greeting or f'Hi {first_name or "there"},', clean_opener(hook, first_name), *paragraphs]))
