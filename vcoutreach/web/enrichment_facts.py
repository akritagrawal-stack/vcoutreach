"""Bounded, provenance-carrying enrichment facts for the drafting engine.

Enrichment (web search, firm site, LinkedIn, PitchBook) stores raw fact entries in the
``enrichment`` table. The engine drafts from ``Contact.facts`` (plain strings), so this
module picks the best few stored entries and renders each one as a string that carries its
source URL. The grounding check only ever sees what is returned here; nothing is invented.
"""
from __future__ import annotations
import json
import re
from dataclasses import replace
from urllib.parse import urlparse

DEFAULT_LIMIT = 8
MIN_WORDS = 4
MIN_CONFIDENCE = 0.55
MAX_TEXT_CHARS = 280

# Lower rank is better. Thesis quotes first, then writing, talks and deals the partner is tied to.
_TYPE_RANK = {
    'pitchbook_thesis_quote': 0, 'thesis_quote': 0,
    'pitchbook_recent_deal': 1,
    'public_writing_topic': 2, 'essay_title': 2, 'podcast_appearance': 2,
    'pitchbook_sector_focus': 3, 'pitchbook_board_seat': 3, 'current_role': 3,
    'portfolio_company': 4,
    'prior_firm': 5, 'tweet_excerpt': 5,
}
# URL-only duplicates carry no quotable content of their own.
_SKIP_TYPES = {'essay_url'}
# Web search results are third party mentions; other sources are about the partner by construction.
_WEB_SOURCES = {'web_search', 'web_search_v2'}
_GENERIC_FIRM_WORDS = {'capital', 'ventures', 'venture', 'partners', 'group', 'fund', 'labs', 'holdings',
                       'management', 'investments', 'the', 'and', 'company', 'inc'}
# The house style bans long dashes; scraped text may contain them.
_DASHES = {ord(chr(0x2014)): '-', ord(chr(0x2013)): '-'}


def _clean(text):
    return re.sub(r'\s+', ' ', str(text or '').translate(_DASHES)).strip()


def _real_url(url):
    if not isinstance(url, str):
        return ''
    url = url.strip()
    parsed = urlparse(url)
    return url if parsed.scheme in ('http', 'https') and '.' in parsed.netloc and ' ' not in url else ''


def _is_fragment(text):
    """True for text that starts mid-word or mid-sentence, or is too short to be a fact."""
    if len(text.split()) < MIN_WORDS:
        return True
    first = text[0]
    return first.isalpha() and first.islower()


def _name_tokens(name):
    return [t for t in re.findall(r"[a-z]+", (name or '').lower()) if len(t) >= 3]


def _firm_tokens(firm):
    return [t for t in re.findall(r"[a-z0-9]+", (firm or '').lower()) if len(t) >= 3 and t not in _GENERIC_FIRM_WORDS]


def _about_partner(haystack, name, firm):
    """Relevance 0 to 2: one point for the partner's last name, one for the firm."""
    score = 0
    tokens = _name_tokens(name)
    # The last name is the strongest signal; first names alone are too common.
    if tokens and tokens[-1] in haystack:
        score += 1
    if any(t in haystack for t in _firm_tokens(firm)):
        score += 1
    return score


def _authored_by(text, name):
    """Authorship is only claimed when the stored text itself says 'by <partner name>'."""
    return bool(name.strip()) and re.search(r'\bby\s+' + re.escape(name.strip()), text, re.IGNORECASE) is not None


def _render(ftype, text, url, source, name):
    """Phrase the fact for the engine. Third party web results are never presented as the partner's own."""
    if source in _WEB_SOURCES:
        if ftype in ('essay_title', 'public_writing_topic'):
            title = text.split(': ', 1)[0] if ftype == 'public_writing_topic' else text
            if _authored_by(text, name):
                return f'An essay by {name.strip()} titled {title}, found at {url}'
            return f'An essay titled {title}, found at {url}'
        if ftype == 'podcast_appearance':
            return f'A web page titled {text}, found at {url}'
        if ftype == 'thesis_quote':
            return f'A web page found at {url} says: {text}'
    return f'{text} (source: {url})'


def select_enrichment_facts(entries, name='', firm='', limit=DEFAULT_LIMIT):
    """Pick the best distinct facts from raw entries ({source, type, text, source_url, confidence}).

    Returns a list of {'text', 'source_url', 'type'} where ``text`` already carries the URL.
    """
    scored, seen_text, seen_url = [], set(), set()
    for entry in entries:
        ftype = entry.get('type') or 'unknown'
        if ftype in _SKIP_TYPES:
            continue
        url = _real_url(entry.get('source_url'))
        text = _clean(entry.get('text') or entry.get('value'))[:MAX_TEXT_CHARS].strip()
        if not url or not text or _is_fragment(text) or text.startswith(('http://', 'https://')):
            continue
        try:
            confidence = float(entry['confidence']) if 'confidence' in entry else (0.85 if ftype.startswith('pitchbook_') else 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < MIN_CONFIDENCE:
            continue
        key = re.sub(r'[^a-z0-9]', '', text.lower())[:60]
        if key in seen_text:
            continue
        seen_text.add(key)
        relevance = _about_partner((text + ' ' + url).lower(), name, firm)
        scored.append((-relevance, _TYPE_RANK.get(ftype, 6), -confidence, len(scored), ftype, text, url, entry.get('source', '')))
    scored.sort()
    result = []
    for _, _, _, _, ftype, text, url, source in scored:
        if url in seen_url:
            continue
        seen_url.add(url)
        result.append({'text': _render(ftype, text, url, source, name), 'source_url': url, 'type': ftype})
        if len(result) >= limit:
            break
    return result


def _stored_entries(partner_id, db_path=None):
    from vcoutreach.web.db import get_conn
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT source, url, facts_json FROM enrichment WHERE partner_id=? AND source!='email_guess' "
                            "AND facts_json IS NOT NULL ORDER BY id", (partner_id,)).fetchall()
    finally:
        conn.close()
    entries = []
    for row in rows:
        try:
            facts = json.loads(row['facts_json'])
        except (TypeError, ValueError):
            continue
        for fact in facts if isinstance(facts, list) else []:
            if isinstance(fact, dict):
                entries.append({**fact, 'source': row['source'], 'source_url': fact.get('source_url') or row['url']})
    return entries


def enrichment_facts_for(slug, limit=DEFAULT_LIMIT, db_path=None):
    """Best stored enrichment facts for the contact with this slug (empty list when none)."""
    from vcoutreach.web.db import get_conn
    conn = get_conn(db_path)
    try:
        partner = conn.execute('SELECT id, partner_name, firm FROM partners WHERE slug=?', (slug,)).fetchone()
    finally:
        conn.close()
    if partner is None:
        return []
    return select_enrichment_facts(_stored_entries(partner['id'], db_path), partner['partner_name'] or '', partner['firm'] or '', limit)


def with_enrichment(contact, limit=DEFAULT_LIMIT, db_path=None):
    """Contact with enrichment facts appended after its own. CSV facts keep priority and are never dropped."""
    extra = enrichment_facts_for(contact.id, limit, db_path)
    if not extra:
        return contact
    facts = tuple(contact.facts) + tuple(f['text'] for f in extra if f['text'] not in contact.facts)
    urls = tuple(dict.fromkeys(tuple(contact.source_urls) + tuple(f['source_url'] for f in extra)))
    return replace(contact, facts=facts, source_urls=urls)
