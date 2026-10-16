"""Workspace-backed outreach integration."""
from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_THIS_FILE = Path(__file__).resolve()
from vcoutreach.web.vendor import workspace_root, workspace_path, data_path, validated_url
from vcoutreach.web.config import WorkspacePath
PROJECT_ROOT = WorkspacePath()

# NOTE on storageState vs persistent profile:
#   launch_persistent_context(user_data_dir=...) already persists cookies to disk.
#   The storageState JSON is saved ADDITIONALLY in login_interactive() as a
#   portability artifact / fast-snapshot  -  but is NOT loaded back into the
#   persistent context on startup (not how Playwright works). Future code should
#   not attempt context.storage_state() as an auth source for persistent contexts.

DEFAULT_PROFILE_DIR = PROJECT_ROOT / 'data' / '.pitchbook_profile'
DEFAULT_STATE_FILE = PROJECT_ROOT / 'data' / '.pitchbook_session.json'
STORAGE_FILE = PROJECT_ROOT / 'data' / '.pitchbook_storage.json'
DEBUG_DIR = PROJECT_ROOT / 'data' / '.pitchbook_debug'

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_LOOKUPS_PER_DAY = int(os.environ.get('PITCHBOOK_MAX_LOOKUPS_PER_DAY', '2000'))

# Throttle  -  min/max seconds between any two Playwright navigations
_THROTTLE_MIN = 8.0
_THROTTLE_MAX = 15.0

# Viewports to randomize across (width, height)
_VIEWPORTS = [
    {'width': 1366, 'height': 768},
    {'width': 1440, 'height': 900},
    {'width': 1920, 'height': 1080},
]

# Challenge / captcha markers
_CHALLENGE_MARKERS = ('challenge', 'verify', 'robot', 'recaptcha', 'are you human')

# Login detection markers
_LOGIN_MARKERS = ('/login', '/loginAction', 'sign in', 'log in to pitchbook')

# Dashboard URL prefix  -  what we check to confirm authenticated state
_DASHBOARD_URL = 'my.pitchbook.com/dashboard'
_LOGIN_URL = 'https://my.pitchbook.com/loginAction.do?action=login'


# ── Custom exceptions ─────────────────────────────────────────────────────────

class PitchBookSessionExpired(Exception):
    """Raised when PitchBook redirects to login or shows Sign In UI."""


class PitchBookChallenge(Exception):
    """Raised when PitchBook shows a captcha / bot-challenge page."""


class PitchBookDailyCapReached(Exception):
    """Raised when today's lookup count reaches MAX_LOOKUPS_PER_DAY."""


# ── Daily cap helpers (SQLite-backed) ─────────────────────────────────────────

def _get_db_path() -> Path:
    try:
        from vcoutreach.web.config import DB_PATH
        return DB_PATH
    except ImportError:
        return PROJECT_ROOT / 'data' / 'outreach.db'


