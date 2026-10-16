"""Refresh only the user's PitchBook cookies from dedicated loopback Chrome CDP."""
from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.request import Request

# Resolve the package root for standalone use as well as installed use.
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))
from vcoutreach.web.vendor import data_path, setting_value, validated_url, fixed_urlopen

DATA_DIR = data_path('data')
LOG_DIR = DATA_DIR / 'logs'
LOG_FILE = LOG_DIR / 'pb_cookie_refresher.log'
DASHBOARD_URL = 'https://my.pitchbook.com/dashboard/private'
DEFAULT_INTERVAL = 1200
NAVIGATE_SETTLE_SECONDS = 6
MIN_BACKOFF = 30
MAX_BACKOFF = 300
logger = logging.getLogger(__name__)


class CdpUnreachable(Exception):
    """The dedicated local browser is unavailable."""


def _cdp_url(value: str) -> str:
    from urllib.parse import urlsplit
    value = validated_url(value, origin_only=True, loopback=True)
    if urlsplit(value).hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise CdpUnreachable('PitchBook CDP must use a loopback host')
    return value


def _build_logger() -> logging.Logger:
    return logger


def preflight(cdp_url: str) -> str:
    url = _cdp_url(cdp_url)
    try:
        with fixed_urlopen(Request(url + '/json/version'), timeout=5) as response:
            info = json.loads(response.read(16384))
        return str(info.get('Browser', '?'))
    except (OSError, ValueError) as exc:
        raise CdpUnreachable('Start the dedicated PitchBook Chrome and sign in first') from exc


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:
        raise RuntimeError('PitchBook needs Playwright. Run: pip install -e ".[integrations]"') from exc


def navigate_dashboard(cdp_url: str) -> None:
    url = _cdp_url(cdp_url)
    from urllib.parse import urlsplit
    with _playwright()() as pw:
        browser = pw.chromium.connect_over_cdp(url)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = next((page for page in context.pages if urlsplit(page.url).hostname == 'my.pitchbook.com'), None)
            page = page or context.new_page()
            page.goto(DASHBOARD_URL, wait_until='domcontentloaded', timeout=30000)
            time.sleep(NAVIGATE_SETTLE_SECONDS)
        finally:
            browser.close()


def _write_cookies(cookies: list[dict]) -> None:
    for name in ('.pitchbook_session.json', '.pitchbook_storage.json'):
        path = data_path('data/' + name)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.pb-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump({'cookies': cookies, 'origins': []}, handle)
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def refresh_once(cdp_url: str, navigate: bool) -> dict:
    url = _cdp_url(cdp_url)
    preflight(url)
    if navigate:
        navigate_dashboard(url)
    with _playwright()() as pw:
        browser = pw.chromium.connect_over_cdp(url)
        try:
            cookies = [cookie for context in browser.contexts for cookie in context.cookies() if cookie.get('domain', '').lstrip('.') in {'pitchbook.com', 'my.pitchbook.com'}]
        finally:
            browser.close()
    names = {c['name']: c.get('value', '') for c in cookies}
    result = {'session': len(names.get('SESSION', '')) > 20, 'cf_clearance': bool(names.get('cf_clearance')), 'cf_bm': bool(names.get('__cf_bm')), 'count': len(cookies), 'written': False, 'issues': []}
    if result['session']:
        _write_cookies(cookies)
        result['written'] = True
    return result


def _status_line(result: dict) -> str:
    return 'PitchBook cookies refreshed' if result.get('written') else 'No valid PitchBook session; existing files kept'


def run_once(cdp_url: str, navigate: bool) -> int:
    try:
        result = refresh_once(cdp_url, navigate)
        logger.info(_status_line(result))
        return 0 if result.get('written') else 1
    except (CdpUnreachable, RuntimeError, ValueError) as exc:
        logger.error('%s', str(exc).splitlines()[0])
        return 2
    except Exception:
        logger.error('PitchBook cookie refresh failed')
        return 1


def run_loop(cdp_url: str, interval: int, navigate: bool) -> int:
    try:
        while True:
            run_once(cdp_url, navigate)
            time.sleep(max(30, interval))
    except KeyboardInterrupt:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--no-navigate', action='store_true')
    parser.add_argument('--cdp-url', default=setting_value('PITCHBOOK_CDP_URL', 'http://127.0.0.1:9222'))
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    if args.once:
        return run_once(args.cdp_url, not args.no_navigate)
    return run_loop(args.cdp_url, args.interval, not args.no_navigate)


if __name__ == '__main__':
    sys.exit(main())
