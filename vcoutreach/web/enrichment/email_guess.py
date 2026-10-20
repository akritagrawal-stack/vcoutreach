"""
Email guessing for VC partners.

Generates common email patterns for a given first name, last name, and domain,
then optionally probes via SMTP RCPT TO to validate.

Expected hit rate: ~30-50%. Many corporate VC domains block SMTP probes on port 25.
Catch-all detection: probes a known-bad address first; if accepted, marks domain
as catch-all and skips real probes.

Note: Port 25 is commonly blocked by ISPs on residential/cloud connections.
When blocked, all probes are marked 'inconclusive' and the best-pattern guess
is returned with low confidence.
"""
from __future__ import annotations

import logging
import re
import smtplib
import socket
import time
import unicodedata
from vcoutreach.web.vendor import validated_url, normalized_email

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT = 8
_SMTP_DELAY = 1.5   # seconds between probes (respect target servers)
_MAX_PROBES_PER_DOMAIN = 6

FREE_EMAIL_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'yahoo.com', 'ymail.com', 'rocketmail.com',
    'hotmail.com', 'outlook.com', 'live.com', 'msn.com', 'aol.com',
    'icloud.com', 'me.com', 'mac.com', 'proton.me', 'protonmail.com',
    'pm.me', 'mail.com', 'gmx.com', 'gmx.net', 'hey.com', 'fastmail.com',
    'tuta.com', 'tutanota.com', 'yandex.com', 'yandex.ru', 'qq.com', '163.com',
})


def is_free_email_domain(domain: str) -> bool:
    domain = (domain or '').strip().lower().rstrip('.')
    return (any(domain == free or domain.endswith('.' + free) for free in FREE_EMAIL_DOMAINS)
            or domain.startswith(('yahoo.', 'hotmail.', 'outlook.')))


def _normalize_name_part(s: str) -> str:
    """Lowercase, strip accents, remove non-alpha chars."""
    nfkd = unicodedata.normalize('NFKD', s)
    ascii_only = nfkd.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z]', '', ascii_only.lower())


def guess_emails(first: str, last: str, domain: str) -> list[dict]:
    """
    Generate candidate email addresses for a person at a given domain.

    Returns list of {email, pattern, confidence} dicts, ordered most-likely first.
    Confidence is a heuristic 0-1 score.

    Patterns tried (in priority order):
      first.last@domain    -  most common in professional/VC contexts
      first@domain         -  common for brand-name VCs with short names
      flast@domain         -  common enterprise pattern
      firstl@domain        -  variant
      f.last@domain        -  dot-initial variant
      firstlast@domain     -  concatenated
    """
    f = _normalize_name_part(first)
    l = _normalize_name_part(last)
    domain = (domain or '').strip().lower().rstrip('.')
    if not f or not l or not domain or is_free_email_domain(domain):
        return []

    fi = f[0]   # first initial
    li = l[0]   # last initial

    candidates = [
        {'pattern': 'first.last',  'email': f'{f}.{l}@{domain}',   'confidence': 0.85},
        {'pattern': 'first',       'email': f'{f}@{domain}',        'confidence': 0.75},
        {'pattern': 'flast',       'email': f'{fi}{l}@{domain}',    'confidence': 0.70},
        {'pattern': 'firstl',      'email': f'{f}{li}@{domain}',    'confidence': 0.60},
        {'pattern': 'f.last',      'email': f'{fi}.{l}@{domain}',   'confidence': 0.55},
        {'pattern': 'firstlast',   'email': f'{f}{l}@{domain}',     'confidence': 0.50},
    ]

    # Deduplicate (handles very short names where patterns collapse)
    seen: set[str] = set()
    result = []
    for c in candidates:
        if c['email'] not in seen:
            seen.add(c['email'])
            result.append(c)

    return result


