"""Read-only, evidence-backed alternate contacts at an existing investor firm.

This module only searches local CRM and saved research records. Its results are
research leads, never permission to send or a claim that an address is valid.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from vcoutreach.web.config import DB_PATH, WorkspacePath, get_workspace

DEFAULT_RESEARCH_ROOT = WorkspacePath('outputs', 'research')

# Official firm websites for the source-reviewed cohort. Never derive a URL to
# fetch from an email address or an untrusted search result inside a web route.
OFFICIAL_FIRM_SITES = {}


def _firm_key(value: str | None) -> str:
    # Normalize punctuation, but retain words such as Capital and Ventures.
    # Removing those words would merge unrelated funds.
    return ' '.join(re.findall(r'[a-z0-9]+', value.casefold())) if isinstance(value, str) else ''


def _name_key(value: str | None) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', value.casefold())) if isinstance(value, str) else ''


def _source_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == 'https' and parsed.netloc else None


def _looks_like_team_roster_url(value: str) -> bool:
    """Reject a firm's editorial article when navigation matching found it by accident."""
    path = urlparse(value).path.casefold()
    return bool(re.search(r'/(team|people|partners|investors|investment-team)(?:/|$)', path))


def _read_research(root: Path, firm_key: str):
    if not root.is_dir():
        return
    paths = sorted(root.glob('RESEARCH_WAVE_*.json'))
    paths.extend(sorted(root.glob('*/FRESH_RESEARCH.json')))
    for path in paths:
        try:
            # Bound local parsing so a malformed export cannot stall the CRM.
            if path.stat().st_size > 2_000_000:
                continue
            path = get_workspace().path(path)
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        records = payload.get('records', []) if isinstance(payload, dict) else []
        if not isinstance(records, list):
            continue
        for record in records:
            if (isinstance(record, dict) and _firm_key(record.get('firm')) == firm_key
                    and _name_key(record.get('name'))):
                yield record, str(path)


def _official_grouped_names(firm_key: str, homepage: str) -> list[dict]:
    return []


def _official_linked_names(firm_key: str, team_url: str) -> list[dict]:
    return []


