"""Workspace-backed outreach integration."""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
# VC emails project root is two levels above this file (app/enrichment/linkedin.py)
_THIS_FILE = Path(__file__).resolve()
from vcoutreach.web.vendor import workspace_root, workspace_path, data_path, validated_url
from vcoutreach.web.config import WorkspacePath
PROJECT_ROOT = WorkspacePath()

PROFILE_DIR = PROJECT_ROOT / 'data' / '.linkedin_profile'
COOKIES_PATH = PROFILE_DIR / 'cookies.json'
STORAGE_FILE = PROJECT_ROOT / 'data' / '.linkedin_storage.json'

# User-provided workspace configuration.

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_PROFILES_PER_SESSION = 25
MAX_PROFILES_PER_HOUR = 15
BREAK_EVERY_N = 6
BREAK_MIN_S = 300.0
BREAK_MAX_S = 720.0

_AUTH_COOKIE_NAMES = {'li_at', 'li_rm', 'JSESSIONID'}
CAPTCHA_MARKERS = (
    'security check', 'unusual activity', 'verify you',
    'are you a robot', 'captcha', 'challenge',
)


class CaptchaHit(Exception):
    """Raised when LinkedIn shows a captcha/challenge page."""


class RateLimitHit(Exception):
    """Raised when we hit self-imposed rate caps."""


# ── Timing helpers ─────────────────────────────────────────────────────────────

def _lognormal_sleep(mean_s: float, sigma: float = 0.4,
                     min_s: float = 0.3, max_s: float = 60.0) -> float:
    dur = random.lognormvariate(mu=max(0.0, mean_s ** 0.5), sigma=sigma)
    dur = max(min_s, min(max_s, dur))
    time.sleep(dur)
    return dur


def _jitter(base: float, spread: float = 0.35) -> float:
    return max(0.05, base + random.uniform(-spread, spread) * base)


# ── Config dataclass ───────────────────────────────────────────────────────────

@dataclass
class HumanConfig:
    max_profiles_per_session: int = MAX_PROFILES_PER_SESSION
    max_profiles_per_hour: int = MAX_PROFILES_PER_HOUR
    break_every_n_profiles: int = BREAK_EVERY_N
    break_min_s: float = BREAK_MIN_S
    break_max_s: float = BREAK_MAX_S
    distraction_chance: float = 0.35
    page_dwell_mean_s: float = 18.0
    scroll_steps_mean: int = 6
    headless: bool = False


# ── Profile management ─────────────────────────────────────────────────────────

def ensure_profile_dir() -> bool:
    """Workspace-backed outreach integration."""
    target_default = PROFILE_DIR / 'Default'

    if target_default.exists():
        logger.info('LinkedIn profile exists at %s', PROFILE_DIR)
        return True

    # User-provided workspace configuration.
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    logger.warning(
        'LinkedIn profile not found. Run: python run.py linkedin-auth\n'
        'This will open a browser at linkedin.com/login for you to sign in.'
    )
    return False


