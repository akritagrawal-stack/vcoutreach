"""
Free email verification for known email addresses.

Orchestrates:
  1. Regex syntax check
  2. MX record lookup (dnspython)
  3. Catch-all detection (SMTP canary probe)
  4. SMTP RCPT TO probe on the real address
  5. Graceful degrade if port 25 is blocked (mx_only confidence ~0.6)

Does NOT send any actual email. Does NOT use paid APIs.

Status values returned:
  'valid'              -  SMTP 250 accepted, not catch-all
  'invalid'            -  SMTP 5xx rejected
  'catch_all'          -  domain accepts any address (can't confirm)
  'inconclusive'       -  port blocked / timeout / grey-listed
  'mx_only'            -  MX exists but no SMTP probe attempted / all probes inconclusive
  'mx_pattern_match'   -  MX-verified domain plus local-part matches standard firm address
                        pattern (first, first.last, f.last, etc.). Confidence ~0.72.
                        Set by scripts/upgrade_mx_pattern.py when port 25 is blocked.

Usage:
    from vcoutreach.web.enrichment.email_verifier import verify_email
    result = verify_email('john.doe@example.com')
    # {'valid': bool, 'confidence': 0.0-1.0, 'status': str, 'method': str, 'reason': str}
"""
from __future__ import annotations

import logging
import re
import smtplib
from vcoutreach.web.vendor import validated_url, normalized_email
import socket
import time

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_SMTP_TIMEOUT = 5  # seconds (reduced from 8; port 25 ISP block gives timeout quickly anyway)
_CANARY_PREFIX = 'zzzz_does_not_exist_xyz_99'
_PROBE_HELO = 'outreach-signal.local'


def _get_mx_host(domain: str) -> str | None:
    """Resolve the lowest-preference MX record for a domain."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, 'MX')
        sorted_ans = sorted(answers, key=lambda r: r.preference)
        if sorted_ans:
            return str(sorted_ans[0].exchange).rstrip('.')
    except ImportError:
        logger.warning('Email MX checks need dnspython. Run: pip install -e \".[integrations]\"')
    except Exception as exc:
        logger.debug('MX lookup failed for %s: %s', domain, exc)
    return None


def smtp_probe(email: str, mx_host: str, timeout: int = _SMTP_TIMEOUT) -> dict:
    """
    Connect to mx_host:25 and probe the address via RCPT TO.

    Returns:
        {status: 'accepted'|'rejected'|'inconclusive', code: int, message: str}
    """
    try:
        validated_url("https://" + mx_host)
        with smtplib.SMTP(mx_host, 25, timeout=timeout) as smtp:
            smtp.helo(_PROBE_HELO)
            smtp.mail('')  # RFC 5321 null reverse-path
            code, msg = smtp.rcpt(email)
            msg_str = msg.decode('utf-8', errors='replace') if isinstance(msg, bytes) else str(msg)
            if code == 250:
                return {'status': 'accepted', 'code': code, 'message': msg_str}
            elif code in (550, 551, 552, 553, 554):
                return {'status': 'rejected', 'code': code, 'message': msg_str}
            else:
                return {'status': 'inconclusive', 'code': code, 'message': msg_str}
    except (ConnectionRefusedError, smtplib.SMTPConnectError) as exc:
        return {'status': 'inconclusive', 'code': 0, 'message': f'Connection refused: {exc}'}
    except socket.timeout:
        return {'status': 'inconclusive', 'code': 0, 'message': 'Timeout'}
    except smtplib.SMTPServerDisconnected as exc:
        return {'status': 'inconclusive', 'code': 0, 'message': f'Server disconnected: {exc}'}
    except OSError as exc:
        return {'status': 'inconclusive', 'code': 0, 'message': f'OS error: {exc}'}
    except Exception as exc:
        return {'status': 'inconclusive', 'code': 0, 'message': f'Unexpected: {exc}'}


def check_catch_all(domain: str, mx_host: str, timeout: int = _SMTP_TIMEOUT) -> bool:
    """
    Probe a random impossible address. Returns True if the domain is catch-all.
    """
    canary = f'{_CANARY_PREFIX}@{domain}'
    result = smtp_probe(canary, mx_host, timeout=timeout)
    return result['status'] == 'accepted'


def verify_email(email: str) -> dict:
    """
    Verify a known email address using free methods only.

    Returns:
        {
          'valid': bool,
          'confidence': float (0.0-1.0),
          'status': str,   # 'valid' | 'invalid' | 'catch_all' | 'inconclusive' | 'mx_only'
          'method': str,   # e.g. 'smtp_rcpt' | 'mx_only' | 'syntax_fail' | 'no_mx'
          'reason': str,   # human-readable explanation
        }
    """
    email = email.strip().lower()

    # 1. Syntax check
    if not _EMAIL_RE.match(email):
        return {
            'valid': False,
            'confidence': 0.0,
            'status': 'invalid',
            'method': 'syntax_fail',
            'reason': f'Email failed regex syntax check: {email}',
        }

    domain = email.split('@', 1)[1]

    # 2. MX lookup
    mx_host = _get_mx_host(domain)
    if not mx_host:
        return {
            'valid': False,
            'confidence': 0.1,
            'status': 'inconclusive',
            'method': 'no_mx',
            'reason': f'No MX record found for domain: {domain}',
        }

    # 3. Catch-all detection (canary probe)
    try:
        is_catch_all = check_catch_all(domain, mx_host)
    except Exception:
        is_catch_all = False
        # If canary probe itself fails, treat as inconclusive later

    if is_catch_all:
        return {
            'valid': None,      # can't confirm either way
            'confidence': 0.5,
            'status': 'catch_all',
            'method': 'smtp_catch_all',
            'reason': f'Domain {domain} accepts all addresses (catch-all). Cannot confirm {email}.',
        }

    # 4. SMTP RCPT TO probe on the real address
    time.sleep(0.3)  # small pause after canary
    result = smtp_probe(email, mx_host)

    if result['status'] == 'accepted':
        return {
            'valid': True,
            'confidence': 0.92,
            'status': 'valid',
            'method': 'smtp_rcpt',
            'reason': f'SMTP 250 accepted by {mx_host} (code {result["code"]})',
        }
    elif result['status'] == 'rejected':
        return {
            'valid': False,
            'confidence': 0.95,
            'status': 'invalid',
            'method': 'smtp_rcpt',
            'reason': f'SMTP {result["code"]} rejected by {mx_host}: {result["message"][:120]}',
        }
    else:
        # Port 25 blocked / timeout / grey-listed → MX exists, degrade gracefully
        return {
            'valid': None,
            'confidence': 0.55,
            'status': 'mx_only',
            'method': 'mx_only',
            'reason': (
                f'MX record exists ({mx_host}) but SMTP probe inconclusive: '
                f'{result["message"][:100]}. Port 25 may be blocked (ISP firewall).'
            ),
        }
