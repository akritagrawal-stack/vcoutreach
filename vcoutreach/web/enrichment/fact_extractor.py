"""
VC-specific fact extractor.

Takes raw enrichment artifacts (web search results, LinkedIn data) and produces
structured facts that the LLM hook generator can quote directly.

Fact types (all capped at 240 chars):
  thesis_quote        -  a quoted or paraphrased investment thesis statement
  essay_title         -  title of a public essay/article by the partner
  essay_url           -  URL of a public essay
  podcast_appearance  -  podcast episode the partner appeared on
  portfolio_company   -  a named company the partner invested in
  prior_firm          -  a previous firm the partner worked at
  current_role        -  current title/role at current firm
  recent_investment   -  recently announced investment
  public_writing_topic  -  a topic they write/speak about regularly
  tweet_excerpt       -  excerpt from a public tweet

Each fact is {type, text, source_url, confidence}.
The text field is what gets passed to the LLM as a quotable fact.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any
from vcoutreach.web.vendor import workspace_path

logger = logging.getLogger(__name__)

_MAX_FACT_CHARS = 240
_MAX_SNIPPET_CHARS = 8000

# Known VC blog/writing domains  -  results from these are higher signal
_WRITING_DOMAINS = {
    'medium.com', 'substack.com', 
    'techcrunch.com', 'venturebeat.com', 'fortune.com', 'forbes.com',
    'inc.com', 'fastcompany.com', 'wired.com', 'theinformation.com',
     'sifted.eu', 'strictlyvc.com',
    'betakit.com', 'axios.com', 'bloomberg.com',
}

_PODCAST_MARKERS = [
    'podcast', 'episode', 'interview', ' on ', 'talks about', 'discusses',
    'how i built this', 'how i invest', "masters of scale", "acquired",
    "invest like the best", "the twenty minute vc", "all-in", "this week in startups",
     "my first million", 
]

# Pages that can mention "interview" or " on " but are never a podcast episode.
_NOT_PODCAST_DOMAINS = ('linkedin.com', 'ycombinator.com', 'a16z.com', 'crunchbase.com', 'wikipedia.org',
                        'twitter.com', 'x.com', 'amazon.com', 'goodreads.com')
_WEAK_PODCAST_MARKERS = {' on ', 'talks about', 'discusses', 'interview'}


def _can_be_podcast(url: str, combined: str, title: str = '', partner_name: str = '') -> bool:
    """A podcast fact needs a podcast-like source: not LinkedIn, YC or news pages, not book pages."""
    domain = _domain_from_url(url)
    if any(domain == d or domain.endswith('.' + d) for d in _NOT_PODCAST_DOMAINS):
        return False
    if _is_writing_domain(url) or re.search(r'/(books?|news)(/|$)', (url or '').lower()):
        return False
    from urllib.parse import urlparse
    path = urlparse(url or '').path.lower()
    if re.search(r'/\d{4}(?:/\d{1,2})?/?$', path):
        return False
    name = re.escape(partner_name.strip())
    guest = bool(name) and bool(re.search(r'\b(?:guest(?:\s+is)?|with|featuring|joined by)\s+' + name + r'\b', combined, re.IGNORECASE))
    podcast_host = ('podcast' in domain
                    or domain in {'acquired.com', 'acquired.fm', 'possible.fm', 'mastersofscale.com', '20vc.com',
                                  'open.spotify.com', 'transistor.fm', 'simplecast.fm'}
                    or domain.endswith(('.transistor.fm', '.simplecast.fm')))
    podcast_path = bool(re.search(r'/(?:podcasts?|episodes?)(?:/|$)|/ep[-_]\d+(?:/|$)', path))
    title_signal = bool(re.search(r'\bepisode\s*(?:#?\d+|with|:)\b|\bpodcast\b', title, re.IGNORECASE))
    blog_post = bool(re.search(r'/\d{4}/\d{1,2}/\d{1,2}/|/(?:blog|posts?|p)/', path))
    if blog_post and not guest and not podcast_path:
        return False
    return podcast_host or podcast_path or title_signal or guest and 'podcast' in combined


def _authored_by_partner(title: str, snippet: str, partner_name: str) -> bool:
    """Own-essay typing needs the text itself to say 'by <partner>'."""
    name = (partner_name or '').strip()
    return bool(name) and re.search(r'\bby\s+' + re.escape(name), f'{title} {snippet}', re.IGNORECASE) is not None


# Phrases that often precede a thesis statement
_THESIS_MARKERS = [
    'thesis', 'we invest in', 'we focus on', 'we back', 'looking for',
    'believe in', 'convinced that', 'principle', 'philosophy',
    'mission is', 'our fund', 'our portfolio', "what we look for",
    "i believe", "the key insight", "the core insight",
]

# Phrases suggesting portfolio company mentions
_PORTFOLIO_MARKERS = [
    'invested in', 'led the round', 'seed round', 'series a', 'portfolio company',
    'backed', 'wrote a check', 'our investment', 'announced investment',
    'funding for', 'raised from', 'we led',
]


def _clean(text: str) -> str:
    """Strip HTML entities, tags, and normalize whitespace."""
    # Unescape HTML entities (&amp; &quot; &#x27; etc.)
    text = html.unescape(text)
    # Strip HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _cap(text: str) -> str:
    """Clean and truncate to max fact length."""
    text = _clean(text)
    if len(text) <= _MAX_FACT_CHARS:
        return text
    return text[:_MAX_FACT_CHARS - 3] + '...'


def _domain_from_url(url: str) -> str:
    m = re.match(r'https?://([^/]+)', url or '')
    return m.group(1).lower().replace('www.', '') if m else ''


def _is_writing_domain(url: str) -> bool:
    domain = _domain_from_url(url)
    # Check exact match or subdomain match
    for wd in _WRITING_DOMAINS:
        if domain == wd or domain.endswith('.' + wd):
            return True
    return False


def _extract_from_web_results(results: list[dict], partner_name: str) -> list[dict]:
    """Extract facts from web search result dicts ({url, title, snippet})."""
    facts: list[dict] = []
    first_name = partner_name.split()[0].lower() if partner_name else ''
    last_name = partner_name.split()[-1].lower() if partner_name else ''

    for r in results:
        url = r.get('url', '')
        title = r.get('title', '').strip()
        snippet = r.get('snippet', '') or r.get('description', '')
        snippet = snippet.strip()[:_MAX_SNIPPET_CHARS]
        combined = f'{title} {snippet}'.lower()
        query = r.get('_query', '')

        # Filter: must mention partner name somewhere
        name_present = (
            last_name in combined or
            (first_name and len(first_name) > 3 and first_name in combined)
        )
        if not name_present:
            continue

        domain = _domain_from_url(url)
        is_writing = _is_writing_domain(url)

        # -- Essay / article title detection --
        # If title looks like a real article title (not just a profile page),
        # extract it. Indicators: title-cased, >4 words, has content words.
        if title and len(title.split()) >= 4:
            # Filter out generic site/profile titles
            skip_title_patterns = [
                r'linkedin', r'twitter', r'profile', r'wikipedia',
                r'crunchbase', r'^[A-Z][a-z]+ [A-Z][a-z]+ on',  # User-provided workspace configuration.
                r'\| home$', r'\| about$',
            ]
            is_generic = any(re.search(p, title, re.IGNORECASE) for p in skip_title_patterns)

            if not is_generic and is_writing and _authored_by_partner(title, snippet, partner_name):
                facts.append({
                    'type': 'essay_title',
                    'text': _cap(title),
                    'source_url': url,
                    'confidence': 0.80 if is_writing else 0.55,
                })
                if url:
                    facts.append({
                        'type': 'essay_url',
                        'text': _cap(f'Essay: {title}  -  {url}'),
                        'source_url': url,
                        'confidence': 0.80,
                    })

        # -- Thesis quote detection --
        for marker in _THESIS_MARKERS:
            if marker in combined:
                # Grab the snippet (which has clean-enough text) around the marker
                raw_snippet = _clean(snippet)
                lower_snippet = raw_snippet.lower()
                idx_in_snippet = lower_snippet.find(marker)
                if idx_in_snippet >= 0:
                    # Extract from marker position forward
                    excerpt = raw_snippet[max(0, idx_in_snippet - 15):idx_in_snippet + 200].strip()
                    if len(excerpt) > 40:
                        # Clean confidence: higher if from a writing domain, lower if aggregator
                        is_agg = any(agg in url for agg in ['crunchbase', 'pitchbook', 'tracxn', 'cbinsights', 'dealroom'])
                        conf = 0.75 if is_writing else (0.50 if is_agg else 0.60)
                        facts.append({
                            'type': 'thesis_quote',
                            'text': _cap(excerpt),
                            'source_url': url,
                            'confidence': conf,
                        })
                break  # one per result

        # -- Podcast detection --
        if _can_be_podcast(url, combined, title, partner_name):
            # Extract the podcast name from title if possible
            podcast_text = title if title else snippet[:120]
            facts.append({
                'type': 'podcast_appearance',
                'text': _cap(podcast_text),
                'source_url': url,
                'confidence': 0.65,
            })

        # -- Portfolio company detection --
        for pm in _PORTFOLIO_MARKERS:
            if pm in combined:
                # Extract the capitalized brand name near the marker
                idx = combined.find(pm)
                context = (title + ' ' + snippet)[max(0, idx - 20):idx + 120]
                # Find capitalized words (likely company names)
                caps = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b', context)
                # Filter out partner's own name parts and generic words
                generic = {'The', 'For', 'With', 'From', 'This', 'That', 'When', 'What',
                           'How', 'New', 'Our', 'Their', 'Your', 'Inc', 'LLC', 'Corp'}
                partner_parts = {p.title() for p in partner_name.split()}
                companies = [c for c in caps if c not in generic and c not in partner_parts]
                if companies:
                    company_str = ', '.join(companies[:2])
                    facts.append({
                        'type': 'portfolio_company',
                        'text': _cap(f'{company_str} (via {domain})'),
                        'source_url': url,
                        'confidence': 0.60,
                    })
                break

        # -- Public writing topic detection --
        # If a writing domain result has a meaty snippet, treat the whole thing
        # as a quotable writing topic fact.
        if is_writing and len(snippet) > 80:
            facts.append({
                'type': 'public_writing_topic',
                'text': _cap(f'{title}: {snippet[:200]}' if title else snippet[:200]),
                'source_url': url,
                'confidence': 0.75,
            })

    return facts


def _extract_from_linkedin(data: dict, partner_name: str) -> list[dict]:
    """Extract facts from LinkedIn scrape result dict."""
    facts: list[dict] = []
    url = data.get('url', '')

    # Current role
    headline = (data.get('headline') or '').strip()
    current_role = (data.get('current_role') or '').strip()
    about = (data.get('about_excerpt') or '').strip()

    if current_role:
        facts.append({
            'type': 'current_role',
            'text': _cap(current_role),
            'source_url': url,
            'confidence': 0.90,
        })
    elif headline:
        facts.append({
            'type': 'current_role',
            'text': _cap(headline),
            'source_url': url,
            'confidence': 0.85,
        })

    # About section  -  scan for thesis-like statements
    if about:
        for marker in _THESIS_MARKERS:
            idx = about.lower().find(marker)
            if idx >= 0:
                excerpt = about[max(0, idx - 10):idx + 200].strip()
                if len(excerpt) > 30:
                    facts.append({
                        'type': 'thesis_quote',
                        'text': _cap(excerpt),
                        'source_url': url,
                        'confidence': 0.80,
                    })
                break

        # General about excerpt if substantive
        if len(about) > 60:
            facts.append({
                'type': 'public_writing_topic',
                'text': _cap(about[:240]),
                'source_url': url,
                'confidence': 0.70,
            })

    # Recent posts
    recent_posts = data.get('recent_posts') or []
    for post in recent_posts[:2]:
        post_text = (post.get('text') or post if isinstance(post, str) else '').strip()
        if post_text and len(post_text) > 30:
            facts.append({
                'type': 'public_writing_topic',
                'text': _cap(post_text),
                'source_url': url,
                'confidence': 0.65,
            })

    # Education  -  extract prior firms from education if they look like firms
    education = data.get('education') or []
    for edu_item in education:
        edu_str = edu_item if isinstance(edu_item, str) else str(edu_item)
        if edu_str and len(edu_str) > 3:
            facts.append({
                'type': 'prior_firm',
                'text': _cap(edu_str),
                'source_url': url,
                'confidence': 0.60,
            })

    return facts


def _cap_pb(text: str, n: int = 280) -> str:
    """Clean and truncate to PitchBook-specific max length (280 chars)."""
    text = _clean(text)
    if len(text) <= n:
        return text
    return text[:n - 3] + '...'


# User-provided workspace configuration.
# pitchbook_portfolio_overlap is now only emitted when a scraped portfolio company
# User-provided workspace configuration.
_PORTFOLIO_OVERLAP_ALLOWLIST = set()


def _extract_from_pitchbook(data: dict, partner_name: str) -> list[dict]:
    """Workspace-backed outreach integration."""
    facts: list[dict] = []
    url = data.get('url', '')

    # ── Thesis quote ──────────────────────────────────────────────────────────
    thesis = (data.get('thesis_quote') or data.get('thesis') or '').strip()
    if thesis and len(thesis) > 20:
        facts.append({
            'type': 'pitchbook_thesis_quote',
            'text': _cap_pb(thesis),
            'source_url': url,
            'confidence': 0.90,
        })

    # ── Sector focus ──────────────────────────────────────────────────────────
    sectors = data.get('sectors', []) or []
    if sectors:
        sector_str = ', '.join(str(s).strip() for s in sectors[:6] if str(s).strip())
        if sector_str:
            facts.append({
                'type': 'pitchbook_sector_focus',
                'text': _cap_pb(f'Sectors: {sector_str}'),
                'source_url': url,
                'confidence': 0.85,
            })

    # ── Board seats ───────────────────────────────────────────────────────────
    board_seats = data.get('board_seats', []) or []
    if board_seats:
        # FIX 3: Filter out entries that are the partner's own name or name parts.
        # PitchBook's board-seats table first cell sometimes contains the member's
        # User-provided workspace configuration.
        # nonprofit/association rows. Exclude any entry whose text matches ≥2 words
        # of the partner name.
        partner_name_parts = set(
            w.lower() for w in (partner_name or '').split() if len(w) > 2
        )
        def _is_own_name(seat: str) -> bool:
            if not partner_name_parts:
                return False
            seat_lower = seat.lower()
            matched = sum(1 for w in partner_name_parts if w in seat_lower)
            return matched >= 2

        filtered_seats = [
            str(s).strip() for s in board_seats[:8]
            if str(s).strip() and not _is_own_name(str(s).strip())
        ]
        if filtered_seats:
            seat_str = ', '.join(filtered_seats)
            facts.append({
                'type': 'pitchbook_board_seat',
                'text': _cap_pb(f'Board member: {seat_str}'),
                'source_url': url,
                'confidence': 0.85,
            })

    # ── Recent deals ──────────────────────────────────────────────────────────
    recent_deals = data.get('recent_deals', []) or []
    for deal in recent_deals[:5]:
        if isinstance(deal, dict):
            raw_text = deal.get('raw', '')
            company = deal.get('company', '')
            round_name = deal.get('round', '')
            amount = deal.get('amount', '')
            deal_date = deal.get('date', '')

            if company:
                # Format: "Led {round} in {company} ({date}) - {amount}" with graceful fallback
                if round_name:
                    deal_str = f'Led {round_name} in {company}'
                else:
                    deal_str = company
                date_amount_parts = []
                if deal_date:
                    date_amount_parts.append(deal_date)
                if amount:
                    date_amount_parts.append(amount)
                if date_amount_parts:
                    deal_str += f' ({" - ".join(date_amount_parts)})'
                facts.append({
                    'type': 'pitchbook_recent_deal',
                    'text': _cap_pb(deal_str),
                    'source_url': url,
                    'confidence': 0.88,
                })
            elif raw_text and len(raw_text) > 5:
                facts.append({
                    'type': 'pitchbook_recent_deal',
                    'text': _cap_pb(raw_text),
                    'source_url': url,
                    'confidence': 0.75,
                })
        elif isinstance(deal, str) and len(deal) > 5:
            facts.append({
                'type': 'pitchbook_recent_deal',
                'text': _cap_pb(deal),
                'source_url': url,
                'confidence': 0.75,
            })

    # User-provided workspace configuration.
    # FIX 4: keyword/bio scanning removed (73-83% FP rate per audit).
    # Only emit pitchbook_portfolio_overlap when a scraped portfolio company name
    # User-provided workspace configuration.
    portfolio_company_names: list[str] = []
    for deal in (data.get('recent_deals', []) or []):
        co = deal.get('company', '') if isinstance(deal, dict) else ''
        if co:
            portfolio_company_names.append(co)
    for seat in (data.get('board_seats', []) or []):
        if seat and isinstance(seat, str):
            portfolio_company_names.append(seat)
    for inv in (data.get('recent_investments', []) or []):
        co = inv.get('company', '') if isinstance(inv, dict) else str(inv)
        if co:
            portfolio_company_names.append(co)

    for co_name in portfolio_company_names:
        if co_name.lower().strip() in _PORTFOLIO_OVERLAP_ALLOWLIST:
            facts.append({
                'type': 'pitchbook_portfolio_overlap',
                'text': _cap_pb(f'Portfolio company: {co_name}  -  portfolio overlap'),
                'source_url': url,
                'confidence': 0.90,
            })
            break  # one overlap fact max

    return facts


def extract_pitchbook_facts(scraped_data: dict) -> list[dict]:
    """
    Public helper: extract PitchBook facts from a scraped profile dict.
    Same interface as the internal extractor  -  useful for standalone calls.

    Returns list of {type, text, source_url, confidence} dicts.
    """
    partner_name = scraped_data.get('partner_name', '') or scraped_data.get('name', '')
    return _extract_from_pitchbook(scraped_data, partner_name)


def extract_facts(source: str, raw: dict) -> list[dict]:
    """
    Extract structured facts from a raw enrichment artifact.

    Args:
        source: 'web_search' | 'linkedin' | 'email_guess' | 'pitchbook'
        raw: the raw data dict from the scraper

    Returns:
        list of {type, text, source_url, confidence} dicts

    Note: email_guess source returns no facts (emails are stored separately).
    """
    if source == 'email_guess':
        return []

    partner_name = raw.get('partner_name', '') or raw.get('name', '')

    if source == 'web_search':
        results = raw.get('results', [])
        if not results:
            return []
        return _extract_from_web_results(results, partner_name)

    if source == 'linkedin':
        return _extract_from_linkedin(raw, partner_name)

    if source == 'pitchbook':
        return _extract_from_pitchbook(raw, partner_name)

    logger.warning('Unknown enrichment source: %r', source)
    return []


def _is_junk_current_role(text: str) -> bool:
    """
    Return True if a current_role fact is junk and should be dropped.

    Junk patterns:
    - Pronouns-only strings: "He/Him", "She/Her", "They/Them"
    - Single-word strings with no role context (likely newsletter name)
    - Strings without any of: comma, " at ", " @", "Inc", "LLC", "-", "Partner",
      "Principal", "Associate", "Founder", "Investor", "Director", "Manager",
      "General", "Managing", "Operating"
    """
    t = text.strip()
    if not t:
        return True

    # Pronoun-only strings (case-insensitive)
    if re.match(r'^(he|she|they)[/|](him|her|them)$', t, re.IGNORECASE):
        return True

    # Single-word (no spaces)  -  likely a newsletter/brand name with no role info
    if ' ' not in t:
        return True

    # Require at least one substantive signal
    _ROLE_SIGNALS = (
        ',', ' at ', ' @ ', 'Inc', 'LLC', 'Ltd', ' - ',
        'Partner', 'Principal', 'Associate', 'Founder', 'Investor',
        'Director', 'Manager', 'General', 'Managing', 'Operating',
        'Officer', 'Head', 'VP', 'President', 'Advisor', 'Angel',
        'Analyst', 'Scout', 'Fellow', 'Entrepreneur',
    )
    t_lower = t.lower()
    if not any(sig.lower() in t_lower for sig in _ROLE_SIGNALS):
        return True

    return False


# Regex matching YC batch cohort labels that look like company names
# e.g. "Summer 2013", "Winter 2009", "S2013", "W22", "F2019"
_YC_BATCH_PATTERN = re.compile(
    r'^(?:Summer|Winter|Spring|Fall|S|W|F)\s*(?:20\d{2}|\d{2})$',
    re.IGNORECASE,
)


def _is_yc_batch_label(text: str) -> bool:
    """
    Return True if ``text`` is a YC cohort batch identifier mistakenly
    stored as a company name (e.g. "Summer 2013", "Winter 2009", "W22").
    """
    return bool(_YC_BATCH_PATTERN.match(text.strip()))


def facts_to_llm_format(facts: list[dict], *, limit: int | None = 10) -> list[dict]:
    """
    Convert internal fact dicts to the {source, text} format expected by
    build_hook_prompt() in app/llm/prompts.py.

    Filters to high-confidence facts (>=0.60) and dedupes by text prefix.
    Orders by confidence descending, then type priority.

    Also filters junk current_role values (pronouns, single-word newsletter names,
    strings with no substantive role signal) and YC cohort batch labels mistakenly
    stored as portfolio company names (e.g. "Summer 2013 is in your portfolio").
    Dropped entries are logged to data/logs/enrichment_filter.log.
    """
    if not facts:
        return []

    TYPE_PRIORITY = {
        # PitchBook facts  -  highest priority (structured, authoritative)
        'pitchbook_thesis_quote': 1,
        'pitchbook_recent_deal': 2,
        'pitchbook_portfolio_overlap': 3,
        'pitchbook_board_seat': 4,
        'pitchbook_sector_focus': 5,
        # Legacy fact types
        'thesis_quote': 6,
        'public_writing_topic': 7,   # meaty snippets with actual content
        'essay_title': 8,            # titles are good but less quotable than full text
        'podcast_appearance': 9,
        'portfolio_company': 10,
        'current_role': 11,
        'essay_url': 12,             # URLs alone are least quotable
        'recent_investment': 13,
        'prior_firm': 14,
        'tweet_excerpt': 15,
    }

    # pitchbook_api facts are authoritative but written without a confidence field  - 
    # treat them as 0.85 so they pass the filter and sort near the top.
    _PB_PREFIX = 'pitchbook_'

    def _confidence(f: dict) -> float:
        if 'confidence' in f:
            return f['confidence']
        if f.get('type', '').startswith(_PB_PREFIX):
            return 0.85
        return 0.0

    # Filter by minimum confidence (include thesis_quotes at 0.55+ since they're valuable)
    filtered = [f for f in facts if _confidence(f) >= 0.55]

    # Filter junk current_role values (pronouns, newsletter-only strings, no role signal)
    _dropped_junk: list[str] = []
    _clean: list[dict] = []
    for f in filtered:
        if f.get('type') == 'current_role':
            text_val = (f.get('text') or f.get('value') or '').strip()
            if _is_junk_current_role(text_val):
                _dropped_junk.append(text_val)
                continue
        _clean.append(f)

    if _dropped_junk:
        try:
            import os
            from pathlib import Path as _Path
            _log_dir = workspace_path('data/logs')
            _log_dir.mkdir(parents=True, exist_ok=True)
            _log_file = _log_dir / 'enrichment_filter.log'
            with open(str(_log_file), 'a', encoding='utf-8') as _lf:
                import datetime as _dt
                _ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                for _v in _dropped_junk:
                    _lf.write(f'{_ts} [DROP current_role junk] {_v!r}\n')
        except Exception as _log_exc:
            logger.debug('enrichment_filter log write failed: %s', _log_exc)
        logger.info(
            'facts_to_llm_format: dropped %d junk current_role fact(s): %s',
            len(_dropped_junk), _dropped_junk,
        )
    filtered = _clean

    # Filter YC cohort batch labels stored as portfolio company names.
    # YC's /companies page uses batch identifiers like "Summer 2013", "Winter 2009"
    # as section headings; the firm_website scraper can pick these up as company names.
    # Any fact whose text STARTS WITH a YC batch label pattern is dropped.
    _dropped_batch: list[str] = []
    _clean2: list[dict] = []
    _PORTFOLIO_TYPES = {'portfolio_company', 'pitchbook_recent_deal', 'pitchbook_board_seat'}
    for f in filtered:
        if f.get('type') in _PORTFOLIO_TYPES:
            text_val = (f.get('text') or f.get('value') or '').strip()
            # Extract the leading token (company name before " is in your portfolio..." etc.)
            _first_token = text_val.split(' is ')[0].split(' (')[0].strip()
            if _is_yc_batch_label(_first_token):
                _dropped_batch.append(text_val)
                continue
        _clean2.append(f)

    if _dropped_batch:
        try:
            from pathlib import Path as _Path2
            _log_dir2 = _Path2(__file__).parent.parent.parent / 'data' / 'logs'
            _log_dir2.mkdir(parents=True, exist_ok=True)
            _log_file2 = _log_dir2 / 'enrichment_filter.log'
            with open(str(_log_file2), 'a', encoding='utf-8') as _lf2:
                import datetime as _dt2
                _ts2 = _dt2.datetime.now(_dt2.timezone.utc).isoformat()
                for _v2 in _dropped_batch:
                    _lf2.write(f'{_ts2} [DROP yc_batch_label] {_v2!r}\n')
        except Exception as _log_exc2:
            logger.debug('enrichment_filter log write failed (batch): %s', _log_exc2)
        logger.info(
            'facts_to_llm_format: dropped %d YC batch label fact(s): %s',
            len(_dropped_batch), _dropped_batch,
        )
    filtered = _clean2

    # Sort: PB facts first (source tier), then high confidence, then type priority.
    # This ensures pitchbook_* facts always appear before linkedin/web facts in the
    # prompt regardless of confidence score differences.
    def _source_tier(f: dict) -> int:
        ftype = f.get('type', '')
        if ftype.startswith('pitchbook_'):
            return 0  # PitchBook API  -  highest priority tier
        if ftype in ('current_role', 'prior_firm', 'thesis_quote'):
            return 1  # LinkedIn / firm_website sourced types
        return 2  # web_search and everything else

    filtered.sort(key=lambda f: (
        _source_tier(f),
        -_confidence(f),
        TYPE_PRIORITY.get(f.get('type', ''), 99),
    ))

    # Dedupe by text similarity (first 60 chars) with per-type caps
    seen_prefixes: set[str] = set()
    type_counts: dict[str, int] = {}
    TYPE_CAPS = {
        # PitchBook fact caps
        'pitchbook_thesis_quote': 3,
        'pitchbook_recent_deal': 5,
        'pitchbook_portfolio_overlap': 1,  # allowlist-only  -  1 is sufficient
        'pitchbook_board_seat': 1,
        'pitchbook_sector_focus': 1,
        # Legacy fact type caps
        'essay_title': 4,       # max 4 essay titles
        'essay_url': 2,         # max 2 URLs
        'podcast_appearance': 3, # max 3 podcast mentions
        'thesis_quote': 5,      # thesis quotes are gold  -  allow up to 5
        'public_writing_topic': 4,
        'portfolio_company': 3,
        'current_role': 1,
        'prior_firm': 2,
    }

    result: list[dict] = []
    for f in filtered:
        text = (f.get('text') or f.get('value') or '').strip()
        if not text:
            continue
        ftype = f.get('type', 'unknown')
        cap = TYPE_CAPS.get(ftype, 3)
        if limit is not None and type_counts.get(ftype, 0) >= cap:
            continue
        prefix = text[:60].lower()
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        type_counts[ftype] = type_counts.get(ftype, 0) + 1
        result.append({
            'source': ftype,
            'type': ftype,
            'provider': f.get('provider') or f.get('source', ''),
            'source_url': f.get('source_url', ''),
            'person_specific': f.get('person_specific', False),
            'text': text,
        })

    return result if limit is None else result[:limit]
