"""
VC enrichment orchestrator.

enrich_partner(partner_id) runs all enabled sources for a given partner,
saves results to the enrichment table, and returns a summary.

Source priority order (highest data quality first):
  1. pitchbook    -  gated by PITCHBOOK_ENABLED=1 env flag (default OFF)
  2. web_search   -  SearXNG + Brave fallback
  3. linkedin     -  Playwright HumanSession
  4. email_guess  -  SMTP pattern probing

Each source saves one row per URL (or one row for email_guess):
  - web_search: one row per unique URL with snippet + facts
  - linkedin: one row with scraped data + facts
  - email_guess: one row with candidate email list
  - pitchbook: one row per profile URL with scraped data + facts

After enrichment, partners.enriched_at and partners.linkedin_url /
partners.firm_domain are updated if discovered.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from vcoutreach.web.vendor import workspace_path, setting_value, validated_url
from typing import Optional

logger = logging.getLogger(__name__)


def _get_db_path() -> Path:
    try:
        from vcoutreach.web.config import DB_PATH
        return DB_PATH
    except ImportError:
        return workspace_path('data/outreach.db')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_partner(partner_id: int, conn: sqlite3.Connection) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT * FROM partners WHERE id = ?', (partner_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _upsert_enrichment_row(
    conn: sqlite3.Connection,
    partner_id: int,
    source: str,
    url: Optional[str],
    raw_text: Optional[str],
    facts_json: Optional[list],
    confidence: Optional[float],
) -> None:
    """Update evidence without erasing saved facts on an empty provider result."""
    if not facts_json:
        existing = conn.execute('SELECT facts_json FROM enrichment WHERE partner_id=? AND source=? AND url=?',
                                (partner_id, source, url or '')).fetchone()
        if existing and existing[0] and existing[0] != '[]':
            return
    facts_str = json.dumps(facts_json) if facts_json is not None else None
    raw_str = raw_text[:8000] if raw_text else None

    try:
        conn.execute(
            """
            INSERT INTO enrichment (partner_id, source, url, raw_text, facts_json,
                                    confidence, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(partner_id, source, url) DO UPDATE SET
                raw_text   = excluded.raw_text,
                facts_json = excluded.facts_json,
                confidence = excluded.confidence,
                fetched_at = excluded.fetched_at
            """,
            (partner_id, source, url or '', raw_str, facts_str, confidence, _now()),
        )
        conn.commit()
    except sqlite3.OperationalError as exc:
        logger.warning('enrichment upsert failed: %s', exc)


def _extract_domain_from_email(email: str) -> str | None:
    from vcoutreach.web.enrichment.email_guess import is_free_email_domain
    if '@' in email:
        domain = email.split('@', 1)[1].strip().lower()
        return None if is_free_email_domain(domain) else domain
    return None


def _run_web_search(partner: dict, partner_id: int, conn: sqlite3.Connection) -> dict:
    """Run web search enrichment and save rows. Returns summary."""
    name = partner.get('partner_name', '')
    firm = partner.get('firm', '')
    errors = []
    rows_written = 0
    total_results = 0
    quotable_results = 0

    try:
        from vcoutreach.web.enrichment.web_search import search_partner
        from vcoutreach.web.enrichment.fact_extractor import extract_facts

        logger.info('Web search: searching for %s at %s', name, firm)
        results = search_partner(name, firm, count_per_query=10)
        total_results = len(results)
        logger.info('Web search: got %d total deduplicated results', total_results)

        if not results:
            logger.warning('Web search: NO results for %s  -  SearXNG may be down', name)
            return {
                'rows_written': 0,
                'total_results': 0,
                'quotable_results': 0,
                'errors': ['SearXNG returned no results; search may be throttled or unavailable'],
            }

        # Group results into one enrichment row (with all results bundled)
        # and also save individual high-value rows per URL
        raw_data = {'results': results, 'partner_name': name, 'name': name}
        facts = extract_facts('web_search', raw_data)
        if not facts:
            errors.append('Search returned results but no usable fact snippets; saved facts were kept')
        logger.info('Web search: extracted %d facts from %d results', len(facts), total_results)

        # Determine overall confidence
        avg_conf = sum(f.get('confidence', 0) for f in facts) / max(len(facts), 1)

        # Save one row per unique result URL (for traceability)
        for r in results:
            url = r.get('url', '')
            snippet = r.get('snippet', '') or ''
            title = r.get('title', '') or ''
            raw_text = f'{title}\n{snippet}'.strip()

            # Per-URL facts
            url_facts = [f for f in facts if f.get('source_url') == url]
            url_conf = max((f.get('confidence', 0) for f in url_facts), default=0.0)

            # A result is "quotable" if it's from a writing domain or has high-confidence facts
            from vcoutreach.web.enrichment.fact_extractor import _is_writing_domain
            is_quotable = _is_writing_domain(url) or url_conf >= 0.70
            if is_quotable:
                quotable_results += 1

            _upsert_enrichment_row(
                conn, partner_id, 'web_search', url,
                raw_text[:2000], url_facts if url_facts else None,
                url_conf or 0.3,
            )
            rows_written += 1

        # Save a summary row with ALL facts under a special URL key
        _upsert_enrichment_row(
            conn, partner_id, 'web_search', '__summary__',
            f'Total: {total_results} results, {len(facts)} facts',
            facts, avg_conf,
        )

    except Exception as exc:
        logger.exception('Web search failed for partner %d', partner_id)
        errors.append(str(exc))

    return {
        'rows_written': rows_written,
        'total_results': total_results,
        'quotable_results': quotable_results,
        'errors': errors,
    }


def _run_linkedin(partner: dict, partner_id: int, conn: sqlite3.Connection) -> dict:
    """Run LinkedIn enrichment and save row. Returns summary."""
    name = partner.get('partner_name', '')
    errors = []
    linkedin_url = None

    try:
        from vcoutreach.web.enrichment.linkedin import scrape_partner
        from vcoutreach.web.enrichment.fact_extractor import extract_facts

        logger.info('LinkedIn: scraping for %s', name)
        data = scrape_partner(partner)

        error = data.get('error')
        if error:
            logger.warning('LinkedIn scrape error for %s: %s', name, error)
            # Still save what we got (URL at minimum)
            _upsert_enrichment_row(
                conn, partner_id, 'linkedin', data.get('url') or '',
                error, None, 0.0,
            )
            errors.append(error)
            return {
                'url': data.get('url'),
                'headline': '',
                'has_about': False,
                'errors': errors,
            }

        linkedin_url = data.get('url')
        headline = data.get('headline', '')
        current_role = data.get('current_role', '')
        about = data.get('about_excerpt', '')

        # Extract facts
        data['partner_name'] = name
        facts = extract_facts('linkedin', data)
        if not facts:
            errors.append('No usable profile facts returned; saved facts were kept')
        logger.info('LinkedIn: extracted %d facts for %s (headline=%r)',
                    len(facts), name, headline[:60] if headline else '')

        raw_text = f'Headline: {headline}\nRole: {current_role}\nAbout: {about}'.strip()
        confidence = 0.85 if (headline or about) else 0.3

        _upsert_enrichment_row(
            conn, partner_id, 'linkedin', linkedin_url or '',
            raw_text[:4000], facts if facts else None, confidence,
        )

        # Update partners.linkedin_url if found
        if linkedin_url:
            try:
                conn.execute(
                    'UPDATE partners SET linkedin_url = ? WHERE id = ? AND linkedin_url IS NULL',
                    (linkedin_url, partner_id),
                )
                conn.commit()
            except Exception as exc:
                logger.debug('Could not update linkedin_url: %s', exc)

        return {
            'url': linkedin_url,
            'headline': headline,
            'has_about': bool(about and len(about) > 30),
            'errors': errors,
        }

    except Exception as exc:
        logger.exception('LinkedIn enrichment failed for partner %d', partner_id)
        errors.append(str(exc))
        return {'url': None, 'headline': '', 'has_about': False, 'errors': errors}


def _run_email_guess(partner: dict, partner_id: int, conn: sqlite3.Connection) -> dict:
    """Run email guessing and save row. Returns summary."""
    name = partner.get('partner_name', '')
    email = partner.get('email', '')
    errors = []

    # Derive first/last from partner_name
    parts = name.strip().split()
    first = parts[0] if parts else ''
    last = parts[-1] if len(parts) > 1 else parts[0] if parts else ''

    # Derive domain from existing email or firm_domain
    from vcoutreach.web.enrichment.email_guess import is_free_email_domain
    domain = (partner.get('firm_domain') or '').strip().lower()
    if not domain and email and '@' in email:
        domain = email.split('@', 1)[1].strip().lower()

    if is_free_email_domain(domain or ''):
        return {'candidates': [], 'domain': domain, 'skipped': 'free-mail domain', 'errors': []}

    if not domain:
        logger.warning('No domain found for %s  -  skipping email guess', name)
        return {'candidates': [], 'domain': None, 'errors': ['No domain available']}

    try:
        from vcoutreach.web.enrichment.email_guess import guess_and_probe

        logger.info('Email guess: trying patterns for %s at %s', name, domain)
        candidates = guess_and_probe(first, last, domain, smtp_probe=True)

        logger.info('Email guess: %d candidates for %s@%s (probe results: %s)',
                    len(candidates), first,
                    domain,
                    {c['pattern']: c.get('probe_result', '?') for c in candidates})

        # Save as one enrichment row
        facts = [
            {
                'type': 'email_candidate',
                'text': f'{c["email"]} ({c["pattern"]}, confidence={c["confidence"]:.2f}, probe={c.get("probe_result", "skipped")})',
                'source_url': '',
                'confidence': c['confidence'],
            }
            for c in candidates
        ]

        best_conf = max((c['confidence'] for c in candidates), default=0.0)
        raw_text = '; '.join(
            f'{c["email"]} [{c.get("probe_result", "skipped")}]' for c in candidates
        )

        _upsert_enrichment_row(
            conn, partner_id, 'email_guess', domain,
            raw_text, facts, best_conf,
        )

        # Update partners.firm_domain if not set
        try:
            conn.execute(
                'UPDATE partners SET firm_domain = ? WHERE id = ? AND firm_domain IS NULL',
                (domain, partner_id),
            )
            conn.commit()
        except Exception as exc:
            logger.debug('Could not update firm_domain: %s', exc)

        return {
            'candidates': candidates,
            'domain': domain,
            'errors': errors,
        }

    except Exception as exc:
        logger.exception('Email guess failed for partner %d', partner_id)
        errors.append(str(exc))
        return {'candidates': [], 'domain': domain, 'errors': errors}


def _run_firm_website(partner: dict, partner_id: int, conn: sqlite3.Connection) -> dict:
    """Run firm-website scraper for partner  -  scrapes their VC firm's /team and /portfolio pages."""
    from vcoutreach.web.enrichment import firm_website
    result_summary: dict = {'facts': 0, 'errors': []}
    try:
        firm_name = partner.get('firm')
        # Derive first/last from partner_name (same approach as _run_email_guess)
        partner_name = partner.get('partner_name', '') or ''
        parts = partner_name.strip().split()
        first = parts[0] if parts else None
        # Strip suffixes like Jr., Sr., II, III before taking last name
        _SUFFIXES = {'jr.', 'jr', 'sr.', 'sr', 'ii', 'iii', 'iv'}
        raw_last = parts[-1].lower().rstrip('.,') if len(parts) > 1 else (parts[0] if parts else '')
        last = parts[-1] if (raw_last not in _SUFFIXES) else (parts[-2] if len(parts) > 2 else parts[-1])
        # Also use first_name column if present (derived field from DB)
        if partner.get('first_name'):
            first = partner['first_name']

        if not firm_name:
            return result_summary

        logger.info('Firm website: scraping %s for %s %s', firm_name, first, last)
        result = firm_website.enrich_firm(firm_name, partner_first=first, partner_last=last)
        if result and result.get('firm_url') and not firm_website.firm_site_matches(firm_name, result['firm_url']):
            result = {'verification_status': 'unverified', 'reason': 'Resolved website does not plausibly belong to ' + firm_name}
        if result and result.get('verification_status') == 'unverified':
            result_summary['verification_status'] = 'unverified'
            result_summary['errors'].append('Unverified firm website: ' + (result.get('reason') or 'No plausible official website found'))
            return result_summary
        if not result or not result.get('firm_url'):
            logger.info('Firm website: no URL found for %s', firm_name)
            return result_summary

        source_url = result.get('team_page_url') or result.get('firm_url')
        facts = firm_website.to_enrichment_facts(result, source_url)
        raw_text = (
            f"firm_url={result.get('firm_url')}\n"
            f"team_page={result.get('team_page_url')}\n"
            f"portfolio_page={result.get('portfolio_page_url')}\n"
            f"partner_bio={(result.get('partner_bio') or '')[:2000]}\n"
            f"firm_thesis={(result.get('firm_thesis') or '')[:2000]}\n"
        )
        _upsert_enrichment_row(
            conn, partner_id, 'firm_website', source_url or '',
            raw_text, facts, confidence=0.85,
        )
        result_summary['facts'] = len(facts)
        logger.info('Firm website: %d facts for %s', len(facts), firm_name)
    except Exception as exc:
        logger.exception('Firm website enrichment failed for partner %d', partner_id)
        result_summary['errors'].append(f'firm_website: {type(exc).__name__}: {str(exc)[:200]}')
    return result_summary


def _pb_profile_to_scrape_dict(profile: dict, name: str, url: str) -> dict:
    """
    Adapt a pitchbook_api.get_investor_profile() result into the scrape-shaped
    dict that fact_extractor._extract_from_pitchbook() consumes.

    The fact extractor reads the key 'recent_deals' (NOT 'investments') and each
    deal as {company, round, amount, date}. The curl_cffi API returns
    investments[] as {company, dealType, amount, currency, dealDate, ...}, so we
    remap field names here.

    Thesis/sectors/board_seats are intentionally omitted: the JSON API does not
    expose them. Deals + bio + contact are the signal.
    """
    investments = profile.get('investments', []) or []
    recent_deals = []
    for inv in investments:
        if not isinstance(inv, dict):
            continue
        amount = inv.get('amount')
        currency = inv.get('currency') or ''
        amount_str = ''
        if amount is not None:
            amount_str = f'{amount} {currency}'.strip() if currency else str(amount)
        recent_deals.append({
            'company': inv.get('company', '') or '',
            'round': inv.get('dealType', '') or '',
            'amount': amount_str,
            'date': inv.get('dealDate', '') or '',
        })

    return {
        'url': url,
        'partner_name': name,
        'bio': profile.get('bio', '') or '',
        'recent_deals': recent_deals,
    }


def _run_pitchbook(partner: dict, partner_id: int, conn: sqlite3.Connection) -> dict:
    """
    Run PitchBook enrichment and save row. Returns summary.

    Uses the curl_cffi JSON API (app.enrichment.pitchbook_api) via the
    module-level search_investor_by_name / get_investor_profile functions.
    Under run.py these resolve to the curl_cffi backend because app.config loads
    .env (PITCHBOOK_BACKEND=curl_cffi) before pitchbook_api is imported. NO
    headed browser is launched on this path.

    Requires PITCHBOOK_ENABLED=1 in environment. Gracefully handles:
      - No search result -> save marker row, return error
      - Session not loadable / HTTP error -> log, return error
      - Any other exception -> log, return error

    Return contract: {url, bio, deals_count, errors, sparse}.

    `sparse` is True when PB returned no contact data (no email AND no phone)
    AND a thin/generic bio or no investments. Sparse results still persist any
    real deals but add a pitchbook_data_sparse marker fact and carry a lower row
    confidence so downstream auto-approve / hook-gen does not over-trust them.
    """
    name = partner.get('partner_name', '')
    firm = partner.get('firm', '')
    errors = []
    pitchbook_url = None

    # Guard: PITCHBOOK_ENABLED must be set
    if os.environ.get('PITCHBOOK_ENABLED', '0') != '1':
        return {
            'url': None,
            'bio': '',
            'deals_count': 0,
            'errors': ['PITCHBOOK_ENABLED not set - skipping PitchBook enrichment'],
        }

    try:
        # NOTE: import from pitchbook_api (curl_cffi JSON API), NOT
        # app.enrichment.pitchbook (the Playwright headed browser). The
        # module-level names below are the curl_cffi implementations under
        # run.py because .env pins PITCHBOOK_BACKEND=curl_cffi.
        from vcoutreach.web.enrichment.pitchbook_api import (
            search_investor_by_name,
            get_investor_profile,
        )
        from vcoutreach.web.enrichment.fact_extractor import extract_facts

        logger.info('PitchBook: enriching %s at %s (curl_cffi API)', name, firm)

        # 1. Resolve the PitchBook profile id by name.
        search_result = search_investor_by_name(name)
        if not search_result or not search_result.get('id'):
            msg = f'PitchBook: no search result found for {name}'
            logger.warning(msg)
            errors.append(msg)
            _upsert_enrichment_row(conn, partner_id, 'pitchbook', '', msg, None, 0.2)
            return {'url': None, 'bio': '', 'deals_count': 0, 'errors': errors}

        pitchbook_id = search_result.get('id')
        pitchbook_url = f'https://my.pitchbook.com/profile/{pitchbook_id}/investor/profile'

        # 2. Fetch full profile (bio, contact, deals).
        profile = get_investor_profile(pitchbook_id)

        # 3. Adapt to the scrape-shaped dict the fact extractor consumes.
        #    This yields the pitchbook_recent_deal facts (deals are the signal).
        scrape_dict = _pb_profile_to_scrape_dict(profile, name, pitchbook_url)
        facts = extract_facts('pitchbook', scrape_dict)

        bio = scrape_dict.get('bio', '')
        recent_deals = scrape_dict.get('recent_deals', [])

        # Contact data: prefer profile contact-info, fall back to search result.
        pb_email_raw = (profile.get('email') or search_result.get('email') or '').strip()
        pb_phone_raw = (profile.get('phone') or '').strip()
        pb_linkedin_raw = (
            profile.get('linkedIn') or search_result.get('linkedIn') or ''
        ).strip()
        current_role = (profile.get('primaryPosition') or '').strip()
        if not current_role:
            current_role = (search_result.get('typeDescription') or '').strip()
        total_investments = profile.get('totalInvestmentsCount')

        # Append discrete contact/bio/role facts so they reach the LLM via
        # get_enrichment_facts (which reads facts_json only, NOT raw_text).
        # Mirrors the named no-regression source types: pitchbook_bio_snippet,
        # pitchbook_current_role, pitchbook_total_investments, pitchbook_email,
        # pitchbook_phone. Shape matches _extract_from_pitchbook
        # ({type, text, source_url, confidence}) so the text-keyed dedup in
        # facts_to_llm_format cannot silently drop them.
        contact_facts = []
        if bio:
            contact_facts.append({
                'type': 'pitchbook_bio_snippet',
                'text': bio[:800],
                'source_url': pitchbook_url,
                'confidence': 0.90,
            })
        if current_role:
            contact_facts.append({
                'type': 'pitchbook_current_role',
                'text': current_role,
                'source_url': pitchbook_url,
                'confidence': 0.90,
            })
        if total_investments is not None:
            contact_facts.append({
                'type': 'pitchbook_total_investments',
                'text': str(total_investments),
                'source_url': pitchbook_url,
                'confidence': 0.85,
            })
        if pb_email_raw:
            contact_facts.append({
                'type': 'pitchbook_email',
                'text': pb_email_raw,
                'source_url': pitchbook_url,
                'confidence': 0.95,
            })
        if pb_phone_raw:
            contact_facts.append({
                'type': 'pitchbook_phone',
                'text': pb_phone_raw,
                'source_url': pitchbook_url,
                'confidence': 0.90,
            })
        # SPARSE-PROFILE DETECTION.
        # A PB result is "sparse" when it carries no usable contact data
        # (no email AND no phone) AND the bio is thin or generic. Empty contact
        # is the hard gate, so a rich-bio partner that has an email (partner 3)
        # never reaches the bio test. The bio test uses a length floor as the
        # primary discriminator (a 43-char generic stub vs a 1000-char real bio
        # separate cleanly); the generic-phrase check is anchored to the whole
        # stripped bio so it cannot false-positive on a long bio that merely
        # mentions "is an Angel Investor" mid-paragraph.
        bio_stripped = (bio or '').strip()
        thin_bio = (
            len(bio_stripped) < 60
            or bio_stripped.endswith('is an Angel Investor.')
        )
        has_contact = bool(pb_email_raw or pb_phone_raw)
        sparse = (not has_contact) and (thin_bio or not total_investments)

        if sparse:
            contact_facts.append({
                'type': 'pitchbook_data_sparse',
                'text': (
                    'PitchBook profile sparse: no contact data, thin bio. '
                    'Rely on firm_website / web_search for this partner.'
                ),
                'source_url': pitchbook_url,
                # Sits at the facts_to_llm_format inclusion floor (0.55) so the
                # marker actually reaches hook-gen as guidance. This per-fact
                # confidence is independent of the row confidence below, which
                # stays low (0.35) for auto-approve gating.
                'confidence': 0.55,
            })

        facts = (facts or []) + contact_facts
        logger.info(
            'PitchBook: extracted %d facts for %s (sparse=%s)',
            len(facts), name, sparse,
        )

        if sparse:
            # Lower row confidence so downstream auto-approve / hook-gen does
            # not over-trust a near-empty PB profile.
            confidence = 0.35
        else:
            confidence = 0.90 if (bio or recent_deals) else 0.50

        raw_text = (
            f'Bio: {bio[:500]}\n'
            f'Deals: {len(recent_deals)}\n'
            f'Email: {pb_email_raw}\n'
            f'Phone: {pb_phone_raw}\n'
            f'LinkedIn: {pb_linkedin_raw}\n'
            f'Current role: {current_role}\n'
            f'Total investments: {total_investments}'
        )

        _upsert_enrichment_row(
            conn, partner_id, 'pitchbook', pitchbook_url,
            raw_text[:4000], facts if facts else None, confidence,
        )

        # Persist email / linkedin to partners columns, mirroring pb_backfill_email.
        _pb_persist_contact(
            conn, partner_id, partner, name,
            pb_email_raw, pb_linkedin_raw, errors,
        )

        # Update partners table with PitchBook ID and last-scraped time.
        if pitchbook_id:
            try:
                conn.execute(
                    """
                    UPDATE partners
                    SET pitchbook_id = ?, pitchbook_last_scraped_at = ?
                    WHERE id = ?
                    """,
                    (pitchbook_id, _now(), partner_id),
                )
                conn.commit()
            except Exception as exc_upd:
                logger.debug('Could not update pitchbook_id: %s', exc_upd)

        return {
            'url': pitchbook_url,
            'bio': bio[:200] if bio else '',
            'deals_count': len(recent_deals),
            'errors': errors,
            'sparse': sparse,
        }

    except Exception as exc:
        logger.exception('PitchBook enrichment failed for partner %d', partner_id)
        errors.append(str(exc))
        try:
            _upsert_enrichment_row(
                conn, partner_id, 'pitchbook', pitchbook_url or '',
                f'PitchBook error: {type(exc).__name__}: {str(exc)[:200]}', None, 0.0,
            )
        except Exception:
            pass
        return {'url': pitchbook_url, 'bio': '', 'deals_count': 0, 'errors': errors}


def _pb_persist_contact(
    conn: sqlite3.Connection,
    partner_id: int,
    partner: dict,
    name: str,
    pb_email_raw: str,
    pb_linkedin_raw: str,
    errors: list,
) -> None:
    """
    Persist PitchBook-sourced email / linkedin to the partners table, mirroring
    pb_backfill_email() so the curl_cffi path loses no contact data vs the old
    browser path.

      - Always writes pb_email / pb_email_source (when email is valid).
      - Writes pb_linkedin and backfills linkedin_url when empty.
      - Overwrites partners.email only when current status is NOT trusted.
    """
    pb_email_valid = bool(pb_email_raw and _EMAIL_RE.match(pb_email_raw))
    current_ev_status = partner.get('email_verification_status')

    # pb_email column
    try:
        conn.execute(
            """
            UPDATE partners
            SET pb_email = ?, pb_email_source = ?
            WHERE id = ?
            """,
            (
                pb_email_raw if pb_email_valid else None,
                'search' if pb_email_valid else None,
                partner_id,
            ),
        )
        conn.commit()
    except Exception as exc:
        errors.append(f'pb_email column write failed: {exc}')

    # pb_linkedin column + linkedin_url backfill
    if pb_linkedin_raw:
        try:
            conn.execute(
                'UPDATE partners SET pb_linkedin = ? WHERE id = ?',
                (pb_linkedin_raw, partner_id),
            )
            conn.execute(
                """
                UPDATE partners SET linkedin_url = ?
                WHERE id = ? AND (linkedin_url IS NULL OR linkedin_url = '')
                """,
                (pb_linkedin_raw, partner_id),
            )
            conn.commit()
        except Exception as exc:
            errors.append(f'pb_linkedin column write failed: {exc}')

    # Conditional partners.email overwrite (never clobber a trusted email)
    if pb_email_valid and current_ev_status not in _TRUSTED_STATUSES:
        try:
            conn.execute(
                """
                UPDATE partners
                SET email = ?, email_verification_status = 'pb_verified'
                WHERE id = ?
                """,
                (pb_email_raw, partner_id),
            )
            conn.commit()
            logger.info(
                'PitchBook: updated email for %s (id=%d) -> %s (was status=%s)',
                name, partner_id, pb_email_raw, current_ev_status,
            )
        except Exception as exc:
            errors.append(f'email column write failed: {exc}')


_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

# Statuses considered trustworthy enough that we do NOT overwrite partners.email
_TRUSTED_STATUSES = frozenset({'valid', 'deliverable'})

# Statuses that are weak / unverified  -  safe to overwrite with a PB-sourced email
_WEAK_STATUSES = frozenset({
    None, 'inconclusive', 'mx_only', 'mx_pattern_match', 'risky', 'invalid',
})


def pb_backfill_email(partner_id: int, db_path: Path | None = None) -> dict:
    """
    Look up a partner by name in PitchBook, extract any direct email PB returns,
    and write it to partners.pb_email.  If the partner's current email is weak
    (null / pattern-guess / unverified) AND the PB email looks valid, also set
    partners.email = pb_email and email_verification_status = 'pb_verified'.

    Emailable-verified 'valid'/'deliverable' emails are NEVER overwritten.

    Throttle: caller is responsible for sleeping between successive calls.

    Returns dict:
        {
          'partner_id': int,
          'partner_name': str,
          'pb_email': str | None,
          'pb_email_source': str | None,
          'pb_linkedin': str | None,
          'email_updated': bool,
          'linkedin_updated': bool,
          'errors': list[str],
        }
    """
    db_path = db_path or _get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')

    result: dict = {
        'partner_id': partner_id,
        'partner_name': '',
        'pb_email': None,
        'pb_email_source': None,
        'pb_linkedin': None,
        'email_updated': False,
        'linkedin_updated': False,
        'errors': [],
    }

    try:
        partner = _get_partner(partner_id, conn)
        if partner is None:
            result['errors'].append(f'Partner {partner_id} not found')
            return result

        name = partner.get('partner_name', '')
        result['partner_name'] = name
        current_ev_status = partner.get('email_verification_status')

        try:
            from vcoutreach.web.enrichment.pitchbook_api import search_investor_by_name
        except ImportError as exc:
            result['errors'].append(f'pitchbook_api import failed: {exc}')
            return result

        # Search PB for this investor
        try:
            pb_data = search_investor_by_name(name)
        except Exception as exc:
            result['errors'].append(f'search_investor_by_name failed: {exc}')
            return result

        if pb_data is None:
            logger.info('pb_backfill_email: no PB result for %s (id=%d)', name, partner_id)
            return result

        pb_email_raw = (pb_data.get('email') or '').strip()
        pb_linkedin_raw = (pb_data.get('linkedIn') or '').strip()
        pb_email_source = 'search'

        # Validate email with basic regex
        pb_email_valid = bool(pb_email_raw and _EMAIL_RE.match(pb_email_raw))

        result['pb_email'] = pb_email_raw if pb_email_valid else None
        result['pb_email_source'] = pb_email_source if pb_email_valid else None
        result['pb_linkedin'] = pb_linkedin_raw or None

        # Always write pb_email + pb_email_source to the dedicated column
        try:
            conn.execute(
                """
                UPDATE partners
                SET pb_email = ?,
                    pb_email_source = ?
                WHERE id = ?
                """,
                (pb_email_raw if pb_email_valid else None,
                 pb_email_source if pb_email_valid else None,
                 partner_id),
            )
            conn.commit()
        except Exception as exc:
            result['errors'].append(f'pb_email column write failed: {exc}')

        # Write pb_linkedin if present and partners.linkedin_url is empty
        if pb_linkedin_raw:
            try:
                conn.execute(
                    """
                    UPDATE partners SET pb_linkedin = ?
                    WHERE id = ?
                    """,
                    (pb_linkedin_raw, partner_id),
                )
                # Also backfill linkedin_url if missing
                conn.execute(
                    """
                    UPDATE partners SET linkedin_url = ?
                    WHERE id = ? AND (linkedin_url IS NULL OR linkedin_url = '')
                    """,
                    (pb_linkedin_raw, partner_id),
                )
                conn.commit()
                result['linkedin_updated'] = True
            except Exception as exc:
                result['errors'].append(f'pb_linkedin column write failed: {exc}')

        # Conditionally overwrite partners.email
        if pb_email_valid:
            if current_ev_status not in _TRUSTED_STATUSES:
                # Weak or unverified email  -  safe to overwrite
                try:
                    conn.execute(
                        """
                        UPDATE partners
                        SET email = ?,
                            email_verification_status = 'pb_verified'
                        WHERE id = ?
                        """,
                        (pb_email_raw, partner_id),
                    )
                    conn.commit()
                    result['email_updated'] = True
                    logger.info(
                        'pb_backfill_email: updated email for %s (id=%d) -> %s (was status=%s)',
                        name, partner_id, pb_email_raw, current_ev_status,
                    )
                except Exception as exc:
                    result['errors'].append(f'email column write failed: {exc}')
            else:
                logger.info(
                    'pb_backfill_email: skipping email overwrite for %s (id=%d)  -  '
                    'current status=%s is trusted',
                    name, partner_id, current_ev_status,
                )

        return result

    finally:
        conn.close()


def get_enrichment_facts(partner_id: int, db_path: Path | None = None, *, limit: int | None = 10) -> list[dict]:
    """
    Retrieve all facts for a partner from the enrichment table.

    Returns a list of {source, text} dicts ready for build_hook_prompt().
    """
    db_path = db_path or _get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            """
            SELECT source, url, facts_json, confidence
            FROM enrichment
            WHERE partner_id = ?
              AND source != 'email_guess'
              AND facts_json IS NOT NULL
            ORDER BY confidence DESC
            """,
            (partner_id,),
        ).fetchall()

        all_facts: list[dict] = []
        for row in rows:
            facts_json = row['facts_json']
            if not facts_json:
                continue
            try:
                facts = json.loads(facts_json)
                if isinstance(facts, list):
                    all_facts.extend({**f, 'provider': row['source'], 'source_url': f.get('source_url') or row['url']}
                                     for f in facts if isinstance(f, dict))
            except Exception:
                pass

        from vcoutreach.web.enrichment.fact_extractor import facts_to_llm_format
        return facts_to_llm_format(all_facts, limit=limit)

    finally:
        conn.close()