def _get_today_lookups() -> int:
    """Return the number of PitchBook lookups recorded today."""
    import sqlite3
    today = date.today().isoformat()
    try:
        conn = sqlite3.connect(str(_get_db_path()))
        row = conn.execute(
            'SELECT lookups FROM pitchbook_usage WHERE date = ?', (today,)
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as exc:
        logger.warning('Could not read pitchbook_usage: %s', exc)
        return 0


def _increment_today_lookups() -> int:
    """Increment today's lookup count, return new total."""
    import sqlite3
    today = date.today().isoformat()
    now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    try:
        conn = sqlite3.connect(str(_get_db_path()))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute(
            """
            INSERT INTO pitchbook_usage (date, lookups, last_lookup_at)
            VALUES (?, 1, ?)
            ON CONFLICT(date) DO UPDATE SET
                lookups = lookups + 1,
                last_lookup_at = excluded.last_lookup_at
            """,
            (today, now_iso),
        )
        conn.commit()
        row = conn.execute(
            'SELECT lookups FROM pitchbook_usage WHERE date = ?', (today,)
        ).fetchone()
        conn.close()
        return row[0] if row else 1
    except Exception as exc:
        logger.warning('Could not update pitchbook_usage: %s', exc)
        return 0


# ── Timing helpers ─────────────────────────────────────────────────────────────

def _throttle_sleep() -> float:
    """Sleep 8-15s (random uniform) between page loads. Returns duration."""
    dur = random.uniform(_THROTTLE_MIN, _THROTTLE_MAX)
    logger.debug('PitchBook throttle sleep: %.1fs', dur)
    time.sleep(dur)
    return dur


def _human_scroll(page) -> None:
    """Perform a randomized human-like scroll pattern before clicks."""
    steps = random.randint(3, 6)
    for _ in range(steps):
        dy = random.randint(150, 450)
        try:
            page.mouse.wheel(0, dy)
        except Exception:
            break
        time.sleep(random.uniform(0.4, 1.2))
        # Occasional back-scroll
        if random.random() < 0.2:
            try:
                page.mouse.wheel(0, -random.randint(40, 120))
            except Exception:
                pass
            time.sleep(random.uniform(0.3, 0.8))


# ── Main session class ─────────────────────────────────────────────────────────

class PitchBookSession:
    """Workspace-backed outreach integration."""

    def __init__(
        self,
        profile_dir: str = DEFAULT_PROFILE_DIR,
        state_file: str = DEFAULT_STATE_FILE,
        headed: bool = True,
    ):
        self.profile_dir = str(data_path(profile_dir))
        self.state_file = str(data_path(state_file))
        self.headed = headed

        self._pw = None
        self._ctx = None
        self._page = None

    # ── Context manager ────────────────────────────────────────────────────────

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                'PitchBook needs Playwright. Run: pip install -e \".[integrations]\"; playwright install chromium' 
            ) from exc

        # Ensure profile dir exists
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        # Use vanilla Playwright's bundled Chromium. No channel='chrome' so the
        # launch is not tied to an installed Google Chrome binary. Stealth args
        # below are sufficient for PitchBook  -  it does not aggressively bot-detect
        # once the on-disk profile carries a valid session.
        launch_kwargs: dict = {
            'user_data_dir': self.profile_dir,
            'headless': not self.headed,
            'viewport': {'width': 1366, 'height': 800},
            'args': [
                '--start-maximized',
                '--window-position=100,100',
                '--no-first-run',
                '--no-default-browser-check',
                '--disable-background-timer-throttling',
            ],
        }

        logger.info(
            'PitchBook: launching browser (playwright, channel=chromium, headed=%s)',
            self.headed,
        )

        # IMPORTANT: do NOT pass --disable-blink-features=AutomationControlled
        # or ignore_default_args=['--enable-automation']  -  those overrides are
        # themselves detectable and are unnecessary for PitchBook's bot-detection.
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)

        # ROOT CAUSE FIX (2026-05-24): Do NOT call add_cookies() from the JSON
        # storage files here.  launch_persistent_context() already loads all
        # cookies from the on-disk Chromium profile
        # (.pitchbook_profile/Default/Network/Cookies).  Calling add_cookies()
        # with a potentially stale .pitchbook_storage.json OVERWRITES the
        # profile's live SESSION cookie with an older value, causing every API
        # call to return HTTP 401 within hours of auth.
        #
        # The persistent profile is the single source of truth.  The JSON files
        # (.pitchbook_session.json, .pitchbook_storage.json) are written for
        # portability/inspection but must never be injected back at startup.

        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, exc_type, exc, tb):
        # Save storageState snapshot on exit (portability artifact).
        # GUARD: only write if the current context actually has a SESSION cookie.
        # A failed auth attempt that captures no SESSION must NOT overwrite an
        # existing valid session on disk.
        if self._ctx and Path(self.profile_dir).exists():
            try:
                state = self._ctx.storage_state()
                has_session = any(
                    c.get("name") == "SESSION"
                    for c in state.get("cookies", [])
                )
            except Exception as exc_check:
                logger.warning('PitchBook: could not read storageState for SESSION check: %s', exc_check)
                has_session = False

            if not has_session:
                logger.warning(
                    'PitchBook __exit__: no SESSION cookie in context  -  skipping storageState write '
                    '(preserving existing valid session on disk)'
                )
            else:
                for target_path in (self.state_file, str(STORAGE_FILE)):
                    try:
                        self._ctx.storage_state(path=target_path)
                        logger.debug('PitchBook storageState saved to %s', target_path)
                    except Exception as exc_save:
                        logger.warning('Could not save storageState to %s: %s', target_path, exc_save)
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # ── Authentication ─────────────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """
        Navigate to dashboard and confirm we're logged in.

        Returns True only if:
          - Resolved URL contains 'my.pitchbook.com/dashboard'
          - No '/login' substring in the resolved URL
          - No 'Sign in' text visible on the page
        """
        try:
            logger.info('PitchBook: checking authentication via dashboard...')
            self._page.goto(
                'https://my.pitchbook.com/dashboard/private',
                timeout=30000,
                wait_until='domcontentloaded',
            )
            time.sleep(3)  # allow redirect to settle
            current_url = self._page.url.lower()

            if '/login' in current_url or 'loginaction' in current_url:
                logger.warning('PitchBook: session not authenticated (redirected to login)')
                return False

            if _DASHBOARD_URL not in current_url:
                logger.warning('PitchBook: unexpected URL after dashboard navigate: %s', current_url)
                return False

            # Extra check: no "Sign in" button visible
            try:
                sign_in_count = self._page.locator('text=Sign in').count()
                if sign_in_count > 0:
                    logger.warning('PitchBook: "Sign in" UI visible on dashboard')
                    return False
            except Exception:
                pass  # if locator fails, proceed

            logger.info('PitchBook: authenticated successfully')
            return True

        except Exception as exc:
            logger.warning('PitchBook: is_authenticated check failed: %s', exc)
            return False

    def login_interactive(self) -> bool:
        """Workspace-backed outreach integration."""
        print('=' * 70, flush=True)
        print('PitchBook Authentication', flush=True)
        print('=' * 70, flush=True)
        print(flush=True)
        print('A Chromium browser will open at the PitchBook login page.', flush=True)
        print('Sign in with your own PitchBook account.', flush=True)
        print('Complete 2FA if prompted.', flush=True)
        print('The browser will close automatically when login is detected.', flush=True)
        print('Press Ctrl+C to cancel.', flush=True)
        print(flush=True)

        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        try:
            self._page.goto(_LOGIN_URL, timeout=30000, wait_until='domcontentloaded')
        except Exception as exc:
            logger.warning('PitchBook: could not navigate to login: %s', exc)

        # Wait for DOM content and surface current URL so user sees it loaded
        try:
            self._page.wait_for_load_state('domcontentloaded', timeout=5000)
        except Exception:
            pass

        print(f'Navigated to: {self._page.url}', flush=True)

        try:
            self._page.bring_to_front()
        except Exception:
            pass

        # Give it 5 more seconds and check if we're stuck on about:blank
        time.sleep(5)
        if self._page.url in ('about:blank', ''):
            print(
                'Page failed to load  -  likely network or detection issue.',
                flush=True,
            )

        # Poll for successful login  -  up to 5 minutes
        timeout_s = 300
        poll_interval_s = 5
        elapsed = 0

        logger.info('PitchBook: waiting for user login (timeout=%ds)', timeout_s)

        while elapsed < timeout_s:
            time.sleep(poll_interval_s)
            elapsed += poll_interval_s

            try:
                current_url = self._page.url.lower()
            except Exception:
                continue

            if _DASHBOARD_URL in current_url:
                logger.info('PitchBook: login detected at %s', self._page.url)
                print(f'\nLogin detected! URL: {self._page.url}', flush=True)

                # Save debug screenshot + content
                try:
                    self._page.screenshot(path=str(DEBUG_DIR / 'dashboard.png'))
                    logger.info('PitchBook: saved dashboard screenshot to %s/dashboard.png', DEBUG_DIR)
                    content_excerpt = self._page.content()[:5000]
                    (DEBUG_DIR / 'dashboard_excerpt.html').write_text(
                        content_excerpt, encoding='utf-8'
                    )
                    logger.info('PitchBook: saved dashboard HTML excerpt to %s/dashboard_excerpt.html', DEBUG_DIR)
                except Exception as exc_debug:
                    logger.warning('PitchBook: could not save debug artifacts: %s', exc_debug)

                # Save storageState
                try:
                    self._ctx.storage_state(path=self.state_file)
                    print(f'Session saved to: {self.state_file}', flush=True)
                    logger.info('PitchBook: storageState saved to %s', self.state_file)
                except Exception as exc_save:
                    logger.warning('PitchBook: could not save storageState: %s', exc_save)

                print(flush=True)
                print('Login successful! You can now run:', flush=True)
                print('  python run.py pitchbook-enrich --partner-id 3', flush=True)
                print(flush=True)
                print('Also set PITCHBOOK_ENABLED=1 in your .env file.', flush=True)
                return True

            if elapsed % 30 == 0:
                remaining = timeout_s - elapsed
                mins, secs = divmod(remaining, 60)
                try:
                    current_display_url = self._page.url
                except Exception:
                    current_display_url = '(unknown)'
                print(
                    f'  Waiting for sign-in... ({mins}:{secs:02d} remaining)  -  current URL: {current_display_url}',
                    flush=True,
                )

        logger.warning('PitchBook: login timeout after %ds', timeout_s)
        print('\nTimeout reached  -  login not detected. Please try again.', flush=True)
        return False

    # ── Page safety checks ─────────────────────────────────────────────────────

    def _check_page_ok(self) -> None:
        """
        Called after every page.goto(). Raises on session expiry or challenge.
        """
        try:
            current_url = self._page.url.lower()
            content = self._page.inner_text('body', timeout=3000).lower()
        except Exception:
            return  # if we can't read body, proceed cautiously

        # Challenge detection  -  captcha / robot check
        if any(m in content for m in _CHALLENGE_MARKERS):
            # Also check for reCAPTCHA iframe
            try:
                captcha_frames = self._page.frames
                for frame in captcha_frames:
                    if 'recaptcha' in (frame.url or '').lower():
                        self._save_debug_state('captcha_hit')
                        raise PitchBookChallenge(
                            f'reCAPTCHA iframe detected at {self._page.url}. '
                            'Run pitchbook-auth to re-establish session.'
                        )
            except PitchBookChallenge:
                raise
            except Exception:
                pass
            self._save_debug_state('challenge_hit')
            raise PitchBookChallenge(
                f'Challenge/verify page at {self._page.url}. '
                'Run pitchbook-auth to re-establish session.'
            )

        # Session expiry detection
        if any(m in current_url for m in ('/login', '/loginaction')):
            self._save_debug_state('session_expired')
            raise PitchBookSessionExpired(
                f'PitchBook session expired  -  redirected to login at {self._page.url}. '
                'Run: python run.py pitchbook-auth to re-seed session.'
            )

        # Check for "Sign in" on page content (belt-and-suspenders)
        if 'sign in to pitchbook' in content or 'log in to pitchbook' in content:
            self._save_debug_state('session_expired_content')
            raise PitchBookSessionExpired(
                f'PitchBook login UI detected on page {self._page.url}. '
                'Run: python run.py pitchbook-auth to re-seed session.'
            )

    def _save_debug_state(self, label: str) -> None:
        """Save screenshot and HTML on error for debugging."""
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            self._page.screenshot(path=str(DEBUG_DIR / f'{label}.png'))
            html = self._page.content()[:20000]
            (DEBUG_DIR / f'{label}.html').write_text(html, encoding='utf-8')
            logger.info('PitchBook: debug state saved to %s/%s.*', DEBUG_DIR, label)
        except Exception as exc_debug:
            logger.debug('PitchBook: could not save debug state: %s', exc_debug)

    # ── Daily cap enforcement ─────────────────────────────────────────────────

    def _check_daily_cap(self) -> None:
        """Raises PitchBookDailyCapReached if at or above cap."""
        current = _get_today_lookups()
        if current >= MAX_LOOKUPS_PER_DAY:
            raise PitchBookDailyCapReached(
                f'Daily PitchBook lookup cap reached: {current}/{MAX_LOOKUPS_PER_DAY}. '
                f'Try again tomorrow.'
            )
        logger.debug('PitchBook daily cap: %d/%d used', current, MAX_LOOKUPS_PER_DAY)

    # ── Search ─────────────────────────────────────────────────────────────────

    def search_person(self, name: str, firm: str = None) -> Optional[dict]:
        """
        Search PitchBook for a person by name and optional firm.

        Strategy:
          1. Navigate to PitchBook search with query "{name} {firm}"
          2. Wait for results to load (SPA  -  wait for networkidle)
          3. Locate the best-match result using text-resilient selectors
          4. Return {url, id, name, firm} or None

        Note: Selectors are adaptive  -  we dump HTML on failure for debugging
        since exact selectors are unknown until a live session is established.
        """
        self._check_daily_cap()

        # Search by name only  -  including the firm name causes company profiles to
        # dominate the results on PitchBook's general search (/gs/all and /gs/profiles).
        query = name
        logger.info('PitchBook: searching for "%s" (firm=%s)', query, firm)

        _throttle_sleep()

        # Navigate to the dashboard  -  the authenticated platform does NOT
        # support URL-based people search (/search?q=...&type=people returns 404).
        # We must use the global search-bar UI input instead.
        import re
        result = None

        # If we're already on the dashboard (from is_authenticated() check), reuse it.
        # Otherwise navigate there.
        current_url = ''
        try:
            current_url = self._page.url
        except Exception:
            pass

        if _DASHBOARD_URL not in current_url:
            try:
                self._page.goto('https://my.pitchbook.com/dashboard/private', timeout=45000, wait_until='domcontentloaded')
            except Exception as exc:
                logger.warning('PitchBook: dashboard navigate failed: %s', exc)
                return None
            self._check_page_ok()

        # Wait for the global search input to appear (SPA hydration can be slow)
        _SEARCH_INPUT_SELECTORS = [
            'input[placeholder*="Search PitchBook"]',
            '#general-search-input',
            '[role="combobox"]',
            'input[placeholder*="search"]',
            'input[type="search"]',
            'input[name="query"]',
            'input[name="q"]',
            'input.search-input',
            'input[class*="search"]',
        ]
        # First wait for any of the selectors
        combined_selector = ', '.join(_SEARCH_INPUT_SELECTORS)
        try:
            self._page.wait_for_selector(combined_selector, timeout=20000)
        except Exception:
            logger.warning('PitchBook: search input did not appear within 20s; proceeding anyway')

        # Locate the global search input
        search_input = None
        for sel in _SEARCH_INPUT_SELECTORS:
            try:
                el = self._page.query_selector(sel)
                if el:
                    search_input = el
                    logger.info('PitchBook: found search input via selector: %s', sel)
                    break
            except Exception:
                continue

        if search_input is None:
            logger.warning('PitchBook: could not locate global search input; dumping debug HTML')
            # Save debug artifacts and bail
            debug_path = DEBUG_DIR / f'search_{name.replace(" ", "_")}.html'
            try:
                html = self._page.content()
                debug_path.write_text(html, encoding='utf-8')
                self._page.screenshot(path=str(DEBUG_DIR / f'search_{name.replace(" ", "_")}.png'))
                logger.info('PitchBook: search debug saved to %s', debug_path)
            except Exception:
                pass
            return None

        # Type the search query into the input
        try:
            search_input.click()
            time.sleep(0.4)
            search_input.fill(query)
            time.sleep(0.3)
        except Exception as exc:
            logger.warning('PitchBook: could not fill search input: %s', exc)
            return None

        # Strategy A: wait for autocomplete dropdown with people results
        autocomplete_found = False
        _AUTOCOMPLETE_SELECTORS = [
            'a[href*="/profiles/professional/"]',
            'a[href*="/profiles/investor/"]',
            'a[href*="/profiles/person/"]',
            'a[href*="/person/"]',
            'a[href*="/people/"]',
            '[class*="autocomplete"] a',
            '[class*="dropdown"] a',
            '[class*="suggestion"] a',
            '[class*="result"] a',
        ]
        for sel in _AUTOCOMPLETE_SELECTORS:
            try:
                self._page.wait_for_selector(sel, timeout=4000)
                autocomplete_found = True
                logger.info('PitchBook: autocomplete appeared via selector: %s', sel)
                break
            except Exception:
                continue

        if not autocomplete_found:
            # Strategy B: press Enter to navigate to a full results page (/gs/all)
            logger.info('PitchBook: no autocomplete  -  pressing Enter for full results page')
            try:
                self._page.keyboard.press('Enter')
                try:
                    self._page.wait_for_load_state('networkidle', timeout=15000)
                except Exception:
                    pass
                time.sleep(2)
            except Exception as exc:
                logger.warning('PitchBook: Enter key submission failed: %s', exc)

            # Click the "Profiles" tab to filter to person/company profiles
            # The all-results tab (/gs/all) shows events/companies by default
            try:
                profiles_tab = self._page.query_selector('[data-testid="tabs-profiles"]')
                if profiles_tab:
                    profiles_tab.click()
                    logger.info('PitchBook: clicked Profiles tab')
                    try:
                        self._page.wait_for_load_state('networkidle', timeout=10000)
                    except Exception:
                        pass
                    time.sleep(1.5)
                else:
                    logger.info('PitchBook: Profiles tab not found; staying on All tab')
            except Exception as exc:
                logger.warning('PitchBook: could not click Profiles tab: %s', exc)

        _human_scroll(self._page)

        # Always save fresh debug artifacts (overwrite stale files)
        debug_name = name.replace(' ', '_')
        debug_path = DEBUG_DIR / f'search_{debug_name}.html'
        try:
            html = self._page.content()
            debug_path.write_text(html, encoding='utf-8')
            self._page.screenshot(path=str(DEBUG_DIR / f'search_{debug_name}.png'))
            logger.info('PitchBook: search debug saved to %s (url=%s)', debug_path, self._page.url)
        except Exception:
            pass

        # Strategy 1: look for anchor href links (some result types use <a>)
        try:
            _LINK_SELECTORS = [
                'a[href*="/profiles/professional/"]',
                'a[href*="/profiles/investor/"]',
                'a[href*="/profiles/person/"]',
                'a[href*="/person/"]',
                'a[href*="/people/"]',
            ]
            for sel in _LINK_SELECTORS:
                try:
                    links = self._page.query_selector_all(sel)
                except Exception:
                    continue
                for link in links[:30]:
                    try:
                        href = link.get_attribute('href') or ''
                        if not href or href.startswith('#') or 'brightspot' in href:
                            continue
                        is_profile = (
                            '/profiles/professional/' in href
                            or '/profiles/investor/' in href
                            or '/profiles/person/' in href
                            or '/person/' in href
                            or '/people/' in href
                            or bool(re.search(r'/[a-z-]+/\d{5,}-\d+', href))
                        )
                        if not is_profile:
                            continue
                        link_text = (link.inner_text(timeout=800) or '').lower()
                        name_parts = name.lower().split()
                        if any(p in link_text for p in name_parts if len(p) > 2):
                            m = re.search(
                                r'/profiles/(?:professional|investor|person)/([^/?#]+)'
                                r'|/(?:person|people)/([^/?#]+)',
                                href,
                            )
                            pb_id = (m.group(1) or m.group(2)) if m else None
                            full_url = (
                                href if href.startswith('http')
                                else f'https://my.pitchbook.com{href}'
                            )
                            result = {
                                'url': full_url,
                                'id': pb_id,
                                'name': name,
                                'firm': firm,
                            }
                            logger.info('PitchBook: found person profile via anchor: %s', full_url)
                            break
                    except Exception:
                        continue
                if result:
                    break
        except Exception as exc:
            logger.warning('PitchBook: anchor link extraction failed: %s', exc)

        # Strategy 2: result cards use <button> with JS routing (no href).
        # Use JavaScript evaluation to atomically extract card data (avoids stale
        # element handle issues from Playwright's element refs).
        if result is None:
            try:
                # Atomically extract title, entity tag, and card index via JS
                name_parts = [p for p in name.lower().split() if len(p) > 2]
                _PERSON_ENTITY_TYPES = ('investor', 'person', 'executive', 'partner', 'professional')
                card_data = self._page.evaluate(r'''
                    () => {
                        const cards = document.querySelectorAll('[data-testid="page-result-card"]');
                        return Array.from(cards).map((card, idx) => {
                            const titleEl = card.querySelector('[data-testid="page-result-card-header-title"]');
                            const tagEl = card.querySelector('[data-testid="page-result-card-entity-tag"]');
                            return {
                                idx: idx,
                                title: titleEl ? (titleEl.textContent || '') : '',
                                tag: tagEl ? (tagEl.textContent || '') : '',
                            };
                        });
                    }
                ''')
                logger.info('PitchBook: JS extracted %d card records', len(card_data) if card_data else 0)
                for cd in (card_data or []):
                    logger.debug('PitchBook: card[%d] title=%r tag=%r', cd.get('idx'), cd.get('title', '')[:60], cd.get('tag', ''))

                # Find best matching card
                best_idx = None
                best_is_person = False
                for cd in (card_data or []):
                    title_lower = (cd.get('title') or '').lower()
                    tag_lower = (cd.get('tag') or '').lower()
                    if not all(p in title_lower for p in name_parts):
                        continue
                    is_person = any(t in tag_lower for t in _PERSON_ENTITY_TYPES)
                    if is_person and not best_is_person:
                        best_idx = cd['idx']
                        best_is_person = True
                        break
                    elif not best_is_person and best_idx is None:
                        best_idx = cd['idx']

                if best_idx is not None:
                    logger.info('PitchBook: clicking card[%d] (is_person=%s)', best_idx, best_is_person)
                    # Click the button inside the matched card via JS (avoids stale handle)
                    clicked = self._page.evaluate(r'''
                        (idx) => {
                            const cards = document.querySelectorAll('[data-testid="page-result-card"]');
                            const card = cards[idx];
                            if (!card) return false;
                            // Try header title button first
                            const titleEl = card.querySelector('[data-testid="page-result-card-header-title"]');
                            const btn = titleEl ? titleEl.querySelector('button') : null;
                            if (btn) { btn.click(); return true; }
                            // Fall back to clicking the card itself
                            card.click();
                            return true;
                        }
                    ''', best_idx)
                    logger.info('PitchBook: card click dispatched (clicked=%s)', clicked)
                    try:
                        self._page.wait_for_load_state('networkidle', timeout=15000)
                    except Exception:
                        pass
                    time.sleep(1.5)
                    profile_url = self._page.url
                    logger.info('PitchBook: clicked card; landed on %s', profile_url)
                    # PitchBook profile URL patterns:
                    # /profile/{id}/investor/profile   (individual investor)
                    # /profiles/professional/{slug}    (older format)
                    # /profiles/person/{slug}          (older format)
                    m = re.search(
                        r'/profile/([^/?#]+)/(?:investor|person|professional|executive)'
                        r'|/profiles/(?:professional|investor|person)/([^/?#]+)'
                        r'|/(?:person|people)/([^/?#]+)',
                        profile_url,
                    )
                    if m:
                        pb_id = m.group(1) or m.group(2) or m.group(3)
                    else:
                        pb_id = None
                    is_profile_url = (
                        '/profile/' in profile_url
                        or '/profiles/' in profile_url
                        or '/person' in profile_url
                    )
                    if is_profile_url:
                        result = {
                            'url': profile_url,
                            'id': pb_id,
                            'name': name,
                            'firm': firm,
                        }
                        logger.info('PitchBook: found person profile via card click: %s', profile_url)
                    else:
                        logger.warning('PitchBook: card click landed on unexpected URL: %s', profile_url)
            except Exception as exc:
                logger.warning('PitchBook: card-button extraction failed: %s', exc)

        return result

    def _search_public_fallback(self, name: str, firm: str = None) -> Optional[dict]:
        """
        Disabled: pitchbook.com/search is the public marketing site and never
        returns professional profile links for authenticated users.
        Kept as a stub so callers don't break.
        """
        logger.debug('PitchBook: public search fallback is disabled (wrong domain)')
        return None

    # ── Person profile scrape ─────────────────────────────────────────────────

    def scrape_person_profile(self, url: str) -> dict:
        """
        Scrape a PitchBook professional profile page.

        Returns dict with:
          bio, current_role, prior_firms, sectors, recent_deals, board_seats,
          public_writing, thesis_quote, raw_html_excerpt

        All fields default to empty  -  partial results are returned on selector
        failure. Debug HTML is saved to data/.pitchbook_debug/ on selector
        failures so selectors can be refined after the first live run.
        """
        self._check_daily_cap()

        logger.info('PitchBook: scraping person profile: %s', url)
        _throttle_sleep()

        result = {
            'url': url,
            'bio': '',
            'current_role': '',
            'prior_firms': [],
            'sectors': [],
            'recent_deals': [],
            'board_seats': [],
            'public_writing': [],
            'thesis_quote': None,
            'raw_html_excerpt': '',
        }

        try:
            self._page.goto(validated_url(url, hosts=('my.pitchbook.com',)), timeout=45000, wait_until='domcontentloaded')
        except Exception as exc:
            logger.warning('PitchBook: profile page load failed for %s: %s', url, exc)
            result['_error'] = str(exc)
            return result

        self._check_page_ok()

        # Wait for SPA content
        try:
            self._page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass

        time.sleep(2)  # extra settle for React/Angular renders
        _human_scroll(self._page)

        # Save debug screenshot + HTML for first-run inspection
        # Strip hash fragment before deriving slug so filenames stay clean
        name_slug = url.split('#')[0].rstrip('/').split('/')[-1]
        debug_html_path = DEBUG_DIR / f'profile_{name_slug}.html'
        debug_png_path = DEBUG_DIR / f'profile_{name_slug}.png'
        html_content = ''
        try:
            html_content = self._page.content()
            # Always overwrite debug files so stale captures don't mask fresh runs
            debug_html_path.write_text(html_content, encoding='utf-8')
            self._page.screenshot(path=str(debug_png_path))
            logger.info('PitchBook: profile debug saved to %s (url=%s)', DEBUG_DIR, self._page.url)
        except Exception as exc_debug:
            logger.debug('PitchBook: could not save profile debug: %s', exc_debug)

        result['raw_html_excerpt'] = html_content[:8000] if html_content else ''

        # ── Extract fields via JavaScript evaluate (avoids stale element issues) ──
        # Selectors verified against real PitchBook investor profile HTML
        # (profile URL format: /profile/{id}/investor/profile)
        # Structure: <section id="general-info"> contains Description <p>
        #            <section id="investments"> contains table with company links
        #            <section id="board-seats"> contains board seat table
        try:
            js_data = self._page.evaluate(r'''
                () => {
                    function txt(el) { return el ? (el.textContent || '').trim() : ''; }
                    function txts(els) { return Array.from(els || []).map(e => txt(e)).filter(Boolean); }

                    // Bio: first <p> inside #general-info
                    const genInfo = document.querySelector('#general-info');
                    const bioPara = genInfo ? genInfo.querySelector('p') : null;
                    const bio = txt(bioPara);

                    // Current role: profile-title data-testid
                    const titleEl = document.querySelector('[data-testid="profile-title"]');
                    const role = txt(titleEl);

                    // FIX 3: Board seats  -  first-cell anchor only, excludes firm-name anchors
                    const boardSection = document.querySelector('#board-seats');
                    const boardSeats = boardSection
                        ? txts(boardSection.querySelectorAll('table tbody tr td:first-child a'))
                        : [];

                    // FIX 1: Full row deal extraction  -  all columns per row
                    const investSection = document.querySelector('#investments');
                    const deals = [];
                    if (investSection) {
                        const rows = investSection.querySelectorAll('table tbody tr');
                        for (const row of rows) {
                            const cells = row.querySelectorAll('td');
                            if (cells.length < 1) continue;
                            const company = cells[0] ? (cells[0].innerText || '').trim() : '';
                            const date    = cells[1] ? (cells[1].innerText || '').trim() : '';
                            const round   = cells[2] ? (cells[2].innerText || '').trim() : '';
                            const amount  = cells[3] ? (cells[3].innerText || '').trim() : '';
                            if (company) deals.push({ company, date, round, amount });
                        }
                    }

                    // Investment preferences / sectors
                    const prefSection = document.querySelector('#investments-by-industry') ||
                                        document.querySelector('#investment-preferences');
                    const sectors = prefSection
                        ? txts(prefSection.querySelectorAll('td:first-child')).slice(0, 10)
                        : [];

                    return { bio, role, boardSeats, deals, sectors };
                }
            ''')
            logger.info('PitchBook: JS profile extraction: bio=%d chars, deals=%d, board=%d',
                        len(js_data.get('bio', '')), len(js_data.get('deals', [])), len(js_data.get('boardSeats', [])))

            if js_data.get('bio'):
                result['bio'] = js_data['bio']
            if js_data.get('role'):
                result['current_role'] = js_data['role']
            if js_data.get('boardSeats'):
                result['board_seats'] = js_data['boardSeats']
            if js_data.get('deals'):
                # FIX 1: deals are already dicts {company, date, round, amount}
                result['recent_deals'] = [
                    d for d in js_data['deals'][:15]
                    if isinstance(d, dict) and d.get('company')
                ]
            if js_data.get('sectors'):
                result['sectors'] = js_data['sectors']
        except Exception as exc:
            logger.warning('PitchBook: JS profile extraction failed: %s', exc)

        # FIX 2: Navigate to the investments sub-tab for full historical deal list.
        # Profile URL format: /profile/{id}/investor/profile → add /investments sibling tab
        # This costs +1 PB view per partner but surfaces the complete deal history.
        investments_url = None
        try:
            # Strip hash fragment before URL manipulation  -  search_person() captures
            # self._page.url after SPA navigation, which may include a hash like
            # #investments. A URL like .../investor/profile#investments would break
            # the endswith('/profile') check and produce a malformed investments URL.
            clean_url = url.split('#')[0].rstrip('/')
            if clean_url.endswith('/profile'):
                investments_url = clean_url[:-len('/profile')] + '/investments'
            elif '/investor' in clean_url and not clean_url.endswith('/investments'):
                investments_url = clean_url + '/investments'
        except Exception:
            investments_url = None

        if investments_url:
            try:
                logger.info('PitchBook: navigating to investments sub-tab: %s', investments_url)
                _throttle_sleep()
                self._page.goto(validated_url(investments_url, hosts=("my.pitchbook.com",)), timeout=45000, wait_until='domcontentloaded')
                try:
                    self._page.wait_for_load_state('networkidle', timeout=15000)
                except Exception:
                    pass
                time.sleep(2)
                _human_scroll(self._page)

                # Save debug artifacts for the investments tab
                try:
                    inv_html = self._page.content()
                    (DEBUG_DIR / f'investments_{name_slug}.html').write_text(inv_html, encoding='utf-8')
                    self._page.screenshot(path=str(DEBUG_DIR / f'investments_{name_slug}.png'))
                except Exception:
                    pass

                # Extract deals from investments sub-tab using same row-extraction JS
                # Also log cell count for the first row so we can diagnose column structure.
                inv_result = self._page.evaluate(r'''
                    () => {
                        const investSection = document.querySelector('#investments');
                        if (!investSection) return { deals: [], firstRowCellCount: 0, sectionIds: [] };
                        const rows = investSection.querySelectorAll('table tbody tr');
                        const deals = [];
                        let firstRowCellCount = 0;
                        for (const row of rows) {
                            const cells = row.querySelectorAll('td');
                            if (firstRowCellCount === 0) firstRowCellCount = cells.length;
                            if (cells.length < 1) continue;
                            const company = cells[0] ? (cells[0].innerText || '').trim() : '';
                            const date    = cells[1] ? (cells[1].innerText || '').trim() : '';
                            const round   = cells[2] ? (cells[2].innerText || '').trim() : '';
                            const amount  = cells[3] ? (cells[3].innerText || '').trim() : '';
                            if (company) deals.push({ company, date, round, amount });
                        }
                        const sectionIds = Array.from(
                            document.querySelectorAll('section[id]')
                        ).map(s => s.id);
                        return { deals, firstRowCellCount, sectionIds };
                    }
                ''')
                inv_deals = inv_result.get('deals', []) if isinstance(inv_result, dict) else inv_result
                first_row_cells = inv_result.get('firstRowCellCount', '?') if isinstance(inv_result, dict) else '?'
                sub_tab_sections = inv_result.get('sectionIds', []) if isinstance(inv_result, dict) else []
                logger.info(
                    'PitchBook: investments sub-tab sections=%s, first-row cell count=%s',
                    sub_tab_sections, first_row_cells,
                )

                if inv_deals:
                    logger.info(
                        'PitchBook: investments sub-tab returned %d deals', len(inv_deals)
                    )
                    # Merge with main-profile deals  -  dedupe by (company, date)
                    existing_keys = {
                        (d.get('company', ''), d.get('date', ''))
                        for d in result['recent_deals']
                        if isinstance(d, dict)
                    }
                    for d in inv_deals:
                        if not isinstance(d, dict) or not d.get('company'):
                            continue
                        key = (d.get('company', ''), d.get('date', ''))
                        if key not in existing_keys:
                            result['recent_deals'].append(d)
                            existing_keys.add(key)
                    # Cap at 15 total deals
                    result['recent_deals'] = result['recent_deals'][:15]
                    logger.info(
                        'PitchBook: after merge, %d unique deals', len(result['recent_deals'])
                    )

                # Increment daily cap for the investments sub-tab page load
                _increment_today_lookups()

            except Exception as exc_inv:
                logger.warning('PitchBook: investments sub-tab navigation failed: %s', exc_inv)

        # Fallback to resilient selector extraction if JS returned nothing useful
        if not result['bio']:
            result['bio'] = self._extract_text_resilient([
                '#general-info p',
                'section[id="general-info"] p',
                '[data-test="bio"]',
                '[class*="bio"]',
            ], 'bio')

        if not result['current_role']:
            result['current_role'] = self._extract_text_resilient([
                '[data-testid="profile-title"]',
                '[data-test="title"]',
                'h2',
            ], 'current_role')

        if not result['board_seats']:
            # FIX 3: first-cell anchor only  -  excludes firm-name anchors in col 2+
            result['board_seats'] = self._extract_list_resilient([
                '#board-seats table tbody tr td:first-child a',
                '#board-seats table tr td:first-child a',
            ], 'board_seats')

        if not result['recent_deals']:
            result['recent_deals'] = self._extract_deals()

        if not result['sectors']:
            result['sectors'] = self._extract_list_resilient([
                '#investments-by-industry td:first-child',
                '[class*="sector"]',
            ], 'sectors')

        # Prior firms  -  from bio text (no dedicated section on PitchBook investor profiles)
        result['prior_firms'] = self._extract_list_resilient([
            '#team a',
            'section[id="team"] a',
        ], 'prior_firms')

        # Thesis quote  -  look in bio or overview section
        bio_text = result['bio']
        import re
        _THESIS_MARKERS = [
            'we invest in', 'we focus on', 'we back', 'i believe', 'thesis',
            'looking for', 'our fund', 'conviction', 'mission is', 'focused on',
        ]
        for marker in _THESIS_MARKERS:
            if marker in bio_text.lower():
                idx = bio_text.lower().find(marker)
                excerpt = bio_text[max(0, idx - 10):idx + 250].strip()
                if len(excerpt) > 30:
                    result['thesis_quote'] = excerpt[:280]
                break

        logger.info(
            'PitchBook: profile scrape complete for %s  -  bio=%d chars, deals=%d, board=%d',
            url, len(result['bio']), len(result['recent_deals']), len(result['board_seats'])
        )

        # Increment daily counter only on successful scrape
        _increment_today_lookups()

        return result

    def _extract_text_resilient(self, selectors: list[str], field_name: str) -> str:
        """Try each selector in order, return first non-empty result."""
        for sel in selectors:
            try:
                el = self._page.query_selector(sel)
                if el:
                    text = (el.inner_text(timeout=1500) or '').strip()
                    if text and len(text) > 3:
                        return text
            except Exception as exc:
                logger.debug('PitchBook: selector %r failed for %s: %s', sel, field_name, exc)
                continue

        logger.debug('PitchBook: no selector worked for %s', field_name)
        return ''

    def _extract_list_resilient(self, selectors: list[str], field_name: str) -> list[str]:
        """Try each selector in order, return list of text items."""
        for sel in selectors:
            try:
                els = self._page.query_selector_all(sel)
                if els:
                    items = []
                    for el in els[:10]:
                        try:
                            text = (el.inner_text(timeout=800) or '').strip()
                            if text and len(text) > 1:
                                items.append(text)
                        except Exception:
                            continue
                    if items:
                        return items
            except Exception as exc:
                logger.debug('PitchBook: list selector %r failed for %s: %s', sel, field_name, exc)
                continue

        logger.debug('PitchBook: no list selector worked for %s', field_name)
        return []

    def _extract_deals(self) -> list[dict]:
        """
        Extract recent deals from the profile.
        Adaptive  -  tries several selector patterns and parses what it finds.
        Returns list of {company, date, round, amount} dicts.
        """
        deals = []
        deal_selectors = [
            '#investments table tr',      # investments tab table rows
            '#lead-partners table tr',    # lead partners on deals table
            '[class*="investments-table"] tr',
            '[class*="deal"] [class*="row"]',
            'section:has-text("Deal") tr',
            'table tr',
        ]

        for sel in deal_selectors:
            try:
                els = self._page.query_selector_all(sel)
                if els and len(els) >= 1:
                    for el in els[:10]:
                        try:
                            text = (el.inner_text(timeout=800) or '').strip()
                            if text and len(text) > 5 and text.lower() not in ('company', 'date', 'round', 'amount'):
                                deals.append({'raw': text[:200]})
                        except Exception:
                            continue
                    if deals:
                        break
            except Exception:
                continue

        return deals

    # ── Firm profile scrape ───────────────────────────────────────────────────

    def scrape_firm_profile(self, url: str) -> dict:
        """
        Scrape a PitchBook investor/firm profile page.

        Returns dict with:
          firm_name, aum, hq, thesis, recent_investments, portfolio_count,
          fund_size_history, partners
        """
        self._check_daily_cap()

        logger.info('PitchBook: scraping firm profile: %s', url)
        _throttle_sleep()

        result = {
            'url': url,
            'firm_name': '',
            'aum': '',
            'hq': '',
            'thesis': '',
            'recent_investments': [],
            'portfolio_count': None,
            'fund_size_history': [],
            'partners': [],
        }

        try:
            self._page.goto(validated_url(url, hosts=('my.pitchbook.com',)), timeout=45000, wait_until='domcontentloaded')
        except Exception as exc:
            logger.warning('PitchBook: firm profile load failed: %s', exc)
            result['_error'] = str(exc)
            return result

        self._check_page_ok()

        try:
            self._page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass

        time.sleep(2)
        _human_scroll(self._page)

        # Save debug
        name_slug = url.rstrip('/').split('/')[-1]
        try:
            html = self._page.content()
            debug_path = DEBUG_DIR / f'firm_{name_slug}.html'
            if not debug_path.exists():
                debug_path.write_text(html[:20000], encoding='utf-8')
                self._page.screenshot(path=str(DEBUG_DIR / f'firm_{name_slug}.png'))
        except Exception:
            pass

        result['firm_name'] = self._extract_text_resilient(
            ['h1', '[class*="firm-name"]', '[data-test="name"]'], 'firm_name'
        )
        result['aum'] = self._extract_text_resilient(
            ['[class*="aum"]', '[data-test*="aum"]', 'text=AUM'], 'aum'
        )
        result['hq'] = self._extract_text_resilient(
            ['[class*="hq"]', '[class*="location"]', '[data-test*="location"]'], 'hq'
        )
        result['thesis'] = self._extract_text_resilient(
            ['[class*="thesis"]', '[class*="overview"] p', 'section:has-text("Overview") p'], 'thesis'
        )
        result['partners'] = self._extract_list_resilient(
            ['[class*="team"] a', 'section:has-text("Team") a', '[data-test*="partner"] span'], 'partners'
        )
        result['recent_investments'] = self._extract_deals()

        _increment_today_lookups()
        return result