def check_session_status() -> dict:
    """
    Check LinkedIn session status.

    Checks in order:
      1. .linkedin_storage.json (MCP-captured session)  -  preferred.
      2. Chromium's Cookies SQLite from the persistent profile  -  fallback.

    Returns dict with: logged_in, auth_cookies, days_until_expiry, warning, error,
                       source ('storage_state' | 'sqlite' | None)
    """
    result = {
        'logged_in': False,
        'auth_cookies': [],
        'days_until_expiry': None,
        'warning': None,
        'error': None,
        'source': None,
    }

    # ── 1. Check MCP storage state file first ────────────────────────────────
    if STORAGE_FILE.exists():
        try:
            state = json.loads(STORAGE_FILE.read_text(encoding='utf-8'))
            cookies = state.get('cookies', [])
            now = time.time()
            soonest: float | None = None
            found_auth: list[str] = []
            for c in cookies:
                if c.get('name') in _AUTH_COOKIE_NAMES:
                    found_auth.append(c['name'])
                    exp = c.get('expires')
                    if exp and exp > 0:
                        if exp > 1e13:
                            exp = exp / 1_000_000 - 11_644_473_600
                        if soonest is None or exp < soonest:
                            soonest = exp
            if found_auth:
                result['auth_cookies'] = found_auth
                result['source'] = 'storage_state'
                result['logged_in'] = True
                if soonest is not None:
                    days = (soonest - now) / 86400
                    result['days_until_expiry'] = days
                    if soonest < now:
                        result['logged_in'] = False
                        result['warning'] = 'LinkedIn storage-state session EXPIRED. Re-capture via MCP login.'
                    elif days < 7:
                        result['warning'] = (
                            f'LinkedIn storage-state session expires in {days:.1f} days  -  refresh soon.'
                        )
                return result
        except Exception as exc:
            logger.warning('Failed to parse %s: %s', STORAGE_FILE, exc)
            # Fall through to SQLite check

    # ── 2. Fall back to Chromium Cookies SQLite ───────────────────────────────
    cookies_db = PROFILE_DIR / 'Default' / 'Network' / 'Cookies'
    if not cookies_db.exists():
        result['error'] = 'Profile not seeded  -  run: python run.py linkedin-auth'
        return result

    try:
        uri = cookies_db.as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=3)
        rows = conn.execute(
            "SELECT name, expires_utc FROM cookies WHERE host_key LIKE '%linkedin%'"
        ).fetchall()
        conn.close()
    except Exception as exc:
        result['error'] = f'Could not read Cookies DB: {exc}'
        return result

    now = time.time()
    soonest: float | None = None

    for name, exp_raw in rows:
        if name in _AUTH_COOKIE_NAMES:
            result['auth_cookies'].append(name)
            if exp_raw and exp_raw > 0:
                exp_unix = float(exp_raw)
                if exp_unix > 1e13:
                    exp_unix = exp_unix / 1_000_000 - 11_644_473_600
                if soonest is None or exp_unix < soonest:
                    soonest = exp_unix

    result['logged_in'] = bool(result['auth_cookies'])
    if result['logged_in']:
        result['source'] = 'sqlite'

    if soonest is not None:
        days = (soonest - now) / 86400
        result['days_until_expiry'] = days
        if soonest < now:
            result['logged_in'] = False
            result['warning'] = 'LinkedIn session EXPIRED. Run: python run.py linkedin-auth'
        elif days < 7:
            result['warning'] = f'LinkedIn session expires in {days:.1f} days  -  refresh soon.'

    return result


# ── Human session ──────────────────────────────────────────────────────────────