def _get_mx_host(domain: str) -> str | None:
    """Resolve the primary MX record for a domain. Returns hostname or None."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, 'MX')
        # Sort by preference (lowest first)
        sorted_ans = sorted(answers, key=lambda r: r.preference)
        if sorted_ans:
            return str(sorted_ans[0].exchange).rstrip('.')
    except ImportError:
        logger.warning('Email MX checks need dnspython. Run: pip install -e \".[integrations]\"')
    except Exception as exc:
        logger.debug('MX lookup failed for %s: %s', domain, exc)
    return None


def probe_smtp(email: str, mx_host: str | None = None) -> str:
    """
    Probe an email address via SMTP RCPT TO.

    Returns:
      'valid'         -  server accepted this specific address (250)
      'invalid'       -  server rejected (5xx)
      'catch-all'     -  server accepts everything (can't distinguish)
      'inconclusive'  -  connection refused, timeout, or ISP blocks port 25
    """
    domain = email.split('@', 1)[-1] if '@' in email else ''

    # Resolve MX if not provided
    if not mx_host:
        mx_host = _get_mx_host(domain)

    if not mx_host:
        logger.debug('No MX record for %s  -  marking inconclusive', domain)
        return 'inconclusive'

    try:
        # First: probe a known-bad address to detect catch-alls
        test_canary = f'zzzz_does_not_exist_xyz_99@{domain}'
        validated_url("https://" + mx_host)
        with smtplib.SMTP(mx_host, 25, timeout=_SMTP_TIMEOUT) as smtp:
            smtp.helo('outreach-signal.local')
            smtp.mail('')  # empty MAIL FROM (RFC 5321 compatible)

            code_canary, _ = smtp.rcpt(test_canary)
            if code_canary == 250:
                logger.debug('Catch-all domain detected: %s', domain)
                return 'catch-all'

            # Probe the real address
            time.sleep(0.5)
            code, _ = smtp.rcpt(email)
            if code == 250:
                return 'valid'
            elif code in (550, 551, 552, 553, 554):
                return 'invalid'
            else:
                return 'inconclusive'

    except (ConnectionRefusedError, smtplib.SMTPConnectError):
        logger.debug('SMTP port 25 blocked or refused for %s  -  ISP firewall likely', mx_host)
        return 'inconclusive'
    except socket.timeout:
        logger.debug('SMTP timeout for %s', mx_host)
        return 'inconclusive'
    except smtplib.SMTPServerDisconnected:
        return 'inconclusive'
    except OSError as exc:
        logger.debug('SMTP OS error for %s: %s', email, exc)
        return 'inconclusive'
    except Exception as exc:
        logger.warning('Unexpected SMTP error for %s: %s', email, exc)
        return 'inconclusive'


def guess_and_probe(first: str, last: str, domain: str,
                    smtp_probe: bool = True) -> list[dict]:
    """
    Generate email candidates and optionally SMTP-probe them.

    Returns list of {email, pattern, confidence, probe_result} dicts.
    probe_result is one of: 'valid' | 'invalid' | 'catch-all' | 'inconclusive' | 'skipped'

    If smtp_probe=False or port 25 is blocked, probe_result='skipped'/'inconclusive'.
    """
    candidates = guess_emails(first, last, domain)
    if not candidates:
        return []

    if not smtp_probe:
        for c in candidates:
            c['probe_result'] = 'skipped'
        return candidates

    # Resolve MX once for the domain
    mx_host = _get_mx_host(domain)
    if not mx_host:
        for c in candidates:
            c['probe_result'] = 'inconclusive'
        return candidates

    # Catch-all check with canary address first
    canary = f'zzzz_does_not_exist_xyz_99@{domain}'
    try:
        validated_url("https://" + mx_host)
        with smtplib.SMTP(mx_host, 25, timeout=_SMTP_TIMEOUT) as smtp:
            smtp.helo('outreach-signal.local')
            smtp.mail('')
            code, _ = smtp.rcpt(canary)
            if code == 250:
                logger.info('Catch-all domain %s  -  skipping real probes', domain)
                for c in candidates:
                    c['probe_result'] = 'catch-all'
                return candidates
    except (ConnectionRefusedError, smtplib.SMTPConnectError, socket.timeout, OSError) as exc:
        logger.debug('SMTP probe blocked for %s: %s', domain, exc)
        for c in candidates:
            c['probe_result'] = 'inconclusive'
        return candidates
    except Exception as exc:
        logger.warning('SMTP canary check error for %s: %s', domain, exc)
        for c in candidates:
            c['probe_result'] = 'inconclusive'
        return candidates

    # Probe real addresses (up to cap)
    probed = 0
    for c in candidates:
        if probed >= _MAX_PROBES_PER_DOMAIN:
            c['probe_result'] = 'skipped'
            continue

        time.sleep(_SMTP_DELAY)
        try:
            validated_url("https://" + mx_host)
            with smtplib.SMTP(mx_host, 25, timeout=_SMTP_TIMEOUT) as smtp:
                smtp.helo('outreach-signal.local')
                smtp.mail('')
                code, _ = smtp.rcpt(c['email'])
                if code == 250:
                    c['probe_result'] = 'valid'
                    # Boost confidence if SMTP confirms
                    c['confidence'] = min(0.95, c['confidence'] + 0.15)
                elif code in (550, 551, 552, 553, 554):
                    c['probe_result'] = 'invalid'
                    c['confidence'] = 0.0
                else:
                    c['probe_result'] = 'inconclusive'
            probed += 1
        except Exception as exc:
            logger.debug('Probe error for %s: %s', c['email'], exc)
            c['probe_result'] = 'inconclusive'
            probed += 1

    return candidates
