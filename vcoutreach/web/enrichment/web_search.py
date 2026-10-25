"""
Web search enrichment for VC partners.

Primary backend: SearXNG at SEARXNG_URL (default http://localhost:8080).
Fallback: Brave API if ENABLE_BRAVE_FALLBACK=1 and BRAVE_API_KEY is set.
Caches results in web_search_cache table (TTL: 7 days).

Per-partner query set:
  1. "{name}" "{firm}" thesis
  2. "{name}" essay site:medium.com OR site:substack.com
  3. "{name}" portfolio investment
  4. "{name}" podcast

Returns list of {url, title, snippet} dicts, deduped across all 4 queries.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from vcoutreach.web.vendor import workspace_path, setting_value, validated_url, require_dependency

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10
_CACHE_TTL_DAYS = 7
_RATE_LIMIT_INTERVAL = 1.0

_BRAVE_WEB_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'

# Module-level state
_searxng_available: bool | None = None
_searxng_fail_time: float | None = None
_last_request_time: float = 0.0


def _normalize(q: str) -> str:
    return unicodedata.normalize('NFKC', q).lower().strip()


def _get_db_path() -> Path:
    """Return the VC emails database path from config."""
    try:
        from vcoutreach.web.config import DB_PATH
        return DB_PATH
    except ImportError:
        return workspace_path('data/outreach.db')


def _get_searxng_url() -> str:
    try:
        from vcoutreach.web.config import SEARXNG_URL
        return SEARXNG_URL or 'http://localhost:8080'
    except ImportError:
        return os.environ.get('SEARXNG_URL', 'http://localhost:8080')


def _ensure_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_search_cache (
            query TEXT PRIMARY KEY,
            results_json TEXT NOT NULL,
            cached_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _cache_get(query_key: str, db_path: Path) -> list[dict] | None:
    """Return cached results if fresh (<7 days), else None."""
    try:
        conn = sqlite3.connect(str(db_path))
        _ensure_cache_table(conn)
        row = conn.execute(
            'SELECT results_json, cached_at FROM web_search_cache WHERE query = ?',
            (query_key,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        # TTL check
        cached_at = row[1]
        try:
            dt = datetime.fromisoformat(cached_at.replace('Z', '+00:00'))
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
            if age_days > _CACHE_TTL_DAYS:
                return None
        except Exception:
            pass  # If we can't parse the date, use the cache anyway
        return json.loads(row[0])
    except Exception as exc:
        logger.debug('Cache get error: %s', exc)
        return None


def _cache_set(query_key: str, results: list[dict], db_path: Path) -> None:
    try:
        conn = sqlite3.connect(str(db_path))
        _ensure_cache_table(conn)
        conn.execute(
            'INSERT OR REPLACE INTO web_search_cache (query, results_json, cached_at) VALUES (?, ?, ?)',
            (query_key, json.dumps(results), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug('Cache set error: %s', exc)


def _rate_limit() -> None:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _RATE_LIMIT_INTERVAL:
        time.sleep(_RATE_LIMIT_INTERVAL - elapsed)
    _last_request_time = time.time()


def _searxng_search(query: str, count: int = 10) -> list[dict]:
    """Hit SearXNG JSON API. Returns list of {url, title, snippet}."""
    global _searxng_available, _searxng_fail_time

    # Re-probe after 5 minutes if previously failed
    if _searxng_available is False and _searxng_fail_time:
        if time.time() - _searxng_fail_time > 300:
            _searxng_available = None

    if _searxng_available is False:
        return []

    try:
        requests = require_dependency("requests")
    except ImportError as exc:
        logger.warning("%s", exc)
        return []

    # SearXNG is opt-in: users without one have no SEARXNG_URL saved, and the
    # search step then simply returns no web results. Nothing is dialed on a
    # guess at a default local port.
    configured = (setting_value("SEARXNG_URL", "") or "").strip()
    if not configured:
        return []
    try:
        url = validated_url(configured, origin_only=True, loopback=True)
    except ValueError as exc:
        logger.warning("SearXNG URL ignored: %s", exc)
        return []
    params = {
        'q': query,
        'format': 'json',
        'categories': 'general',
        'pageno': 1,
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
    }

    try:
        _rate_limit()
        resp = requests.get(url + '/search', params=params, headers=headers,
                            timeout=_REQUEST_TIMEOUT, allow_redirects=False)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get('results', [])[:count]:
            results.append({
                'url': r.get('url', ''),
                'title': r.get('title', ''),
                'snippet': r.get('content', '') or r.get('description', ''),
            })
        _searxng_available = True
        return results
    except Exception as exc:
        logger.warning('SearXNG search failed for %r: %s', query, exc)
        _searxng_available = False
        _searxng_fail_time = time.time()
        return []


def _brave_search(query: str, count: int = 10) -> list[dict]:
    """Brave API fallback. Returns list of {url, title, snippet}."""
    try:
        from vcoutreach.web.config import ENABLE_BRAVE_FALLBACK, BRAVE_API_KEY
    except ImportError:
        ENABLE_BRAVE_FALLBACK = os.environ.get('ENABLE_BRAVE_FALLBACK', '0') == '1'
        BRAVE_API_KEY = os.environ.get('BRAVE_API_KEY', '')

    BRAVE_API_KEY = setting_value("BRAVE_API_KEY", BRAVE_API_KEY)
    ENABLE_BRAVE_FALLBACK = setting_value("ENABLE_BRAVE_FALLBACK", "1" if BRAVE_API_KEY else "0") == "1"
    if not ENABLE_BRAVE_FALLBACK:
        logger.debug('Brave fallback disabled  -  ENABLE_BRAVE_FALLBACK not set')
        return []

    if not BRAVE_API_KEY:
        logger.warning('Brave fallback enabled but BRAVE_API_KEY not set')
        return []

    try:
        requests = require_dependency("requests")
    except ImportError as exc:
        logger.warning("%s", exc)
        return []

    headers = {
        'Accept': 'application/json',
        'X-Subscription-Token': BRAVE_API_KEY,
    }
    params = {'q': query, 'count': count}

    try:
        _rate_limit()
        resp = requests.get(_BRAVE_WEB_ENDPOINT, headers=headers, params=params,
                            timeout=_REQUEST_TIMEOUT, allow_redirects=False)
        if resp.status_code in (429, 402):
            logger.warning('Brave API quota exhausted (HTTP %s)', resp.status_code)
            return []
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get('web', {}).get('results', []):
            results.append({
                'url': r.get('url', ''),
                'title': r.get('title', ''),
                'snippet': r.get('description', ''),
            })
        return results
    except Exception as exc:
        logger.warning('Brave search failed for %r: %s', query, exc)
        return []


def search(query: str, count: int = 10, force_refresh: bool = False) -> list[dict]:
    """
    Search with caching. Returns list of {url, title, snippet} dicts.

    Tries SearXNG first; falls back to Brave if enabled; returns [] if both fail.
    Caches by normalized query for 7 days.
    """
    db_path = _get_db_path()
    query_key = _normalize(query)

    # Cache check
    if not force_refresh:
        cached = _cache_get(query_key, db_path)
        if cached is not None:
            logger.debug('Cache hit for: %r', query[:60])
            return cached

    # Try SearXNG
    results = _searxng_search(query, count)

    # Brave fallback
    if not results:
        results = _brave_search(query, count)

    if results:
        _cache_set(query_key, results, db_path)
    else:
        logger.warning('No results for query: %r (SearXNG=%s)', query[:80], _searxng_available)

    return results


def search_partner(name: str, firm: str, count_per_query: int = 10) -> list[dict]:
    """
    Run the 4-query enrichment set for a VC partner.

    Queries:
      1. "{name}" "{firm}" thesis
      2. "{name}" essay site:medium.com OR site:substack.com
      3. "{name}" portfolio investment
      4. "{name}" podcast

    Returns deduplicated list of {url, title, snippet} dicts.
    """
    queries = [
        f'"{name}" "{firm}" thesis',
        f'"{name}" essay site:medium.com OR site:substack.com',
        f'"{name}" portfolio investment',
        f'"{name}" podcast',
    ]

    seen_urls: set[str] = set()
    all_results: list[dict] = []

    for q in queries:
        results = search(q, count=count_per_query)
        for r in results:
            url = r.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                # Tag which query produced this result
                r['_query'] = q
                all_results.append(r)
        logger.info('Query: %r -> %d results', q[:60], len(results))

    return all_results


# ---------------------------------------------------------------------------
# Partner-type-aware enrichment
# ---------------------------------------------------------------------------

# Junk domains that return boilerplate previews (LinkedIn gating, Crunchbase
# one-liners, PitchBook paywalls)  -  filtered out before returning results.
_JUNK_DOMAINS = frozenset({
    'linkedin.com', 'crunchbase.com', 'pitchbook.com',
    'zoominfo.com', 'rocketreach.com', 'owler.com',
    'dnb.com', 'spokeo.com',
})

# Firms that are "non-firms"  -  solo angels, independents. When firm matches
# one of these tokens we skip firm-paired queries (they return 0 results).
_SOLO_FIRM_TOKENS = frozenset({
    'solo', 'angel', 'independent', 'self', 'individual', 'personal',
    'freelance', 'none', '',
})

# Known active-writer signals: substack subdomains, personal blog domains, etc.
_WRITER_FIRM_PATTERNS = (
    'newsletter', 'substack', 'blog', 'media', 'fund', 'capital',
    'ventures', 'partners', 'vc', 'venture',
)

# Known operator/builder signals in firm name or notes
_OPERATOR_SIGNALS = (
    'product', 'growth', 'operator', 'builder', 'studio',
)


def _detect_archetype(partner: dict) -> str:
    """
    Infer partner archetype from available fields.

    Archetypes:
      active_writer    -  prolific blogger/newsletter author (Substack, Medium)
      operator_vc      -  founded/sold a startup before investing
      journalist_vc    -  came from press/media background
      low_profile      -  little public writing, sparse web presence
      default          -  fallback

    Detection uses: firm, notes, source, partner_name.
    No bio field exists in schema  -  detection is heuristic only.
    """
    name = (partner.get('partner_name') or partner.get('name') or '').lower()
    firm = (partner.get('firm') or '').lower()
    notes = (partner.get('notes') or '').lower()
    source = (partner.get('source') or '').lower()

    # Explicit override via partner dict (caller can set 'archetype' key)
    if partner.get('archetype'):
        return partner['archetype']

    # Active writer check FIRST  -  newsletter/substack/blog signals are stronger
    # than generic 'writer' which could appear in journalist OR writer-VC notes.
    if any(kw in firm for kw in ('newsletter', 'substack', 'blog')):
        return 'active_writer'
    if any(kw in notes for kw in ('newsletter', 'substack', 'writes on substack', 'writes on medium', 'essay', 'blogger')):
        return 'active_writer'

    # Journalist signals: former press background (use specific press terms, not generic 'writer')
    if any(kw in notes for kw in ('journalist', 'reporter', 'techcrunch', 'editor', 'press')):
        return 'journalist_vc'
    if any(kw in firm for kw in ('media', 'press')):
        return 'journalist_vc'

    # Operator signals: built/sold/founded in notes
    if any(kw in notes for kw in ('built', 'sold', 'founded', 'co-founder', 'cofounder', 'ceo', 'cto', 'product head')):
        return 'operator_vc'
    if any(kw in firm for kw in _OPERATOR_SIGNALS):
        return 'operator_vc'

    # Broader active writer: general writing signals after specific press signals are ruled out
    if any(kw in notes for kw in ('writes', 'prolific writer', 'blogger')):
        return 'active_writer'

    # Low profile: firm is generic/solo or notes indicate low presence
    firm_words = set(firm.replace('-', ' ').split())
    if firm_words & _SOLO_FIRM_TOKENS or not firm.strip():
        return 'low_profile'
    if any(kw in notes for kw in ('low profile', 'no public', 'private')):
        return 'low_profile'

    return 'default'


def _is_real_firm(firm: str) -> bool:
    """Return True if the firm name is a real VC fund (not solo/independent)."""
    if not firm or not firm.strip():
        return False
    firm_lower = firm.lower().strip()
    # Split into tokens and check if any token is a junk token
    tokens = set(firm_lower.replace('-', ' ').split())
    if tokens & _SOLO_FIRM_TOKENS:
        return False
    # Must have at least 2 chars (not just whitespace)
    return len(firm.strip()) > 2


def _build_queries(partner: dict, archetype: str) -> list[str]:
    """
    Build ordered list of search queries for this partner.

    Returns 5 queries ordered by expected yield for the archetype.
    Skips firm-paired queries when firm is not a real VC fund.
    """
    name = (partner.get('partner_name') or partner.get('name') or '').strip()
    firm = (partner.get('firm') or '').strip()
    has_real_firm = _is_real_firm(firm)

    # Base queries (ordered by quality from investigation)
    q_substack = f'"{name}" substack OR medium essay'
    q_philosophy = f'"{name}" investment philosophy OR thesis'
    q_firm_thesis = f'"{name}" "{firm}" thesis' if has_real_firm else None
    q_podcast = f'"{name}" podcast appearance'
    q_portfolio = f'"{name}" "invested in" OR "led" portfolio company'
    q_built = f'"{name}" built OR founded OR sold startup'
    q_recent = f'"{name}" recent investment 2024 OR 2025'
    q_firm_interview = f'"{name}" "{firm}" interview' if has_real_firm else None

    archetype_queries: dict[str, list] = {
        'active_writer': [
            q_substack,
            q_philosophy,
            q_firm_thesis,
            q_podcast,
            q_portfolio,
        ],
        'operator_vc': [
            q_substack,
            q_built,
            q_podcast,
            q_philosophy,
            q_portfolio,
        ],
        'journalist_vc': [
            q_substack,
            q_podcast,
            q_firm_thesis,
            q_portfolio,
            q_philosophy,
        ],
        'low_profile': [
            q_firm_thesis,
            q_portfolio,
            q_substack,
            q_recent,
            q_philosophy,
        ],
        'default': [
            q_substack,
            q_philosophy,
            q_firm_thesis,
            q_podcast,
            q_portfolio,
        ],
    }

    selected = archetype_queries.get(archetype, archetype_queries['default'])
    # Remove None entries (skipped queries) and deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for q in selected:
        if q and q not in seen:
            seen.add(q)
            result.append(q)
    return result


def _filter_junk(results: list[dict]) -> list[dict]:
    """Remove results whose URL is from a known junk domain."""
    filtered = []
    for r in results:
        url = r.get('url', '').lower()
        domain = url.split('/')[2] if url.startswith('http') else ''
        # Strip www. prefix for matching
        domain = domain.removeprefix('www.')
        if not any(domain == j or domain.endswith('.' + j) for j in _JUNK_DOMAINS):
            filtered.append(r)
    return filtered


_ENRICH_RATE_LIMIT = 1.5  # seconds between queries in enrich_partner_web


def enrich_partner_web(partner: dict, count_per_query: int = 7) -> list[dict]:
    """
    Run archetype-aware web enrichment for a VC partner.

    Parameters
    ----------
    partner : dict
        Must contain at least 'partner_name' (or 'name') and 'firm'.
        Optional keys used for archetype detection: 'notes', 'source', 'archetype'.
        If 'archetype' is set explicitly (e.g. 'active_writer'), it overrides detection.
    count_per_query : int
        Results requested per query (default 7 to stay within cache/budget).

    Returns
    -------
    list[dict]
        Deduplicated list of {url, title, snippet, _query, _archetype} dicts.
        Junk-domain results (LinkedIn, Crunchbase, PitchBook, etc.) are filtered.
        Results are cached via the standard web_search_cache table.

    Notes
    -----
    - Throttled at 1.5 s between queries (polite to SearXNG/Brave).
    - Existing search() cache is used  -  re-runs are free.
    - Does NOT modify the database schema (web_search_cache already exists).
    """
    global _last_request_time

    archetype = _detect_archetype(partner)
    queries = _build_queries(partner, archetype)
    name = (partner.get('partner_name') or partner.get('name') or '').strip()

    logger.info(
        'enrich_partner_web: %r archetype=%r queries=%d',
        name, archetype, len(queries)
    )

    seen_urls: set[str] = set()
    all_results: list[dict] = []

    for q in queries:
        # Enforce 1.5s inter-query throttle (search() already has 1.0s via _rate_limit)
        # We add an extra 0.5s delay here before calling search()
        elapsed = time.time() - _last_request_time
        if elapsed < _ENRICH_RATE_LIMIT:
            time.sleep(_ENRICH_RATE_LIMIT - elapsed)

        results = search(q, count=count_per_query)
        results = _filter_junk(results)

        for r in results:
            url = r.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                r['_query'] = q
                r['_archetype'] = archetype
                all_results.append(r)

        logger.info(
            'enrich_partner_web: query=%r -> %d results (%d after junk filter)',
            q[:70], count_per_query, len([r for r in results if r.get('url', '') in seen_urls])
        )

    logger.info(
        'enrich_partner_web: %r done  -  %d total unique results (archetype=%s)',
        name, len(all_results), archetype
    )
    return all_results