class HumanSession:
    """
    Context-managed Playwright session that scrapes LinkedIn like a human.

    Usage:
        with HumanSession() as sess:
            data = sess.scrape_profile('https://www.linkedin.com/in/your-profile/')
    """

    def __init__(self, config: HumanConfig | None = None):
        self.cfg = config or HumanConfig()
        self._pw = None
        self._ctx = None
        self._page = None
        self._profiles_done = 0
        self._hour_bucket: list[float] = []

    def __enter__(self):
        # Prefer patchright (undetected drop-in)  -  falls back to vanilla
        # playwright if not installed so we never hard-break in CI.
        try:
            from patchright.sync_api import sync_playwright  # type: ignore
            using_patchright = True
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
                using_patchright = False
            except ImportError as exc:
                raise RuntimeError(
                    'LinkedIn needs Playwright. Run: pip install -e \".[integrations]\"; playwright install chromium' 
                ) from exc

        # Ensure profile is available
        ensure_profile_dir()

        # Pre-flight session check
        status = check_session_status()
        if status.get('error'):
            logger.warning('LinkedIn session: %s', status['error'])
        elif status.get('warning'):
            logger.warning('LinkedIn session: %s', status['warning'])
        elif status.get('logged_in'):
            days = status.get('days_until_expiry')
            logger.info('LinkedIn session valid (expires in %.0f days)', days or 0)

        # Use bundled patchright Chromium  -  avoids the launch hang caused by
        # channel='chrome' when Google Chrome's user-data-dir is locked or
        # incompatible with patchright's CDP patch level.
        launch_kwargs: dict = {
            'user_data_dir': str(PROFILE_DIR),
            'headless': self.cfg.headless,
            'viewport': {'width': 1366, 'height': 800},
            'args': [
                '--start-maximized',
                '--window-position=100,100',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-background-timer-throttling',
            ],
        }

        # IMPORTANT: do NOT pass --disable-blink-features=AutomationControlled
        # or ignore_default_args=['--enable-automation'] with patchright  - 
        # patchright's docs warn these are themselves detectable fingerprints
        # now. Patchright handles the underlying CDP leaks correctly.
        logger.info(
            'LinkedIn: launching browser (%s, bundled chromium)',
            'patchright' if using_patchright else 'playwright',
        )

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)

        # Inject MCP storage state cookies first (highest priority  -  actual session)
        if STORAGE_FILE.exists():
            try:
                state = json.loads(STORAGE_FILE.read_text(encoding='utf-8'))
                cookies = state.get('cookies', [])
                if cookies:
                    self._ctx.add_cookies(cookies)
                    logger.info(
                        'Loaded %d cookies from %s', len(cookies), STORAGE_FILE
                    )
            except Exception as exc:
                logger.warning('Failed to load storage state: %s', exc)

        # Also inject cookies.json if present (covers cold persistent profile)
        if COOKIES_PATH.exists():
            try:
                raw = json.loads(COOKIES_PATH.read_text(encoding='utf-8'))
                self._ctx.add_cookies(raw)
                logger.info('Injected %d LinkedIn cookies from cookies.json', len(raw))
            except Exception as exc:
                logger.warning('cookies.json present but failed to load: %s', exc)

        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ── Public API ─────────────────────────────────────────────────────────────

    def scrape_profile(self, url_or_handle: str) -> Optional[dict]:
        """
        Fetch one LinkedIn profile with human-like behavior.

        Returns dict with {url, headline, current_role, about_excerpt,
        education, recent_posts, html_excerpt} or None if blocked/cap hit.

        Raises CaptchaHit if LinkedIn shows a challenge page.
        """
        self._enforce_caps_or_raise()

        if url_or_handle.startswith('http'):
            url = url_or_handle
        else:
            url = f'https://www.linkedin.com/in/{url_or_handle.lstrip("/")}/'

        _lognormal_sleep(0.6, sigma=0.3)
        logger.info('LinkedIn: visiting %s', url)

        try:
            self._page.goto(validated_url(url, hosts=('www.linkedin.com', 'linkedin.com')), timeout=45000, wait_until='load')
        except Exception as exc:
            logger.warning('LinkedIn goto failed for %s: %s', url, exc)
            return None

        # Wait for React hydration. LinkedIn 2025 has no h1 on profile pages  - 
        # the name lives in an h2 inside a[href*="isSelfProfile="], which LinkedIn
        # appends specifically to the profile-view self-link. Multiple such anchors
        # exist (sticky nav fires early); wait until one contains an h2 (profile card).
        try:
            self._page.wait_for_function(
                "() => !!Array.from(document.querySelectorAll(\"a[href*='isSelfProfile=']\")).find(a => a.querySelector('h2'))",
                timeout=15000,
            )
        except Exception:
            pass  # don't crash  -  attempt extraction anyway

        if self._is_challenge():
            raise CaptchaHit(f'LinkedIn challenge page at {self._page.url}')

        # Check if we landed on login page (session expired)
        current_url = self._page.url
        if 'login' in current_url or 'authwall' in current_url:
            logger.warning(
                'LinkedIn session expired or not logged in. '
                'Run: python run.py linkedin-auth'
            )
            return None

        self._read_profile_like_human()

        html = self._page.content()

        # Dump debug HTML for the first profile only (filesystem-gated)
        self._maybe_dump_debug_html(url, html)

        data = {
            'url': current_url,
            'headline': self._extract_headline(),
            'current_role': self._extract_current_role(),
            'about_excerpt': self._extract_about()[:800],
            'education': self._extract_education(),
            'recent_posts': self._extract_recent_posts(),
            'html_excerpt': html[:4000],
        }

        self._profiles_done += 1
        self._hour_bucket.append(time.time())

        if random.random() < self.cfg.distraction_chance:
            self._do_distraction()

        _lognormal_sleep(self.cfg.page_dwell_mean_s * 0.4)

        if self._profiles_done % self.cfg.break_every_n_profiles == 0:
            self._take_break()

        return data

    def find_profile_url(self, name: str, firm: str) -> Optional[str]:
        """
        Search Google-style for a LinkedIn profile URL via LinkedIn search.
        Returns the first plausible /in/ URL, or None.
        """
        query = f'{name} {firm}'
        search_url = (
            f'https://www.linkedin.com/search/results/people/'
            f'?keywords={query.replace(" ", "%20")}'
        )

        _lognormal_sleep(1.5, sigma=0.3)
        try:
            self._page.goto(search_url, timeout=30000, wait_until='domcontentloaded')
        except Exception as exc:
            logger.warning('LinkedIn search goto failed: %s', exc)
            return None

        if self._is_challenge():
            raise CaptchaHit(f'LinkedIn challenge during search for {name}')

        _lognormal_sleep(3.0)

        # Try to extract first result link
        try:
            links = self._page.query_selector_all('a[href*="/in/"]')
            for link in links[:5]:
                href = link.get_attribute('href') or ''
                # Clean up the href
                if '/in/' in href:
                    # Strip query params
                    clean = href.split('?')[0]
                    if not clean.endswith('/'):
                        clean += '/'
                    if 'linkedin.com/in/' in clean:
                        # Validate: link text should contain partner name fragment
                        try:
                            link_text = (link.inner_text(timeout=500) or '').lower()
                            name_parts = name.lower().split()
                            if any(p in link_text for p in name_parts if len(p) > 2):
                                logger.info('Found LinkedIn URL: %s', clean)
                                return clean
                        except Exception:
                            return clean  # Return URL even without name validation
        except Exception as exc:
            logger.debug('LinkedIn search link extraction failed: %s', exc)

        return None

    # ── Human rhythm helpers ───────────────────────────────────────────────────

    def _read_profile_like_human(self):
        steps = max(3, int(random.gauss(self.cfg.scroll_steps_mean, 1.5)))
        for _ in range(steps):
            dy = random.randint(220, 520)
            try:
                self._page.mouse.wheel(0, dy)
            except Exception:
                break
            _lognormal_sleep(_jitter(1.8, 0.5))
            if random.random() < 0.15:
                try:
                    self._page.mouse.wheel(0, -random.randint(60, 180))
                except Exception:
                    pass
                _lognormal_sleep(_jitter(1.2, 0.4))
        _lognormal_sleep(self.cfg.page_dwell_mean_s, sigma=0.5)

    def _do_distraction(self):
        """Workspace-backed outreach integration."""
        choice = random.choice(['feed', 'messages', 'notifications'])
        targets = {
            'feed':          'https://www.linkedin.com/feed/',
            'messages':      'https://www.linkedin.com/messaging/',
            'notifications': 'https://www.linkedin.com/notifications/',
        }
        try:
            logger.debug('LinkedIn distraction -> %s', choice)
            self._page.goto(targets[choice], timeout=30000, wait_until='domcontentloaded')
            for _ in range(random.randint(2, 5)):
                try:
                    self._page.mouse.wheel(0, random.randint(200, 500))
                except Exception:
                    break
                _lognormal_sleep(_jitter(1.5, 0.5))
            _lognormal_sleep(random.uniform(4, 10))
        except Exception as exc:
            logger.debug('Distraction %s failed: %s', choice, exc)

    def _take_break(self):
        dur = random.uniform(self.cfg.break_min_s, self.cfg.break_max_s)
        logger.info('LinkedIn: taking %.0fs break (after %d profiles)',
                    dur, self._profiles_done)
        time.sleep(dur)

    def _enforce_caps_or_raise(self):
        if self._profiles_done >= self.cfg.max_profiles_per_session:
            raise RateLimitHit('Session cap reached')
        cutoff = time.time() - 3600
        self._hour_bucket = [t for t in self._hour_bucket if t >= cutoff]
        if len(self._hour_bucket) >= self.cfg.max_profiles_per_hour:
            raise RateLimitHit('Hourly cap reached')

    def _is_challenge(self) -> bool:
        try:
            body = self._page.inner_text('body', timeout=2000)
            return any(m in body.lower() for m in CAPTCHA_MARKERS)
        except Exception:
            return False

    # ── Extractors ─────────────────────────────────────────────────────────────

    # ── Debug dump ─────────────────────────────────────────────────────────────

    def _maybe_dump_debug_html(self, url: str, html: str) -> None:
        """Dump first 4000 chars of page HTML to data/.linkedin_debug/ for the
        first profile only (skipped once any .html file exists in that dir)."""
        try:
            debug_dir = PROJECT_ROOT / 'data' / '.linkedin_debug'
            debug_dir.mkdir(parents=True, exist_ok=True)
            existing = list(debug_dir.glob('*.html'))
            if existing:
                return  # already have a debug dump  -  skip to avoid disk bloat
            # Build a slug from the URL
            slug = url.rstrip('/').split('/')[-1] or 'profile'
            out_path = debug_dir / f'{slug}.html'
            out_path.write_text(html[:4000], encoding='utf-8')
            logger.info('LinkedIn debug HTML saved to %s', out_path)
        except Exception as exc:
            logger.debug('Failed to write debug HTML: %s', exc)

    def _extract_profile_card(self) -> dict:
        """Workspace-backed outreach integration."""
        try:
            return self._page.evaluate(r'''() => {
                // Multiple anchors have isSelfProfile=  -  find the one that
                // actually contains an h2 (the profile name card anchor).
                const anchors = Array.from(
                    document.querySelectorAll("a[href*='isSelfProfile=']")
                );
                const anchor = anchors.find(a => a.querySelector("h2")) || null;
                if (!anchor) return {name: '', headline: '', current_role: ''};

                // Name: text of the h2 inside the anchor
                const nameEl = anchor.querySelector("h2");
                const name = nameEl ? nameEl.textContent.trim() : '';

                // Climb up from anchor until we find a container
                // whose direct children include <p> siblings (headline, company)
                let container = anchor.parentElement;
                let headline = '', current_role = '';
                for (let i = 0; i < 7; i++) {
                    if (!container) break;
                    const directPs = Array.from(container.children)
                        .filter(c => c.tagName === 'P');
                    if (directPs.length >= 1) {
                        headline = directPs[0] ? directPs[0].textContent.trim() : '';
                        // Company·edu p: split on · and take the first segment
                        const compEdu = directPs[1] ? directPs[1].textContent.trim() : '';
                        if (compEdu && compEdu.includes('·')) {
                            current_role = compEdu.split('·')[0].trim();
                        } else {
                            current_role = compEdu;
                        }
                        break;
                    }
                    container = container.parentElement;
                }
                return {name, headline, current_role};
            }''')
        except Exception as exc:
            logger.debug('_extract_profile_card JS failed: %s', exc)
            return {'name': '', 'headline': '', 'current_role': ''}

    def _extract_headline(self) -> str:
        """Return the LinkedIn tagline (extracted via _extract_profile_card JS)."""
        try:
            card = self._extract_profile_card()
            return card.get('headline', '')
        except Exception:
            return ''

    def _extract_current_role(self) -> str:
        """Return current company from profile card (company segment of company·edu p)."""
        try:
            card = self._extract_profile_card()
            return card.get('current_role', '')
        except Exception:
            return ''

    def _extract_about(self) -> str:
        """
        Return the About section text.
        LinkedIn 2025: About/Experience are paywall-blurred for many viewing contexts.
        Look for the About section heading then extract text below it.
        Returns '' gracefully when not present (paywall or no about section).
        """
        try:
            text = self._page.evaluate(r'''() => {
                // Strategy 1: find a heading-level element whose text is "About"
                // (standalone), then look for text content in its sibling/parent.
                const headings = Array.from(document.querySelectorAll("h2, h3, h4, [role=heading]"));
                for (const h of headings) {
                    if (h.textContent.trim() !== 'About') continue;
                    // Found the About heading - look for text in the nearby container
                    let container = h.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!container) break;
                        // Find spans/ps with substantial text that aren't UI strings
                        const texts = Array.from(container.querySelectorAll("span, p"))
                            .map(el => el.textContent.trim())
                            .filter(t => t.length > 50 && t !== 'About');
                        if (texts.length) return texts[0];
                        container = container.parentElement;
                    }
                }
                return '';
            }''')
            return (text or '').strip()
        except Exception as exc:
            logger.debug('_extract_about JS failed: %s', exc)
            return ''

    def _extract_education(self) -> list[str]:
        """Return education entries. Returns [] if section is paywall-gated."""
        edu: list[str] = []
        try:
            results = self._page.evaluate(r'''() => {
                // Find a list section whose heading contains "Education"
                const headings = Array.from(document.querySelectorAll("h2, h3"))
                    .filter(el => /education/i.test(el.textContent));
                for (const h of headings) {
                    const section = h.closest("section, [data-section]") || h.parentElement;
                    if (!section) continue;
                    const items = Array.from(section.querySelectorAll("li")).slice(0, 6);
                    const texts = items.map(li => li.textContent.trim()).filter(t => t.length > 3);
                    if (texts.length) return texts;
                }
                return [];
            }''')
            edu = results or []
        except Exception as exc:
            logger.debug('_extract_education JS failed: %s', exc)
        return edu

    def _extract_recent_posts(self) -> list[str]:
        posts: list[str] = []
        try:
            els = self._page.query_selector_all(
                'div[data-urn*="activity"] .feed-shared-text'
            )
            for el in els[:3]:
                text = (el.inner_text(timeout=800) or '').strip()
                if text and len(text) > 20:
                    posts.append(text[:300])
        except Exception:
            pass
        return posts


