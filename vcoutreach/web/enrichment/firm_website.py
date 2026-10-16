"""
VC firm website scraper  -  highest-ROI free enrichment source.

Scrapes a VC firm's own website (/team, /portfolio, /about, /thesis pages)
for partner bios, fund thesis, and recent portfolio companies.

Tech stack:
  - requests + BeautifulSoup for static pages (fast, no overhead)
  - Playwright headless fallback for JS-heavy SPAs
  - Rate-limited to 1 request per 2 seconds per domain
  - 10-second page timeout; all exceptions caught, returns empty dict

Does NOT import from orchestrator.py or any other coupled module.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from vcoutreach.web.vendor import validated_url, public_requests_get, require_dependency
from typing import Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

# ── Rate-limiting ─────────────────────────────────────────────────────────────
_RATE_LIMIT_INTERVAL = 2.0          # seconds between requests per domain
_PAGE_TIMEOUT = 10                  # seconds for requests
_PLAYWRIGHT_TIMEOUT = 15_000        # ms for Playwright
_last_request_by_domain: dict[str, float] = {}

# ── SPA detection threshold ───────────────────────────────────────────────────
_MIN_STATIC_CHARS = 300             # body text below this → assume SPA

# ── Known VC firm homepage mappings (firm name → domain) ─────────────────────
# User-provided workspace configuration.
# Keys are lowercase, spaces stripped; add more as needed.
_KNOWN_FIRM_DOMAINS = {}

# ── Team/people page keyword patterns ────────────────────────────────────────
_TEAM_KEYWORDS = [
    'team', 'people', 'partners', 'who we are', 'our team',
    'investors', 'meet the team', 'meet our team', 'the team',
    'our people', 'our partners', 'crew', 'staff',
]

# ── Portfolio page keyword patterns ──────────────────────────────────────────
_PORTFOLIO_KEYWORDS = [
    'portfolio', 'companies', 'investments', 'our work',
    'our portfolio', 'invested', 'our investments', 'portfolio companies',
    'startups', 'founders',
]

# ── Thesis/about page paths to probe ─────────────────────────────────────────
_THESIS_PATHS = [
    '/about', '/thesis', '/philosophy', '/manifesto',
    '/approach', '/our-approach', '/mission', '/values',
    '/investment-thesis', '/what-we-believe',
]

# ── Headers for requests ──────────────────────────────────────────────────────
_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _rate_limit(url: str) -> None:
    """Block until 2 seconds have passed since last request to this domain."""
    netloc = urlparse(url).netloc
    now = time.time()
    elapsed = now - _last_request_by_domain.get(netloc, 0.0)
    if elapsed < _RATE_LIMIT_INTERVAL:
        time.sleep(_RATE_LIMIT_INTERVAL - elapsed)
    _last_request_by_domain[netloc] = time.time()


def _fetch_static(url: str) -> Optional[str]:
    """
    Fetch a URL with requests. Returns HTML string or None on error.
    Respects per-domain rate limit.
    """
    try:
        requests = require_dependency("requests")
        _rate_limit(url)
        resp = public_requests_get(url, headers=_HEADERS, timeout=_PAGE_TIMEOUT,
                            allow_redirects=False)
        if resp.status_code == 403:
            logger.warning('HTTP 403 from %s  -  site is blocking scrapers', url)
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        logger.warning('Static fetch failed for %s: %s', url, exc)
        return None


def _is_spa(html: str) -> bool:
    """
    Return True if the page is likely a JavaScript SPA (body mostly empty).
    Heuristic: strip <script>/<style> from body; if remaining text < 300 chars
    OR a framework root div (#root, #app, #__next) is present with no body text.
    """
    try:
        BeautifulSoup = require_dependency("bs4").BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')

        # Check for common SPA root markers
        spa_markers = soup.find_all('div', id=re.compile(r'^(root|app|__next|main|gatsby-focus-wrapper)$'))
        if spa_markers:
            body = soup.find('body')
            if body:
                body_text = body.get_text(separator=' ', strip=True)
                if len(body_text) < _MIN_STATIC_CHARS:
                    return True

        # Remove script and style tags, measure remaining text
        body = soup.find('body')
        if not body:
            return False
        for tag in body.find_all(['script', 'style', 'noscript']):
            tag.decompose()
        remaining = body.get_text(separator=' ', strip=True)
        return len(remaining) < _MIN_STATIC_CHARS

    except Exception:
        return False


def _fetch_with_playwright(url: str) -> Optional[str]:
    """
    Fetch a URL using a fresh, ephemeral Playwright headless browser.
    Never shares the LinkedIn profile dir. Returns rendered HTML or None.
    """
    validated_url(url)
    try:
        from playwright.sync_api import sync_playwright

        _rate_limit(url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_HEADERS['User-Agent'],
                viewport={'width': 1280, 'height': 800},
            )
            page = ctx.new_page()
            try:
                page.goto(url, timeout=_PLAYWRIGHT_TIMEOUT, wait_until='networkidle')
                # Give JS frameworks a moment to render
                time.sleep(1.5)
                html = page.content()
            except Exception as exc:
                logger.warning('Playwright navigation failed for %s: %s', url, exc)
                html = None
            finally:
                ctx.close()
                browser.close()
        return html
    except ImportError:
        logger.warning('playwright not installed  -  cannot scrape JS-heavy pages')
        return None
    except Exception as exc:
        logger.warning('Playwright fetch failed for %s: %s', url, exc)
        return None


def _fetch(url: str) -> Optional[str]:
    """
    Smart fetch: try requests first; if page is a SPA, fall back to Playwright.
    Returns rendered HTML or None.
    """
    html = _fetch_static(url)
    if html is None:
        return None
    if _is_spa(html):
        logger.info('SPA detected at %s  -  falling back to Playwright', url)
        pw_html = _fetch_with_playwright(url)
        return pw_html if pw_html else html  # return static as last resort
    return html


def _parse_soup(html: str):
    """Return BeautifulSoup object from HTML string."""
    BeautifulSoup = require_dependency("bs4").BeautifulSoup
    return BeautifulSoup(html, 'html.parser')


def _normalize_url(href: str, base_url: str) -> str:
    """Resolve relative href to absolute URL using base."""
    return urljoin(base_url, href)


def _same_domain(url: str, homepage: str) -> bool:
    """Return True if url is on the same domain as homepage."""
    hp_netloc = urlparse(homepage).netloc.lower().replace('www.', '')
    u_netloc = urlparse(url).netloc.lower().replace('www.', '')
    return hp_netloc == u_netloc or u_netloc.endswith('.' + hp_netloc)


def _find_nav_link(soup, keywords: list[str], homepage_url: str) -> Optional[str]:
    """
    Scan all <a> tags for a text/href match against keywords.
    Prefers same-domain links. Returns absolute URL or None.
    """
    candidates: list[tuple[int, str]] = []  # (priority, url)

    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        if not href or href.startswith('#') or href.startswith('mailto:'):
            continue

        text = anchor.get_text(separator=' ', strip=True).lower()
        href_lower = href.lower()

        # Score: exact keyword in link text > keyword in href path
        for i, kw in enumerate(keywords):
            if kw in text:
                abs_url = _normalize_url(href, homepage_url)
                priority = i  # lower index = higher priority keyword
                if _same_domain(abs_url, homepage_url):
                    priority -= 100  # heavily prefer same-domain
                candidates.append((priority, abs_url))
                break
            elif '/' + kw.replace(' ', '-') in href_lower or '/' + kw.replace(' ', '_') in href_lower:
                abs_url = _normalize_url(href, homepage_url)
                priority = i + len(keywords)  # lower priority than text match
                if _same_domain(abs_url, homepage_url):
                    priority -= 50
                candidates.append((priority, abs_url))
                break

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# ── Public API ────────────────────────────────────────────────────────────────

def find_firm_website(firm_name: str, fallback_search: bool = True) -> Optional[str]:
    """
    Try to derive the firm's homepage URL.

    Strategy:
    1. Lookup in the known-firms dict (lowercase firm name).
    2. If fallback_search=True and no match: use web_search to query
       "{firm} VC official website" and return top non-social result.

    Returns the homepage URL (with https://) or None.
    """
    if not firm_name:
        return None

    key = firm_name.lower().strip()
    domain = _KNOWN_FIRM_DOMAINS.get(key)

    # Try partial match (firm name contains a known key or vice versa)
    if not domain:
        for k, d in _KNOWN_FIRM_DOMAINS.items():
            if k in key or key in k:
                domain = d
                break

    if domain:
        return f'https://{domain}'

    if not fallback_search:
        return None

    # Fallback: use web_search
    try:
        from vcoutreach.web.enrichment.web_search import search

        query = f'"{firm_name}" venture capital official website'
        results = search(query, count=10)

        _skip_domains = {
            'twitter.com', 'x.com', 'linkedin.com', 'wikipedia.org',
            'crunchbase.com', 'pitchbook.com', 'techcrunch.com',
            'bloomberg.com', 'forbes.com', 'facebook.com',
        }

        for r in results:
            url = r.get('url', '')
            if not url:
                continue
            netloc = urlparse(url).netloc.lower().replace('www.', '')
            if any(skip in netloc for skip in _skip_domains):
                continue
            # Return the root of this domain
            parsed = urlparse(url)
            homepage = f'{parsed.scheme}://{parsed.netloc}'
            logger.info('find_firm_website: fallback found %s for %r', homepage, firm_name)
            return homepage

    except Exception as exc:
        logger.warning('find_firm_website fallback search failed: %s', exc)

    return None


def discover_team_url(homepage_url: str) -> Optional[str]:
    """
    From the homepage, find the team/people/about page URL.
    Loads homepage, scans anchors for team-related keywords.
    Returns absolute URL or None.
    """
    if not homepage_url:
        return None

    html = _fetch(homepage_url)
    if not html:
        return None

    try:
        soup = _parse_soup(html)
        url = _find_nav_link(soup, _TEAM_KEYWORDS, homepage_url)
        if url:
            logger.info('discover_team_url: found %s', url)
        return url
    except Exception as exc:
        logger.warning('discover_team_url failed for %s: %s', homepage_url, exc)
        return None


def discover_portfolio_url(homepage_url: str) -> Optional[str]:
    """
    From the homepage, find the portfolio/companies/investments page URL.
    Returns absolute URL or None.
    """
    if not homepage_url:
        return None

    html = _fetch(homepage_url)
    if not html:
        return None

    try:
        soup = _parse_soup(html)
        url = _find_nav_link(soup, _PORTFOLIO_KEYWORDS, homepage_url)
        if url:
            logger.info('discover_portfolio_url: found %s', url)
        return url
    except Exception as exc:
        logger.warning('discover_portfolio_url failed for %s: %s', homepage_url, exc)
        return None


def scrape_team_page(team_url: str) -> dict:
    """Workspace-backed outreach integration."""
    empty = {'partners': [], 'raw_text_excerpt': ''}

    if not team_url:
        return empty

    html = _fetch(team_url)
    if not html:
        return empty

    try:
        BeautifulSoup = require_dependency("bs4").BeautifulSoup

        soup = BeautifulSoup(html, 'html.parser')
        raw_text = soup.get_text(separator='\n', strip=True)[:4000]

        partners = _extract_team_cards(soup, team_url)

        # If partners have no bios, try following individual profile links
        partners_without_bio = [p for p in partners if not p.get('bio')]
        if partners_without_bio and len(partners_without_bio) == len(partners):
            # No bios anywhere  -  try to find individual profile subpages
            partners = _enrich_partners_via_profile_pages(soup, partners, team_url)

        return {
            'partners': partners,
            'raw_text_excerpt': raw_text,
        }

    except Exception as exc:
        logger.warning('scrape_team_page failed for %s: %s', team_url, exc)
        return empty


def _enrich_partners_via_profile_pages(
    soup,
    partners: list[dict],
    team_url: str,
) -> list[dict]:
    """
    For each partner card that lacks a bio, check if there's an individual
    profile page linked from the team listing. Fetch up to 5 individual pages.
    Uses the partner name to locate their profile link on the team page.
    """
    base_parsed = urlparse(team_url)
    base_path = base_parsed.path.rstrip('/')  # e.g. /team

    # Collect all same-domain links that look like profile subpages
    # e.g. /team/mike-maples-jr, /people/charles-hudson
    profile_links: dict[str, str] = {}  # slug → absolute URL
    for anchor in soup.find_all('a', href=True):
        href = anchor['href'].strip()
        abs_url = _normalize_url(href, team_url)
        if not _same_domain(abs_url, team_url):
            continue
        path = urlparse(abs_url).path
        # Must be a sub-path of team page (e.g. /team/mike-maples-jr)
        if path.startswith(base_path + '/') and path != base_path + '/':
            slug = path[len(base_path) + 1:].strip('/')
            if slug and '/' not in slug:  # single-level subpath only
                profile_links[slug] = abs_url

    if not profile_links:
        return partners

    logger.info('Found %d individual profile links under %s', len(profile_links), team_url)

    # For each partner missing a bio, try to match to a profile link
    enriched = list(partners)
    pages_fetched = 0

    for i, partner in enumerate(enriched):
        if partner.get('bio'):
            continue
        if pages_fetched >= 5:  # cap individual page fetches
            break

        name = partner.get('name', '')
        # User-provided workspace configuration.
        slug_guess = re.sub(r'[^a-z0-9]+', '-', name.lower().strip()).strip('-')
        # Also try just last name
        name_parts = name.split()
        last_slug = name_parts[-1].lower().strip('.,') if name_parts else ''

        # Find matching profile URL
        matched_url = None
        for slug, url in profile_links.items():
            slug_norm = slug.lower()
            if slug_norm == slug_guess or slug_norm.startswith(last_slug + '-') or slug_norm.endswith('-' + last_slug) or slug_norm == last_slug:
                matched_url = url
                break

        if not matched_url:
            continue

        # Fetch the individual profile page
        profile_html = _fetch(matched_url)
        pages_fetched += 1

        if not profile_html:
            continue

        try:
            BS4 = require_dependency("bs4").BeautifulSoup
            psoup = BS4(profile_html, 'html.parser')
            for tag in psoup(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()

            # Extract the longest paragraph-like block as bio
            best_bio = ''
            for p in psoup.find_all(['p', 'div']):
                text = p.get_text(separator=' ', strip=True)
                # Must be substantive and not look like nav text
                if len(text) > len(best_bio) and len(text) > 60:
                    # Exclude very long concatenated-junk text
                    if len(text) < 3000:
                        best_bio = text

            if best_bio and len(best_bio) > 60:
                enriched[i] = dict(partner)
                enriched[i]['bio'] = best_bio[:600]
                logger.info('Enriched %s bio from profile page: %s', name, matched_url)

            # Also look for LinkedIn link on profile page
            if not enriched[i].get('linkedin_url'):
                li_link = psoup.find('a', href=re.compile(r'linkedin\.com/in/'))
                if li_link:
                    enriched[i]['linkedin_url'] = li_link['href']

        except Exception as exc:
            logger.warning('Failed to parse profile page %s: %s', matched_url, exc)

    return enriched


def _extract_team_cards(soup, base_url: str) -> list[dict]:
    """
    Heuristic extraction of partner cards from team page soup.
    Multiple strategies attempted in order of signal strength.
    """
    partners: list[dict] = []
    seen_names: set[str] = set()

    # ── Strategy 1: look for structured card containers ───────────────────────
    # Common patterns: div.team-member, div.person, article.partner, li.member
    card_selectors = [
        '[class*="team-member"]', '[class*="person"]', '[class*="partner"]',
        '[class*="member"]', '[class*="people"]', '[class*="investor"]',
        '[class*="founder"]', '[class*="staff"]', '[class*="bio"]',
        '[class*="card"]', 'article',
    ]

    for selector in card_selectors:
        try:
            cards = soup.select(selector)
            if len(cards) < 2:
                continue

            for card in cards:
                p = _parse_card(card, base_url)
                if p and p['name'] and p['name'] not in seen_names:
                    seen_names.add(p['name'])
                    partners.append(p)

            if len(partners) >= 2:
                logger.debug('Team extraction: found %d partners via %s', len(partners), selector)
                return partners[:30]  # cap at 30 team members
        except Exception:
            continue

    # ── Strategy 2: scan all h2/h3/h4 tags as potential name headings ────────
    if len(partners) < 2:
        partners = []
        seen_names = set()
        heading_tags = soup.find_all(['h2', 'h3', 'h4'])

        for heading in heading_tags:
            name_text = heading.get_text(strip=True)
            # Plausible name: 2-4 words, title-cased, no special chars
            if not _looks_like_name(name_text):
                continue
            if name_text in seen_names:
                continue

            # Look for title and bio in siblings or parent's children
            title = ''
            bio = ''
            linkedin = None
            photo = None

            # Check next siblings
            for sibling in heading.next_siblings:
                if hasattr(sibling, 'name'):
                    if sibling.name in ('p', 'span', 'div'):
                        sibling_text = sibling.get_text(strip=True)
                        if not title and sibling_text and len(sibling_text) < 100:
                            title = sibling_text
                        elif not bio and sibling_text and len(sibling_text) > 30:
                            bio = sibling_text[:500]
                        link = sibling.find('a', href=re.compile(r'linkedin\.com/in/'))
                        if link and not linkedin:
                            linkedin = link['href']
                    elif sibling.name in ('h2', 'h3', 'h4'):
                        break  # stop at next heading

            seen_names.add(name_text)
            partners.append({
                'name': name_text,
                'title': title,
                'bio': bio,
                'linkedin_url': linkedin,
                'photo_url': photo,
            })

        logger.debug('Team extraction: found %d partners via heading scan', len(partners))

    return partners[:30]


def _parse_card(card, base_url: str) -> Optional[dict]:
    """Parse a single team member card element."""
    try:
        # Name: prefer heading tags inside card
        name = ''
        for tag in ['h1', 'h2', 'h3', 'h4', 'h5', '[class*="name"]', '[class*="title"]']:
            el = card.select_one(tag)
            if el:
                candidate = el.get_text(strip=True)
                if _looks_like_name(candidate):
                    name = candidate
                    break

        if not name:
            return None

        # Title: look for role/position element (usually a <p> or <span> right after name)
        title = ''
        title_selectors = [
            '[class*="title"]', '[class*="role"]', '[class*="position"]',
            '[class*="job"]', 'p:first-of-type', 'span',
        ]
        for sel in title_selectors:
            el = card.select_one(sel)
            if el:
                candidate = el.get_text(strip=True)
                # Title: short, not a name, not bio-length
                if candidate and 5 < len(candidate) < 120 and not _looks_like_name(candidate):
                    title = candidate
                    break

        # Bio: longest paragraph in card
        bio = ''
        for p in card.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > len(bio) and len(text) > 30:
                bio = text[:500]

        # LinkedIn URL
        linkedin = None
        li_link = card.find('a', href=re.compile(r'linkedin\.com/in/'))
        if li_link:
            linkedin = li_link['href']

        # Photo URL
        photo = None
        img = card.find('img')
        if img and img.get('src'):
            photo = _normalize_url(img['src'], base_url)

        return {
            'name': name,
            'title': title,
            'bio': bio,
            'linkedin_url': linkedin,
            'photo_url': photo,
        }

    except Exception:
        return None


def _looks_like_name(text: str) -> bool:
    """
    Heuristic: is this text a plausible person name?
    2-4 words, mostly title-cased letters, no digits.
    """
    if not text:
        return False
    words = text.strip().split()
    if len(words) < 2 or len(words) > 5:
        return False
    if any(ch.isdigit() for ch in text):
        return False
    if len(text) > 60:
        return False
    # At least half the words should start with a capital
    cap_count = sum(1 for w in words if w and w[0].isupper())
    return cap_count >= len(words) // 2 + 1


def scrape_portfolio_page(portfolio_url: str) -> dict:
    """
    Extract portfolio companies from a portfolio page.

    Returns:
      {
        'companies': [{'name', 'url', 'sector', 'description'}],
        'raw_text_excerpt': str
      }
    """
    empty = {'companies': [], 'raw_text_excerpt': ''}

    if not portfolio_url:
        return empty

    html = _fetch(portfolio_url)
    if not html:
        return empty

    try:
        BeautifulSoup = require_dependency("bs4").BeautifulSoup

        soup = BeautifulSoup(html, 'html.parser')
        raw_text = soup.get_text(separator='\n', strip=True)[:4000]
        companies = _extract_portfolio_companies(soup, portfolio_url)

        return {
            'companies': companies,
            'raw_text_excerpt': raw_text,
        }

    except Exception as exc:
        logger.warning('scrape_portfolio_page failed for %s: %s', portfolio_url, exc)
        return empty


def _extract_portfolio_companies(soup, base_url: str) -> list[dict]:
    """
    Extract company entries from a portfolio page.
    Multiple strategies: card-based, logo grids, list items.
    """
    companies: list[dict] = []
    seen: set[str] = set()

    # ── Strategy 1: named card/grid containers ────────────────────────────────
    card_selectors = [
        '[class*="portfolio"]', '[class*="company"]', '[class*="investment"]',
        '[class*="startup"]', '[class*="logo"]', '[class*="grid"] > *',
        '[class*="card"]', 'li',
    ]

    for selector in card_selectors:
        try:
            cards = soup.select(selector)
            if len(cards) < 3:
                continue

            batch: list[dict] = []
            for card in cards:
                co = _parse_company_card(card, base_url)
                if co and co['name'] and co['name'] not in seen:
                    seen.add(co['name'])
                    batch.append(co)

            if len(batch) >= 3:
                logger.debug('Portfolio: found %d companies via %s', len(batch), selector)
                return batch[:50]
        except Exception:
            continue

    # ── Strategy 2: <a> tags that look like company links ────────────────────
    for anchor in soup.find_all('a', href=True):
        href = anchor['href']
        text = anchor.get_text(strip=True)
        if not text or len(text) > 60 or len(text) < 2:
            continue
        # Looks like a company link: external link, text is short proper name
        if href.startswith('http') and _same_domain(href, base_url) is False:
            if text not in seen and not _is_nav_text(text):
                seen.add(text)
                companies.append({
                    'name': text,
                    'url': href,
                    'sector': None,
                    'description': None,
                })

    return companies[:50]


def _parse_company_card(card, base_url: str) -> Optional[dict]:
    """Parse a single portfolio company card."""
    try:
        name = ''
        # Try heading tags first
        for tag in ['h2', 'h3', 'h4', 'h5', 'strong', '[class*="name"]', 'a', 'img']:
            el = card.select_one(tag)
            if el:
                if tag == 'img':
                    candidate = el.get('alt', '') or el.get('title', '')
                else:
                    candidate = el.get_text(strip=True)
                if candidate and 1 < len(candidate) < 80 and not _is_nav_text(candidate):
                    name = candidate
                    break

        if not name:
            # Try alt text on images
            img = card.find('img')
            if img:
                name = img.get('alt', '') or img.get('title', '')

        if not name or _is_nav_text(name):
            return None

        # URL: find first external link
        co_url = None
        link = card.find('a', href=True)
        if link:
            href = link['href']
            abs_href = _normalize_url(href, base_url)
            co_url = abs_href

        # Description: first <p> with some substance
        description = None
        for p in card.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 20:
                description = text[:200]
                break

        # Sector: look for tag/badge elements
        sector = None
        for sel in ['[class*="sector"]', '[class*="tag"]', '[class*="category"]',
                    '[class*="badge"]', '[class*="label"]']:
            el = card.select_one(sel)
            if el:
                sector = el.get_text(strip=True)[:80]
                break

        return {
            'name': name,
            'url': co_url,
            'sector': sector,
            'description': description,
        }

    except Exception:
        return None


def _clean_excerpt(text: str, max_len: int = 500) -> str:
    """
    Trim a text excerpt so it starts at a sentence boundary and ends cleanly.

    Strategy:
    1. If the text starts mid-word (no leading capital after stripping),
       find the next '. ' or '. ' boundary and start from there.
    2. Otherwise, walk forward to the first capital letter (within first 80 chars).
    3. Cap at max_len, ending at a sentence boundary if possible.
    """
    if not text:
        return text

    text = text.strip()

    # Check if text starts mid-sentence (first word doesn't start with capital)
    first_word_end = text.find(' ')
    first_word = text[:first_word_end] if first_word_end > 0 else text[:20]
    first_word_clean = first_word.strip('.,;:()"\'')

    if first_word_clean and not first_word_clean[0].isupper():
        # Text starts mid-sentence: find first '. [Capital]' boundary
        import re as _re
        m = _re.search(r'\.\s+([A-Z])', text)
        if m:
            text = text[m.start() + 2:].strip()
        else:
            # No sentence boundary found  -  find first capital at word start
            for i, ch in enumerate(text[:120]):
                if i > 0 and ch.isupper() and text[i - 1] in (' ', '\n', '\t'):
                    text = text[i:]
                    break
    else:
        # Walk start forward to first capital letter (sentence start)
        for i, ch in enumerate(text[:80]):
            if ch.isupper():
                text = text[i:]
                break

    # Cap length, ending at sentence boundary
    if len(text) > max_len:
        end = text[:max_len].rfind('. ')
        if end > max_len // 2:
            text = text[:end + 1]
        else:
            text = text[:max_len - 3] + '...'

    return text.strip()


def _is_nav_text(text: str) -> bool:
    """Return True if text looks like nav/UI text rather than a company name."""
    nav_words = {
        'home', 'about', 'contact', 'team', 'portfolio', 'blog', 'news',
        'press', 'jobs', 'careers', 'learn more', 'read more', 'view more',
        'see all', 'all companies', 'companies', 'investments', 'login',
        'sign up', 'sign in', 'get started', 'our work', 'apply', 'faq',
        'privacy', 'terms', 'menu', 'search', 'back', 'next', 'prev',
    }
    return text.lower().strip() in nav_words


def scrape_thesis_or_about(homepage_url: str) -> dict:
    """
    Try /about, /thesis, /philosophy and similar pages.
    Extract mission, thesis, and philosophy paragraphs.

    Returns:
      {'thesis_text': str|None, 'mission_text': str|None, 'raw_text_excerpt': str}
    """
    empty = {'thesis_text': None, 'mission_text': None, 'raw_text_excerpt': ''}

    if not homepage_url:
        return empty

    _THESIS_MARKERS_LOW = [
        'thesis', 'we invest in', 'we focus on', 'we back', 'looking for',
        'believe in', 'philosophy', 'mission is', 'our fund', "what we look for",
        "i believe", "the core insight", "we look for", "we support",
        "we partner with", "investment philosophy",
    ]

    _MISSION_MARKERS = [
        'mission', 'purpose', 'vision', 'we exist', 'our goal', 'we help',
        'we support founders', 'we work with', 'our focus',
    ]

    best_thesis: Optional[str] = None
    best_mission: Optional[str] = None
    best_raw: str = ''

    for path in _THESIS_PATHS:
        url = urljoin(homepage_url, path)
        html = _fetch(url)
        if not html:
            continue

        try:
            BeautifulSoup = require_dependency("bs4").BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
            text = soup.get_text(separator=' ', strip=True)

            if not best_raw:
                best_raw = text[:4000]

            text_lower = text.lower()

            # Extract thesis
            if not best_thesis:
                for marker in _THESIS_MARKERS_LOW:
                    idx = text_lower.find(marker)
                    if idx >= 0:
                        # Use a 100-char back-window so sentence boundary finder
                        # has enough context to walk to a capital letter start.
                        raw_excerpt = text[max(0, idx - 100): idx + 500].strip()
                        if len(raw_excerpt) > 40:
                            best_thesis = _clean_excerpt(raw_excerpt, max_len=600)
                            break

            # Extract mission
            if not best_mission:
                for marker in _MISSION_MARKERS:
                    idx = text_lower.find(marker)
                    if idx >= 0:
                        raw_excerpt = text[max(0, idx - 100): idx + 400].strip()
                        if len(raw_excerpt) > 40:
                            best_mission = _clean_excerpt(raw_excerpt, max_len=400)
                            break

            if best_thesis and best_mission:
                break  # found both, stop probing

        except Exception as exc:
            logger.warning('scrape_thesis_or_about: error on %s: %s', url, exc)
            continue

    # Also probe homepage itself for thesis content
    if not best_thesis or not best_mission:
        try:
            html = _fetch(homepage_url)
            if html:
                BeautifulSoup = require_dependency("bs4").BeautifulSoup
                soup = BeautifulSoup(html, 'html.parser')
                text = soup.get_text(separator=' ', strip=True)
                text_lower = text.lower()

                if not best_raw:
                    best_raw = text[:4000]

                if not best_thesis:
                    for marker in _THESIS_MARKERS_LOW:
                        idx = text_lower.find(marker)
                        if idx >= 0:
                            raw_excerpt = text[max(0, idx - 100): idx + 500].strip()
                            if len(raw_excerpt) > 40:
                                best_thesis = _clean_excerpt(raw_excerpt, max_len=600)
                                break

                if not best_mission:
                    for marker in _MISSION_MARKERS:
                        idx = text_lower.find(marker)
                        if idx >= 0:
                            raw_excerpt = text[max(0, idx - 100): idx + 400].strip()
                            if len(raw_excerpt) > 40:
                                best_mission = _clean_excerpt(raw_excerpt, max_len=400)
                                break
        except Exception as exc:
            logger.warning('scrape_thesis_or_about: homepage probe failed: %s', exc)

    return {
        'thesis_text': best_thesis,
        'mission_text': best_mission,
        'raw_text_excerpt': best_raw,
    }


def enrich_firm(
    firm_name: str,
    partner_first: Optional[str] = None,
    partner_last: Optional[str] = None,
) -> dict:
    """
    Public entry point. Runs all scrapers, filters team output to matching
    partner if name given.

    Returns:
      {
        'firm_url': str|None,
        'team_page_url': str|None,
        'portfolio_page_url': str|None,
        'partner_bio': str|None,
        'partner_title': str|None,
        'partner_linkedin': str|None,
        'firm_thesis': str|None,
        'recent_portfolio_companies': [...]  # capped at 10
      }
    """
    result: dict = {
        'firm_url': None,
        'team_page_url': None,
        'portfolio_page_url': None,
        'partner_bio': None,
        'partner_title': None,
        'partner_linkedin': None,
        'firm_thesis': None,
        'recent_portfolio_companies': [],
    }

    # 1. Find homepage
    firm_url = find_firm_website(firm_name)
    if not firm_url:
        logger.warning('Could not find website for firm: %r', firm_name)
        return result
    result['firm_url'] = firm_url

    # 2. Discover team and portfolio page URLs from homepage
    team_url = discover_team_url(firm_url)
    portfolio_url = discover_portfolio_url(firm_url)
    result['team_page_url'] = team_url
    result['portfolio_page_url'] = portfolio_url

    # 3. Scrape team page
    if team_url:
        team_data = scrape_team_page(team_url)
        partners = team_data.get('partners', [])

        if partners and (partner_first or partner_last):
            # Filter to the matching partner
            matched = _find_matching_partner(partners, partner_first, partner_last)
            if matched:
                result['partner_bio'] = matched.get('bio') or None
                result['partner_title'] = matched.get('title') or None
                result['partner_linkedin'] = matched.get('linkedin_url') or None
                logger.info('Found partner %s %s on team page', partner_first, partner_last)
            else:
                logger.info('Partner %s %s not found in %d entries',
                            partner_first, partner_last, len(partners))

    # 4. Scrape portfolio page
    if portfolio_url:
        portfolio_data = scrape_portfolio_page(portfolio_url)
        companies = portfolio_data.get('companies', [])
        result['recent_portfolio_companies'] = companies[:10]

    # 5. Scrape thesis/about
    thesis_data = scrape_thesis_or_about(firm_url)
    thesis = thesis_data.get('thesis_text')
    if not thesis:
        thesis = thesis_data.get('mission_text')
    result['firm_thesis'] = thesis

    return result


def _find_matching_partner(
    partners: list[dict],
    first_name: Optional[str],
    last_name: Optional[str],
) -> Optional[dict]:
    """
    Find the best-matching partner from a list.
    Lenient: match if last_name is in partner name (case-insensitive).
    """
    if not partners:
        return None

    first_l = (first_name or '').lower().strip()
    last_l = (last_name or '').lower().strip()

    # Exact last-name match first
    for p in partners:
        name = (p.get('name') or '').lower()
        if last_l and last_l in name:
            if not first_l or first_l in name:
                return p

    # Partial first-name match fallback
    if first_l:
        for p in partners:
            name = (p.get('name') or '').lower()
            if first_l in name:
                return p

    return None


def to_enrichment_facts(enrich_result: dict, source_url: str) -> list[dict]:
    """
    Convert enrich_firm() result into standard {type, text, source_url, confidence} facts.

    Fact types emitted:
      - 'firm_thesis_quote'   from firm_thesis
      - 'partner_bio'         from partner_bio (capped at 280 chars)
      - 'partner_title'       from partner_title
      - 'portfolio_company'   per recent company (max 10)
    """
    facts: list[dict] = []

    # Thesis
    thesis = enrich_result.get('firm_thesis')
    if thesis and len(thesis.strip()) > 20:
        facts.append({
            'type': 'firm_thesis_quote',
            'text': thesis.strip()[:280],
            'source_url': source_url,
            'confidence': 0.85,
        })

    # Partner bio
    bio = enrich_result.get('partner_bio')
    if bio and len(bio.strip()) > 20:
        facts.append({
            'type': 'partner_bio',
            'text': bio.strip()[:280],
            'source_url': source_url,
            'confidence': 0.88,
        })

    # Partner title
    title = enrich_result.get('partner_title')
    if title and len(title.strip()) > 3:
        facts.append({
            'type': 'partner_title',
            'text': title.strip()[:120],
            'source_url': source_url,
            'confidence': 0.90,
        })

    # Portfolio companies
    companies = enrich_result.get('recent_portfolio_companies') or []
    for co in companies[:10]:
        name = co.get('name', '') if isinstance(co, dict) else str(co)
        sector = co.get('sector', '') if isinstance(co, dict) else ''
        if not name or _is_nav_text(name):
            continue
        sector_note = f'  -  relevant sector: {sector}' if sector else ''
        text = f'{name} is in your portfolio{sector_note}'
        facts.append({
            'type': 'portfolio_company',
            'text': text[:240],
            'source_url': source_url,
            'confidence': 0.75,
        })

    return facts
