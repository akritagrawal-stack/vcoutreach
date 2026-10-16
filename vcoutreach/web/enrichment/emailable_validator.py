"""
Emailable.com single-verify API integration.

Docs: https://emailable.com/docs/api/#verify-an-email-address

Usage:
    from vcoutreach.web.enrichment.emailable_validator import EmailableValidator
    v = EmailableValidator()
    result = v.verify('john.doe@example.com')
    # {'state': 'deliverable', 'score': 99, 'reason': 'accepted_email', ...}

State mapping to internal vocabulary:
    deliverable  → 'valid'        (confidence 0.95)
    risky        → 'risky'        (confidence 0.50)
    undeliverable→ 'invalid'      (confidence 0.00)
    unknown      → 'inconclusive' (confidence 0.30)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import urllib.request
import urllib.parse
import urllib.error
import json
from vcoutreach.web.vendor import setting_value, credential_value, fixed_urlopen

logger = logging.getLogger(__name__)

# Throttle: max 5 req/sec (Emailable free tier allows 10/sec; we're conservative)
_MIN_INTERVAL = 0.20  # seconds between calls
_RETRY_SLEEP = 2.0    # seconds before retry after 5xx

# Map Emailable states → internal vocabulary
_STATE_MAP = {
    'deliverable':   ('valid',        0.95),
    'risky':         ('risky',        0.50),
    'undeliverable': ('invalid',      0.00),
    'unknown':       ('inconclusive', 0.30),
}


class EmailableValidator:
    """
    Wraps the Emailable v1/verify endpoint.

    Thread-safe as long as each thread uses its own instance.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or setting_value('EMAILABLE_API_KEY')
        if not self.api_key:
            raise ValueError(
                'EMAILABLE_API_KEY not set. Add it to .env or pass api_key= explicitly.'
            )
        self._last_call: float = 0.0
        self.api_key = credential_value(self.api_key)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)

    def _get(self, email: str) -> dict:
        """
        Make a single GET to Emailable v1/verify.
        Returns parsed JSON dict.
        Raises RuntimeError for fatal errors (401, 402).
        Raises urllib.error.HTTPError for retriable errors (5xx, 429).
        """
        params = urllib.parse.urlencode({'email': email, 'api_key': self.api_key})
        url = f'https://api.emailable.com/v1/verify?{params}'
        req = urllib.request.Request(url, headers={'User-Agent': 'outreach-signal.local/1.0'})

        logger.debug('Emailable verification requested')
        self._throttle()
        self._last_call = time.monotonic()

        try:
            with fixed_urlopen(req, timeout=15) as resp:
                raw = resp.read(1048576).decode('utf-8')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = ''
            try:
                body = exc.read(16384).decode('utf-8')
            except Exception:
                pass

            if status == 401:
                raise RuntimeError(
                    'Emailable rejected the configured key'
                ) from exc
            if status == 402:
                raise RuntimeError(
                    'Emailable 402 Payment Required  -  credits exhausted'
                ) from exc
            if status == 429:
                logger.warning('Emailable 429 rate-limit hit  -  sleeping 2s')
                time.sleep(_RETRY_SLEEP)
                raise  # caller decides whether to retry
            # 5xx and other: re-raise for retry logic above
            logger.warning('Emailable HTTP %d', status)
            raise

    def verify(self, email: str) -> dict:
        """
        Verify a single email address via Emailable.

        Returns:
            {
              'state':             str,   # 'deliverable'|'risky'|'undeliverable'|'unknown'
              'score':             int,   # 0-100
              'reason':            str,   # e.g. 'accepted_email', 'rejected_email'
              'free_email':        bool,
              'role_email':        bool,
              'accept_all':        bool,
              'did_you_mean':      str|None,
              'credits_remaining': int|None,
              'internal_status':   str,   # mapped: 'valid'|'risky'|'invalid'|'inconclusive'
              'confidence':        float,
              'error':             str|None,  # set only on API/network error
            }
        """
        try:
            data = self._get(email)
        except RuntimeError:
            raise  # fatal  -  propagate immediately
        except Exception as exc:
            # Retry once on transient errors (5xx, 429, network)
            logger.info('Emailable request failed; retrying once')
            time.sleep(_RETRY_SLEEP)
            try:
                self._last_call = 0.0  # reset throttle to retry immediately after sleep
                data = self._get(email)
            except RuntimeError:
                raise
            except Exception as exc2:
                logger.error('Emailable request failed after retry')
                return {
                    'state':             'unknown',
                    'score':             0,
                    'reason':            'Emailable API unavailable after retry',
                    'free_email':        False,
                    'role_email':        False,
                    'accept_all':        False,
                    'did_you_mean':      None,
                    'credits_remaining': None,
                    'internal_status':   'inconclusive',
                    'confidence':        0.30,
                    'error':             'provider_request_failed',
                }

        emailable_state = data.get('state', 'unknown')
        internal_status, confidence = _STATE_MAP.get(
            emailable_state, ('inconclusive', 0.30)
        )

        return {
            'state':             emailable_state,
            'score':             data.get('score', 0),
            'reason':            data.get('reason', ''),
            'free_email':        bool(data.get('free_email', False)),
            'role_email':        bool(data.get('role_email', False)),
            'accept_all':        bool(data.get('accept_all', False)),
            'did_you_mean':      data.get('did_you_mean') or None,
            'credits_remaining': data.get('credits', None),
            'internal_status':   internal_status,
            'confidence':        confidence,
            'error':             None,
        }