# ── High-level partner scrape API ──────────────────────────────────────────────

def scrape_partner(partner: dict) -> dict:
    """
    Scrape LinkedIn for a VC partner.

    Args:
        partner: dict with keys: partner_name, firm, linkedin_url (optional)

    Returns:
        dict with: url, headline, current_role, about_excerpt, education,
                   recent_posts, html_excerpt, error (if failed)

    Does NOT open a browser if the session is known dead/expired.
    """
    name = partner.get('partner_name') or partner.get('name', '')
    firm = partner.get('firm', '')
    known_url = partner.get('linkedin_url')

    # Quick session check before opening browser
    status = check_session_status()
    if status.get('error') and not (PROFILE_DIR / 'Default').exists():
        return {
            'error': status['error'],
            'url': known_url,
            'headline': '',
            'current_role': '',
            'about_excerpt': '',
            'education': [],
            'recent_posts': [],
        }

    try:
        with HumanSession() as sess:
            # If no known URL, search for it first
            if not known_url:
                logger.info('Searching LinkedIn for: %s at %s', name, firm)
                known_url = sess.find_profile_url(name, firm)
                if not known_url:
                    return {
                        'error': f'Could not find LinkedIn URL for {name}',
                        'url': None,
                        'headline': '',
                        'current_role': '',
                        'about_excerpt': '',
                        'education': [],
                        'recent_posts': [],
                    }

            data = sess.scrape_profile(known_url)
            if data is None:
                return {
                    'error': 'Scrape returned None (blocked, session dead, or cap hit)',
                    'url': known_url,
                    'headline': '',
                    'current_role': '',
                    'about_excerpt': '',
                    'education': [],
                    'recent_posts': [],
                }

            # Add partner name for fact extractor context
            data['partner_name'] = name
            return data

    except CaptchaHit as exc:
        logger.error('LinkedIn captcha hit for %s: %s', name, exc)
        return {
            'error': f'CaptchaHit: {exc}',
            'url': known_url,
            'headline': '',
            'current_role': '',
            'about_excerpt': '',
            'education': [],
            'recent_posts': [],
        }
    except RateLimitHit as exc:
        logger.warning('LinkedIn rate limit for %s: %s', name, exc)
        return {
            'error': f'RateLimitHit: {exc}',
            'url': known_url,
            'headline': '',
            'current_role': '',
            'about_excerpt': '',
            'education': [],
            'recent_posts': [],
        }
    except Exception as exc:
        logger.exception('Unexpected LinkedIn error for %s', name)
        return {
            'error': str(exc),
            'url': known_url,
            'headline': '',
            'current_role': '',
            'about_excerpt': '',
            'education': [],
            'recent_posts': [],
        }