def enrich_partner(
    partner_id: int,
    sources: list[str] | None = None,
    db_path: Path | None = None,
) -> dict:
    """
    Run enrichment for a VC partner.

    Args:
        partner_id: the partners.id to enrich
        sources: list of sources to run in priority order:
                 'pitchbook', 'web_search', 'linkedin', 'email_guess'
                 or None to run all (pitchbook only if PITCHBOOK_ENABLED=1)
        db_path: override DB path

    Returns:
        dict with: partner_name, sources_run, facts_count, errors, summary_per_source
    """
    if sources is None:
        # PitchBook first (richest), but gated by env flag
        sources = ['pitchbook', 'firm_website', 'web_search', 'linkedin', 'email_guess']

    db_path = db_path or _get_db_path()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')

    try:
        partner = _get_partner(partner_id, conn)
        if partner is None:
            return {'error': f'Partner {partner_id} not found'}

        name = partner.get('partner_name', 'Unknown')
        before = {(row['source'], row['url']): row['facts_json'] for row in conn.execute(
            'SELECT source,url,facts_json FROM enrichment WHERE partner_id=? AND facts_json IS NOT NULL', (partner_id,))}
        logger.info('=== Enriching partner %d: %s ===', partner_id, name)

        summary: dict = {
            'partner_id': partner_id,
            'partner_name': name,
            'sources_run': [],
            'facts_count': 0,
            'errors': [],
            'per_source': {},
        }

        # Run each source  -  PitchBook first (richest), gated by env flag
        if 'pitchbook' in sources:
            pb_enabled = os.environ.get('PITCHBOOK_ENABLED', '0') == '1'
            if pb_enabled:
                logger.info('--- Source: pitchbook ---')
                pb_result = _run_pitchbook(partner, partner_id, conn)
                summary['sources_run'].append('pitchbook')
                summary['per_source']['pitchbook'] = pb_result
                summary['errors'].extend(pb_result.get('errors', []))
            else:
                logger.debug('PitchBook skipped (PITCHBOOK_ENABLED != 1)')

        if 'web_search' in sources:
            logger.info('--- Source: web_search ---')
            ws_result = _run_web_search(partner, partner_id, conn)
            summary['sources_run'].append('web_search')
            summary['per_source']['web_search'] = ws_result
            summary['errors'].extend(ws_result.get('errors', []))

        if 'linkedin' in sources:
            logger.info('--- Source: linkedin ---')
            li_result = _run_linkedin(partner, partner_id, conn)
            summary['sources_run'].append('linkedin')
            summary['per_source']['linkedin'] = li_result
            summary['errors'].extend(li_result.get('errors', []))

        if 'email_guess' in sources:
            logger.info('--- Source: email_guess ---')
            eg_result = _run_email_guess(partner, partner_id, conn)
            summary['sources_run'].append('email_guess')
            summary['per_source']['email_guess'] = eg_result
            summary['errors'].extend(eg_result.get('errors', []))

        if 'firm_website' in sources:
            logger.info('--- Source: firm_website ---')
            fw_result = _run_firm_website(partner, partner_id, conn)
            summary['sources_run'].append('firm_website')
            summary['per_source']['firm_website'] = fw_result
            summary['errors'].extend(fw_result.get('errors', []))

        # Report provider names and actual usable facts, without the prompt's ten-fact cap.
        summary['errors'] = [f'{source}: {error}' for source, result in summary['per_source'].items()
                             for error in result.get('errors', [])]
        summary['preserved_sources'] = []
        for source in summary['sources_run']:
            if not summary['per_source'][source].get('errors'):
                continue
            after = {row['url']: row['facts_json'] for row in conn.execute(
                'SELECT url,facts_json FROM enrichment WHERE partner_id=? AND source=?', (partner_id, source))}
            if any(saved and saved != '[]' and after.get(url) == saved
                   for (provider, url), saved in before.items() if provider == source):
                summary['preserved_sources'].append(source)
        summary['facts_count'] = len(get_enrichment_facts(partner_id, db_path=db_path, limit=None))

        # Mark partner as enriched
        try:
            conn.execute(
                'UPDATE partners SET enriched_at = ? WHERE id = ?',
                (_now(), partner_id),
            )
            conn.commit()
        except Exception as exc:
            logger.debug('Could not update enriched_at: %s', exc)

        logger.info('=== Enrichment complete for %s: %d fact rows ===',
                    name, summary['facts_count'])
        return summary

    finally:
        conn.close()