def find_alternate_investors(
    partner_id: int,
    *,
    db_path: Path | None = None,
    research_root: Path | None = None,
    include_official_team: bool = False,
) -> dict:
    """Find named same-firm contacts and expose their provenance and outreach flags.

    By default this is local only. include_official_team also scans a pinned
    official site for the source-reviewed cohort. Neither mode looks up emails,
    writes to the database, drafts, or sends.
    """
    database = get_workspace().path(db_path or DB_PATH).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        source = conn.execute(
            'SELECT id, partner_name, firm, email FROM partners WHERE id=?',
            (partner_id,),
        ).fetchone()
        if source is None:
            raise ValueError('Unknown partner')
        firm = str(source['firm'] or '').strip()
        firm_key = _firm_key(firm)
        if not firm_key:
            return {
                'status': 'needs_external_research', 'firm': firm, 'source_partner_id': partner_id,
                'reason': 'The contact has no firm recorded.', 'firm_contacted': False,
                'candidates': [],
            }

        rows = conn.execute(
            """SELECT p.id, p.partner_name, p.firm, p.email, p.source, p.role AS saved_role,
                      p.linkedin_url, p.pb_linkedin, p.email_verification_status,
                      cp.stage,
                      EXISTS(SELECT 1 FROM drafts d WHERE d.partner_id=p.id
                             AND (d.status IN ('live_sent','sent') OR d.live_sent_at IS NOT NULL))
                        AS draft_sent,
                      EXISTS(SELECT 1 FROM crm_send_evidence s WHERE s.partner_id=p.id
                             AND s.evidence_status='sent_confirmed') AS provider_sent
               FROM partners p LEFT JOIN crm_profiles cp ON cp.partner_id=p.id"""
        ).fetchall()
        firm_rows = [row for row in rows if _firm_key(row['firm']) == firm_key]
        firm_contacted = any(row['draft_sent'] or row['provider_sent'] for row in firm_rows)
        candidates: dict[str, dict] = {}
        source_name = _name_key(source['partner_name'])

        for row in firm_rows:
            if row['id'] == partner_id or _name_key(row['partner_name']) == source_name:
                continue
            person_contacted = bool(row['draft_sent'] or row['provider_sent'])
            name_key = _name_key(row['partner_name'])
            if not name_key:
                continue
            candidate = {
                'partner_id': row['id'], 'name': row['partner_name'], 'firm': row['firm'],
                'role': (str(row['saved_role']).strip()[:100] or None) if row['saved_role'] else None,
                'email': row['email'],
                'email_verification_status': row['email_verification_status'] or 'unverified',
                'person_contacted': person_contacted, 'firm_contacted': firm_contacted,
                'relationship_stage': row['stage'] or 'unreviewed',
                'contact_status': 'already_contacted' if person_contacted else 'known_unsent',
                'needs_firm_policy_review': firm_contacted and not person_contacted,
                'suppressed': person_contacted or row['stage'] in {'passed', 'hold'},
                'suppression_reasons': (["Prior direct outreach"] if person_contacted else [])
                    + (["Relationship is on hold or passed"] if row['stage'] in {'passed', 'hold'} else []),
                'role_evidence_status': 'needs_role_research', 'role_evidence': None,
                'send_eligible': False,
                'source_urls': [], 'local_sources': [f'partners:{row["id"]}'],
            }
            for url in (row['linkedin_url'], row['pb_linkedin']):
                checked = _source_url(url)
                if checked and checked not in candidate['source_urls']:
                    candidate['source_urls'].append(checked)
            candidates[name_key] = candidate

        for record, path in _read_research(get_workspace().path(research_root or DEFAULT_RESEARCH_ROOT), firm_key):
            name_key = _name_key(record['name'])
            if name_key == source_name:
                continue
            candidate = candidates.setdefault(name_key, {
                'partner_id': None, 'name': record['name'], 'firm': record['firm'],
                'role': None, 'email': None, 'email_verification_status': 'unverified',
                'person_contacted': False, 'firm_contacted': firm_contacted,
                'relationship_stage': 'unreviewed', 'contact_status': 'research_only',
                'needs_firm_policy_review': firm_contacted,
                'suppressed': False, 'suppression_reasons': [],
                'role_evidence_status': 'needs_role_research', 'role_evidence': None,
                'send_eligible': False,
                'source_urls': [], 'local_sources': [],
            })
            role = record.get('role') or record.get('title')
            if not candidate['role'] and isinstance(role, str) and role.strip():
                candidate['role'] = role.strip()[:100]
                candidate['role_evidence_status'] = 'documented_in_saved_research'
                candidate['role_evidence'] = {'local_source': path, 'source_url': _source_url(record.get('source'))}
            research_email = record.get('email')
            if candidate['partner_id'] is None and isinstance(research_email, str):
                candidate['email'] = research_email.strip() or None
            elif (isinstance(research_email, str) and candidate['email']
                  and research_email.casefold() != candidate['email'].casefold()):
                candidate['email_conflict'] = True
            supporting = record.get('supporting_sources')
            if not isinstance(supporting, list):
                supporting = []
            for value in [record.get('source'), *supporting]:
                checked = _source_url(value)
                if checked and checked not in candidate['source_urls']:
                    candidate['source_urls'].append(checked)
            if path not in candidate['local_sources']:
                candidate['local_sources'].append(path)

        official_status = 'not_requested'
        official_homepage = OFFICIAL_FIRM_SITES.get(firm_key)
        if include_official_team and official_homepage:
            from vcoutreach.web.enrichment.firm_website import discover_team_url, scrape_team_page

            team_url = discover_team_url(official_homepage)
            if team_url and _source_url(team_url) and _looks_like_team_roster_url(team_url):
                team = scrape_team_page(team_url)
                people = (team.get('partners', [])
                          + _official_grouped_names(firm_key, official_homepage)
                          + _official_linked_names(firm_key, team_url))
                for person in people:
                    if not isinstance(person, dict):
                        continue
                    name = person.get('name')
                    role = person.get('title')
                    if not isinstance(name, str) or not isinstance(role, str):
                        continue
                    name_key = _name_key(name)
                    if (not name_key or name_key == source_name
                            or not re.search(r'\b(partner|principal|investor|investment|founder|managing director)\b', role, re.I)):
                        continue
                    item = candidates.setdefault(name_key, {
                        'partner_id': None, 'name': name.strip()[:120], 'firm': firm,
                        'role': None, 'email': None,
                        'email_verification_status': 'unverified',
                        'person_contacted': False, 'firm_contacted': firm_contacted,
                        'relationship_stage': 'unreviewed',
                        'contact_status': 'official_team_research',
                        'needs_firm_policy_review': firm_contacted,
                        'suppressed': False, 'suppression_reasons': [],
                        'role_evidence_status': 'needs_role_research',
                        'role_evidence': None, 'send_eligible': False,
                        'source_urls': [], 'local_sources': [],
                    })
                    if not item['role']:
                        item['role'] = role.strip()[:100]
                        item['role_evidence_status'] = (
                            'official_roster_category_only'
                            if role == 'Investment team, title to verify' else 'official_team_page'
                        )
                        item['role_evidence'] = {'source_url': person.get('role_source_url') or team_url}
                    if team_url not in item['source_urls']:
                        item['source_urls'].append(team_url)
                    role_source = _source_url(person.get('role_source_url'))
                    if role_source and role_source not in item['source_urls']:
                        item['source_urls'].append(role_source)
                    profile_url = _source_url(person.get('profile_url'))
                    if profile_url and profile_url not in item['source_urls']:
                        item['source_urls'].append(profile_url)
                official_status = 'team_page_scanned'
            else:
                official_status = 'team_page_unavailable'
        elif include_official_team:
            official_status = 'official_site_not_pinned'

        def role_priority(item: dict) -> int:
            role = (item.get('role') or '').casefold()
            if 'general partner' in role or 'managing partner' in role:
                return 0
            if 'partner' in role:
                return 1
            if 'principal' in role:
                return 2
            return 3

        ordered = sorted(candidates.values(), key=lambda item: (
            item['person_contacted'], role_priority(item), item['name'].casefold(),
        ))
        search_scope = f'site:{urlparse(official_homepage).netloc} ' if official_homepage else ''
        search_url = 'https://www.google.com/search?q=' + quote_plus(
            f'{search_scope}{firm} venture capital investment team official'
        ) if not ordered or official_status != 'team_page_scanned' else None
        return {
            'status': 'candidates_found' if ordered else 'needs_external_research',
            'firm': firm, 'source_partner_id': partner_id,
            'reason': None if ordered else 'No other named contact for this firm is in local records.',
            'firm_contacted': firm_contacted, 'candidates': ordered,
            'official_status': official_status, 'official_homepage': official_homepage,
            'official_search_url': search_url,
        }
    finally:
        conn.close()
