"""Compact, local relationship records for the outreach dashboard.

The existing partners table remains the contact identity source. CRM tables hold
only operator decisions and short activity summaries, never copied email bodies.
"""
from __future__ import annotations

import json
import sqlite3
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from vcoutreach.web.db import get_conn
from vcoutreach.web.config import WorkspacePath, get_workspace

STAGES = ('prospect', 'contacted', 'warm', 'meeting', 'passed', 'hold')
FIT_GRADES = ('unreviewed', 'high', 'medium', 'low', 'not_fit')
ACTIVITY_TYPES = ('note', 'call', 'meeting', 'follow_up')
_NEW100_ROOT = WorkspacePath('outputs', 'research')
_QUEUE_CAMPAIGN = 'workspace_outreach'
_FOLLOWUP_ROOT = WorkspacePath('outputs', 'followups')
_NEW_REVIEW_ROOT = _FOLLOWUP_ROOT / 'new_reviewed'
_CAMPAIGN_KEY = 'workspace_research'


def _firm_key(name: str | None) -> str:
    return ' '.join((name or '').strip().casefold().split())


def _https_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme == 'https' and parsed.netloc else None


def _connection(db_path: Path | None = None) -> sqlite3.Connection:
    return get_conn(db_path)


def _has_campaign_qualification_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_campaign_qualifications'"
    ).fetchone() is not None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: str | None) -> str | None:
    parsed = _parse_timestamp(value)
    return parsed.isoformat(timespec='seconds').replace('+00:00', 'Z') if parsed else None


def _latest_timestamp(values) -> str | None:
    parsed = [(timestamp, _parse_timestamp(timestamp)) for timestamp in values if timestamp]
    valid = [(raw, instant) for raw, instant in parsed if instant is not None]
    if valid:
        return max(valid, key=lambda pair: pair[1])[0]
    # Keep an opaque timestamp visible when no candidate can be parsed. It sorts
    # after known timestamps but is never promoted to a fabricated date.
    opaque = [str(value) for value in values if value]
    return max(opaque) if opaque else None


def _activity_sort_key(row: dict) -> tuple[int, float]:
    instant = _parse_timestamp(row.get('last_activity_at'))
    return (1, instant.timestamp()) if instant else (0, float('-inf'))


def _sent_sort_key(row: dict) -> tuple[int, float]:
    instant = _parse_timestamp(row.get('last_sent_at'))
    return (1, instant.timestamp()) if instant else (0, float('-inf'))


def _new100_research_index() -> dict[str, str]:
    inventory = _NEW100_ROOT / 'BATCH_INVENTORY.json'
    try:
        payload = json.loads(inventory.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return {
        str(row.get('email') or '').casefold(): str(row.get('research') or '')
        for row in payload.get('records', [])
        if isinstance(row, dict) and row.get('email') and row.get('research')
    }


def _current_research_facts(email: str) -> list[dict]:
    raw_path = _new100_research_index().get(email.casefold())
    if not raw_path:
        return []
    research_path = get_workspace().path(raw_path).resolve()
    if research_path != _NEW100_ROOT and _NEW100_ROOT.resolve() not in research_path.parents:
        return []
    try:
        payload = json.loads(research_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    records = payload.get('records', []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        return []
    for record in records:
        if not isinstance(record, dict) or str(record.get('email') or '').casefold() != email.casefold():
            continue
        facts = []
        for key, label in (('fit_review', 'Investor fit research'), ('check_size_status', 'Check size and fund research')):
            if record.get(key):
                facts.append({
                    'source': label + ' (review before reuse)',
                    'fact': str(record[key])[:550],
                    'source_url': _https_url(record.get('source')),
                    'fetched_at': record.get('observed_at'),
                })
        return facts
    return []


def _current_pitchbook_url(email: str) -> str | None:
    raw_path = _new100_research_index().get(email.casefold())
    if not raw_path:
        return None
    path = get_workspace().path(raw_path).resolve()
    if _NEW100_ROOT.resolve() not in path.parents:
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    for record in payload.get('records', []):
        if not isinstance(record, dict) or str(record.get('email', '')).casefold() != email.casefold():
            continue
        profile_id = str(record.get('pitchbook_id') or '')
        if re.fullmatch(r'\d{3,8}-\d{2}', profile_id):
            return f'https://my.pitchbook.com/profile/{profile_id}/investor/profile'
    return None


def prepared_followup_for(email: str) -> dict | None:
    """Read a hash-checked local follow-up copy, never a Gmail draft."""
    try:
        manifest = json.loads((_FOLLOWUP_ROOT / 'FOLLOWUP_DRAFT_MANIFEST.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if manifest.get('status') != 'local_review_only' or manifest.get('sent') is not False:
        return None
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses
    for record in manifest.get('drafts', []):
        if str(record.get('recipient') or '').casefold() != email.casefold():
            continue
        filename = record.get('draft')
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith('.eml'):
            return None
        try:
            raw = (_FOLLOWUP_ROOT / 'followups' / filename).read_bytes()
        except OSError:
            return None
        if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
            return None
        message = BytesParser(policy=policy.default).parsebytes(raw)
        recipients = [address.casefold() for _, address in getaddresses(message.get_all('To', []))]
        if recipients != [email.casefold()] or message.get('X-Unsent') != '1' or message.get_all('Bcc'):
            return None
        part = message.get_body(preferencelist=('plain',))
        return {'subject': str(message.get('Subject') or ''),
                'body': part.get_content() if part else '',
                'attachment_names': [item.get_filename() for item in message.iter_attachments()],
                'status': 'Local review only, not a Gmail draft or sent message'}
    return None


def prepared_new_investor_for(email: str) -> dict | None:
    """Expose only a hash-checked local new-investor review copy."""
    try:
        manifest = json.loads((_NEW_REVIEW_ROOT / 'LOCAL_DRAFT_MANIFEST.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if manifest.get('status') != 'local_review_only' or manifest.get('sent') is not False:
        return None
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses
    for record in manifest.get('drafts', []):
        if str(record.get('recipient') or '').casefold() != email.casefold():
            continue
        filename = record.get('draft')
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith('.eml'):
            return None
        try:
            raw = (_NEW_REVIEW_ROOT / filename).read_bytes()
        except OSError:
            return None
        if hashlib.sha256(raw).hexdigest() != record.get('sha256'):
            return None
        message = BytesParser(policy=policy.default).parsebytes(raw)
        recipients = [address.casefold() for _, address in getaddresses(message.get_all('To', []))]
        if recipients != [email.casefold()] or message.get('X-Unsent') != '1' or message.get_all('Bcc'):
            return None
        part = message.get_body(preferencelist=('plain',))
        return {'subject': str(message.get('Subject') or ''),
                'body': part.get_content() if part else '',
                'attachment_names': [item.get_filename() for item in message.iter_attachments()],
                'status': 'Local review only, not a Gmail draft or sent message'}
    return None


def _contact_rows(conn: sqlite3.Connection) -> list[dict]:
    if _has_campaign_qualification_table(conn):
        qualification_select = """(SELECT q.qualification_recommendation FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_qualification_recommendation,
                  (SELECT q.fit_review FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_qualification_notes,
                  (SELECT q.check_size_status FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_check_size_notes,
                  (SELECT q.source_path FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_qualification_source,
                  (SELECT q.source_url FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_qualification_source_url,
                  (SELECT q.observed_at FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_qualification_observed_at,
                  (SELECT q.supporting_sources_json FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_qualification_supporting_sources_json,
                  (SELECT q.pitchbook_id FROM crm_campaign_qualifications q
                   WHERE q.partner_id=p.id AND q.campaign_key='workspace_research') AS campaign_qualification_pitchbook_id"""
    else:
        qualification_select = """NULL AS campaign_qualification_recommendation,
                  NULL AS campaign_qualification_notes, NULL AS campaign_check_size_notes,
                  NULL AS campaign_qualification_source, NULL AS campaign_qualification_source_url,
                  NULL AS campaign_qualification_observed_at,
                  NULL AS campaign_qualification_supporting_sources_json,
                  NULL AS campaign_qualification_pitchbook_id"""
    rows = conn.execute(
        f"""SELECT p.id AS partner_id, p.partner_name AS name, p.firm, p.email,
                  p.source, p.tags, p.slug, cp.stage AS saved_stage, cp.fit_grade, cp.fit_rank, cp.on_update_list,
                  cp.next_follow_up_at,
                  (SELECT q.queue_rank FROM crm_outreach_queues q WHERE q.partner_id=p.id
                   AND q.campaign_key='workspace_outreach' AND q.lane='followup_watchlist') AS watchlist_rank,
                  (SELECT q.queue_rank FROM crm_outreach_queues q WHERE q.partner_id=p.id
                   AND q.campaign_key='workspace_outreach' AND q.lane='followup_next') AS next_followup_rank,
                  (SELECT q.queue_rank FROM crm_outreach_queues q WHERE q.partner_id=p.id
                   AND q.campaign_key='workspace_outreach' AND q.lane='new_research') AS new_research_rank,
                  (SELECT q.readiness FROM crm_outreach_queues q WHERE q.partner_id=p.id
                   AND q.campaign_key='workspace_outreach' AND q.lane='followup_next') AS next_followup_readiness,
                  (SELECT q.readiness FROM crm_outreach_queues q WHERE q.partner_id=p.id
                   AND q.campaign_key='workspace_outreach' AND q.lane='new_research') AS new_research_readiness,
                  (SELECT q.rationale FROM crm_outreach_queues q WHERE q.partner_id=p.id
                   AND q.campaign_key='workspace_outreach' AND q.lane='new_research') AS new_research_rationale,
                  (SELECT q.candidate_email FROM crm_outreach_queues q WHERE q.partner_id=p.id
                   AND q.campaign_key='workspace_outreach' AND q.lane='new_research') AS new_research_email,
                  (SELECT COALESCE(NULLIF(r.user_label,''),r.label) FROM replies r WHERE r.partner_id=p.id
                   ORDER BY r.received_at DESC, r.id DESC LIMIT 1) AS latest_reply_label,
                  (SELECT d.status FROM drafts d WHERE d.partner_id=p.id
                   ORDER BY d.updated_at DESC, d.id DESC LIMIT 1) AS latest_draft_status,
                  (SELECT MAX(COALESCE(d.live_sent_at,d.sent_at)) FROM drafts d
                   WHERE d.partner_id=p.id AND (d.live_sent_at IS NOT NULL OR d.status IN ('live_sent','sent'))) AS last_draft_sent_at,
                  (SELECT MAX(s.observed_at) FROM crm_send_evidence s
                   WHERE s.partner_id=p.id) AS last_provider_evidence_at,
                  (SELECT MAX(s.observed_at) FROM crm_send_evidence s
                   WHERE s.partner_id=p.id AND s.evidence_status='sent_confirmed') AS last_confirmed_sent_at,
                  EXISTS(SELECT 1 FROM crm_send_evidence s
                         WHERE s.partner_id=p.id AND s.evidence_status='sent_confirmed') AS has_confirmed_send,
                  (SELECT s.evidence_status FROM crm_send_evidence s
                   WHERE s.partner_id=p.id ORDER BY s.observed_at DESC,s.id DESC LIMIT 1) AS latest_send_evidence_status,
                  {qualification_select},
                  (SELECT MAX(r.received_at) FROM replies r WHERE r.partner_id=p.id) AS last_reply_at,
                  (SELECT MAX(m.observed_at) FROM crm_mail_evidence m WHERE m.partner_id=p.id) AS last_mail_evidence_at,
                  (SELECT MAX(a.created_at) FROM crm_activities a WHERE a.partner_id=p.id) AS last_note_at
           FROM partners p LEFT JOIN crm_profiles cp ON cp.partner_id=p.id
           ORDER BY p.partner_name COLLATE NOCASE"""
    ).fetchall()
    output = []
    for record in rows:
        row = dict(record)
        row['last_sent_at'] = _latest_timestamp(
            (row['last_draft_sent_at'], row['last_confirmed_sent_at'])
        )
        row['last_sent_at'] = _canonical_timestamp(row['last_sent_at'])
        row['last_sent_at_status'] = 'known' if row['last_sent_at'] else 'unknown'
        reply = (row['latest_reply_label'] or '').lower()
        if row['saved_stage']:
            stage = row['saved_stage']
        elif reply in {'pass', 'passed', 'bounce'}:
            stage = 'passed'
        elif reply in {'interested', 'needs_more_info'}:
            stage = 'warm'
        elif row['last_sent_at']:
            stage = 'contacted'
        else:
            stage = 'prospect'
        row['stage'] = stage
        # A high fit score is not engagement, and an address-matched incoming
        # message may be an automated response. Keep those out of the hot lane.
        row['is_hot'] = stage not in {'passed', 'hold'} and (
            stage in {'warm', 'meeting'} or reply in {'interested', 'needs_more_info'}
        )
        row['is_current'] = True
        row['fit_grade'] = row['fit_grade'] or 'unreviewed'
        recommendation = row.get('campaign_qualification_recommendation')
        if recommendation == 'pass_subject_to_final_history_and_delivery_gate':
            row['campaign_qualification_label'] = 'Passed preparation gate; final history and delivery gate required'
        elif recommendation:
            row['campaign_qualification_label'] = 'Campaign recommendation: ' + recommendation.replace('_', ' ')
        else:
            row['campaign_qualification_label'] = None
        try:
            supporting_sources = json.loads(row.pop('campaign_qualification_supporting_sources_json') or '[]')
        except (TypeError, ValueError):
            supporting_sources = []
        row['campaign_qualification_supporting_sources'] = (
            supporting_sources if isinstance(supporting_sources, list) else []
        )
        row['campaign_fit_review_present'] = bool(row.get('campaign_qualification_notes'))
        if row['new_research_readiness'] == 'waiting':
            row['new_research_label'] = 'historical contact hold'
        elif (row['new_research_rationale'] or '').startswith('FIT HOLD:'):
            row['new_research_label'] = 'fit hold'
        else:
            row['new_research_label'] = (row['new_research_readiness'] or 'research').replace('_', ' ')
        row['last_activity_at'] = _latest_timestamp(
            (value for value in (row['last_sent_at'], row['last_reply_at'],
                                 row['last_mail_evidence_at'], row['last_note_at']) if value),
        )
        row['last_activity_at'] = _canonical_timestamp(row['last_activity_at'])
        row['last_activity_at_status'] = 'known' if _parse_timestamp(row['last_activity_at']) else 'unknown'
        output.append(row)
    return output


def list_contacts(*, q: str = '', view: str = 'all', stage: str = '', page: int = 1,
                  db_path: Path | None = None, tag: str = '', draft: str = '') -> tuple[list[dict], dict]:
    conn = _connection(db_path)
    try:
        all_rows = _contact_rows(conn)
    finally:
        conn.close()
    today = date.today().isoformat()
    follow_up_cutoff = (date.today() - timedelta(days=7)).isoformat()
    current_rows = [r for r in all_rows if r['is_current']]
    def follow_up_candidate(row: dict) -> bool:
        return bool(row['is_current'] and row['stage'] == 'contacted'
                    and row['last_sent_at'] and row['last_sent_at'][:10] <= follow_up_cutoff
                    and not row['last_reply_at'] and not row['last_mail_evidence_at']
                    and not row['next_follow_up_at'])
    counts = {
        'current': len(current_rows),
        'hot': sum(r['is_hot'] for r in current_rows),
        'warm': sum(r['stage'] in {'warm', 'meeting'} for r in current_rows),
        'due': sum(bool(r['next_follow_up_at'] and r['next_follow_up_at'][:10] <= today) for r in current_rows),
        'investor_list': sum(bool(r['on_update_list']) for r in current_rows),
        'unreplied': sum(bool(r['last_sent_at'] and not r['last_reply_at']) for r in current_rows),
        'new_outreach': sum(bool(r['has_confirmed_send'] or r['latest_draft_status'] in {'sent', 'live_sent'})
                            for r in current_rows),
        'best_fit': sum(r['fit_grade'] == 'high' for r in current_rows),
        'best_fit_awaiting': sum(bool(r['fit_rank'] and r['last_sent_at']
                                      and not r['last_reply_at'] and not r['last_mail_evidence_at']
                                      and r['stage'] not in {'passed', 'hold'}) for r in current_rows),
        'reviewed_fit': sum(r['fit_grade'] in {'high', 'medium'} for r in current_rows),
        'follow_up_candidates': sum(follow_up_candidate(r) for r in current_rows),
        'followup_watchlist': sum(r['watchlist_rank'] is not None for r in all_rows),
        'followup_next': sum(r['next_followup_rank'] is not None for r in all_rows),
        'new_research': sum(r['new_research_rank'] is not None for r in all_rows),
    }
    needle = q.strip().casefold()[:120]
    draft_states = {}
    if draft in {'ok', 'blocked', 'none'}:
        from vcoutreach.web.drafts_service import list_drafts
        draft_states = {row['slug']: row['status'] for row in list_drafts()}
    def include(row: dict) -> bool:
        if needle and not any(needle in str(row.get(k) or '').casefold()
                              for k in ('name', 'firm', 'email', 'new_research_email')):
            return False
        if tag and tag.casefold() not in {value.strip().casefold() for value in (row.get('tags') or '').split(',')}:
            return False
        if draft in {'ok', 'blocked', 'none'} and draft_states.get(row.get('slug'), 'none') != draft:
            return False
        if stage and row['stage'] != stage:
            return False
        if view == 'current':
            return row['is_current']
        if view == 'warm':
            return row['is_current'] and row['stage'] in {'warm', 'meeting'}
        if view == 'hot':
            return row['is_current'] and row['is_hot']
        if view == 'due':
            return row['is_current'] and bool(row['next_follow_up_at'] and row['next_follow_up_at'][:10] <= today)
        if view == 'follow_up_candidates':
            return follow_up_candidate(row)
        if view == 'investor_list':
            return row['is_current'] and bool(row['on_update_list'])
        if view == 'unreplied':
            return row['is_current'] and bool(row['last_sent_at'] and not row['last_reply_at'])
        if view == 'new_outreach':
            return row['is_current'] and bool(
                row['has_confirmed_send'] or row['latest_draft_status'] in {'sent', 'live_sent'}
            )
        if view in {'sent', 'sent_emails'}:
            return row['is_current'] and bool(
                row['last_sent_at'] or row['has_confirmed_send']
                or row['latest_draft_status'] in {'sent', 'live_sent'}
            )
        if view == 'best_fit':
            return row['is_current'] and row['fit_grade'] == 'high'
        if view == 'best_fit_awaiting':
            return bool(row['is_current'] and row['fit_rank'] and row['last_sent_at']
                        and not row['last_reply_at'] and not row['last_mail_evidence_at']
                        and row['stage'] not in {'passed', 'hold'})
        if view == 'reviewed_fit':
            return row['is_current'] and row['fit_grade'] in {'high', 'medium'}
        if view in {'followup_watchlist', 'followup_next', 'new_research'}:
            return row[{'followup_watchlist': 'watchlist_rank',
                        'followup_next': 'next_followup_rank',
                        'new_research': 'new_research_rank'}[view]] is not None
        return True
    rows = [row for row in all_rows if include(row)]
    if view in {'followup_watchlist', 'followup_next', 'new_research'}:
        rank_key = {'followup_watchlist': 'watchlist_rank',
                    'followup_next': 'next_followup_rank',
                    'new_research': 'new_research_rank'}[view]
        rows.sort(key=lambda row: (row[rank_key], row['name'].casefold()))
    elif view == 'new_outreach':
        rows.sort(key=_activity_sort_key, reverse=True)
    elif view in {'sent', 'sent_emails'}:
        rows.sort(key=_sent_sort_key, reverse=True)
    elif view in {'best_fit', 'best_fit_awaiting', 'reviewed_fit'}:
        rows.sort(key=lambda row: (
            row['fit_rank'] if row['fit_rank'] is not None else 9999,
            row['name'].casefold(),
        ))
    else:
        rows.sort(key=lambda row: (
            row['next_follow_up_at'] or '9999',
            0 if row['stage'] in {'warm', 'meeting', 'update_list'} else 1,
            0 if row['latest_send_evidence_status'] else 1,
            row['name'].casefold(),
        ))
    page_size = 100
    page_count = max((len(rows) + page_size - 1) // page_size, 1)
    page = max(1, min(page, page_count))
    counts.update({'matching': len(rows), 'page': page, 'pages': page_count})
    return rows[(page - 1) * page_size:page * page_size], counts


def get_contact(partner_id: int, db_path: Path | None = None) -> dict | None:
    if not isinstance(partner_id,int) or not 0 <= partner_id <= 9223372036854775807:
        return None
    conn = _connection(db_path)
    try:
        partner_row = conn.execute('SELECT * FROM partners WHERE id=?', (partner_id,)).fetchone()
        if partner_row is None:
            return None
        partner = dict(partner_row)
        profile_row = conn.execute('SELECT * FROM crm_profiles WHERE partner_id=?', (partner_id,)).fetchone()
        profile = {key: None for key in ('fit_rank','fit_rationale','next_follow_up_at','follow_up_reason','updated_at')}
        profile.update(on_update_list=0,fit_grade='unreviewed')
        if profile_row:
            profile.update(dict(profile_row))
        firm_row = conn.execute('SELECT * FROM crm_firms WHERE firm_key=?', (_firm_key(partner['firm']),)).fetchone()
        firm = dict(firm_row) if firm_row else {}
        for key in ('check_size_min', 'check_size_max', 'fund_status', 'evidence_note'):
            profile[key] = firm.get(key)
        if not profile.get('stage'):
            derived = next((row['stage'] for row in _contact_rows(conn) if row['partner_id'] == partner_id), 'prospect')
            profile['stage'] = derived
        profile.setdefault('fit_grade', 'unreviewed')
        qualification_row = None
        if _has_campaign_qualification_table(conn):
            qualification_row = conn.execute(
                "SELECT qualification_recommendation,fit_review,check_size_status,source_path,source_url,"
                "supporting_sources_json,pitchbook_id,observed_at FROM crm_campaign_qualifications "
                "WHERE partner_id=? AND campaign_key=?",
                (partner_id, _CAMPAIGN_KEY),
            ).fetchone()
        if qualification_row:
            recommendation = qualification_row['qualification_recommendation']
            profile.update({
                'campaign_qualification_recommendation': recommendation,
                'campaign_qualification_label': (
                    'Passed preparation gate; final history and delivery gate required'
                    if recommendation == 'pass_subject_to_final_history_and_delivery_gate'
                    else 'Campaign recommendation: ' + recommendation.replace('_', ' ')
                ),
                'campaign_qualification_notes': qualification_row['fit_review'],
                'campaign_check_size_notes': qualification_row['check_size_status'],
                'campaign_qualification_source': qualification_row['source_path'],
                'campaign_qualification_source_url': qualification_row['source_url'],
                'campaign_qualification_observed_at': qualification_row['observed_at'],
                'campaign_qualification_pitchbook_id': qualification_row['pitchbook_id'],
                'campaign_fit_review_present': bool(qualification_row['fit_review']),
            })
            try:
                supporting = json.loads(qualification_row['supporting_sources_json'] or '[]')
            except (TypeError, ValueError):
                supporting = []
            profile['campaign_qualification_supporting_sources'] = supporting if isinstance(supporting, list) else []
        else:
            profile.update({
                'campaign_qualification_recommendation': None,
                'campaign_qualification_label': None,
                'campaign_qualification_notes': None,
                'campaign_check_size_notes': None,
                'campaign_qualification_source': None,
                'campaign_qualification_source_url': None,
                'campaign_qualification_observed_at': None,
                'campaign_qualification_pitchbook_id': None,
                'campaign_fit_review_present': False,
                'campaign_qualification_supporting_sources': [],
            })
        profile['is_synthetic'] = bool((partner.get('source') or '').startswith('synthetic'))
        # No legacy note: a contact with no CRM profile yet is simply new (just imported or added by hand).
        profile['integrity_note'] = ''
        activities = [dict(row) for row in conn.execute(
            "SELECT id, 'follow_up' AS type, title, due_at, status, note FROM crm_tasks "
            "WHERE partner_id=? AND status='open' ORDER BY due_at LIMIT 20", (partner_id,)
        )]
        timeline = [dict(row) for row in conn.execute(
            "SELECT created_at AS date, activity_type AS type, 'recorded' AS status, subject, snippet "
            "FROM crm_activities WHERE partner_id=? ORDER BY created_at DESC LIMIT 25", (partner_id,)
        )]
        timeline.extend(dict(row) for row in conn.execute(
            "SELECT received_at AS date, 'reply' AS type, COALESCE(user_label,label,'received') AS status, "
            "subject, SUBSTR(snippet,1,240) AS snippet FROM replies WHERE partner_id=? "
            "ORDER BY received_at DESC LIMIT 20", (partner_id,)
        ))
        timeline.extend(dict(row) for row in conn.execute(
            "SELECT COALESCE(live_sent_at,sent_at,created_at) AS date, "
            "CASE WHEN live_sent_at IS NOT NULL OR status='live_sent' THEN 'outbound_email' ELSE 'local_draft' END AS type, "
            "status, COALESCE(live_subject,subject) AS subject, "
            "CASE WHEN live_sent_at IS NOT NULL OR status='live_sent' "
            "THEN 'Recorded sender state; inbox delivery is not proven.' "
            "ELSE 'Local draft state; no send is implied.' END AS snippet "
            "FROM drafts WHERE partner_id=? "
            "ORDER BY created_at DESC LIMIT 20", (partner_id,)
        ))
        timeline.extend(dict(row) for row in conn.execute(
            "SELECT observed_at AS date, 'email' AS type, evidence_status AS status, "
            "'Outbound send confirmation' AS subject, "
            "'Provider and Sent-folder confirmation. Inbox delivery and reading are not verified.' AS snippet "
            "FROM crm_send_evidence WHERE partner_id=? ORDER BY observed_at DESC LIMIT 20",
            (partner_id,),
        ))
        timeline.extend(dict(row) for row in conn.execute(
            "SELECT observed_at AS date, 'incoming_mail' AS type, 'metadata_only' AS status, "
            "'Incoming mail from this address' AS subject, "
            "'Read-only Gmail metadata. Content and human interest are not classified.' AS snippet "
            "FROM crm_mail_evidence WHERE partner_id=? ORDER BY observed_at DESC LIMIT 20",
            (partner_id,),
        ))
        timeline.sort(key=lambda item: item['date'] or '', reverse=True)
        related_contacts = [dict(row) for row in conn.execute(
            "SELECT p.id, p.partner_name, p.firm, COALESCE(cp.stage,'prospect') AS stage "
            "FROM partners p LEFT JOIN crm_profiles cp ON cp.partner_id=p.id "
            "WHERE lower(trim(p.firm))=? AND p.id!=? ORDER BY p.partner_name LIMIT 12",
            (_firm_key(partner['firm']), partner_id),
        )] if _firm_key(partner['firm']) else []
        queue_entries = [dict(row) for row in conn.execute(
            'SELECT lane,queue_rank,readiness,rationale,evidence_source,candidate_email FROM crm_outreach_queues '
            'WHERE campaign_key=? AND partner_id=? ORDER BY lane',
            (_QUEUE_CAMPAIGN, partner_id),
        )]
        prepared_new_investor = prepared_new_investor_for(
            next((entry['candidate_email'] for entry in queue_entries
                  if entry['lane'] == 'new_research' and entry['candidate_email']), '')
        )
        enrichment_facts = []
        for source in conn.execute(
            'SELECT source,url,facts_json,fetched_at FROM enrichment WHERE partner_id=? ORDER BY fetched_at DESC LIMIT 6',
            (partner_id,),
        ):
            try:
                facts = json.loads(source['facts_json'] or '[]')
            except (ValueError, TypeError):
                facts = []
            if not isinstance(facts, list):
                continue
            for fact in facts[:2]:
                if isinstance(fact, dict) and fact.get('text'):
                    enrichment_facts.append({
                        'source': str(source['source']) + ' (research lead)',
                        'fact': str(fact['text'])[:300],
                        'source_url': _https_url(fact.get('source_url') or source['url']),
                        'fetched_at': source['fetched_at'],
                    })
        enrichment_facts = _current_research_facts(partner['email']) + enrichment_facts
        pitchbook_url = _https_url(firm.get('pitchbook_url')) or _current_pitchbook_url(partner['email'])
        if not pitchbook_url:
            for fact in enrichment_facts:
                if fact['source_url'] and 'pitchbook.com' in urlparse(fact['source_url']).netloc:
                    pitchbook_url = fact['source_url']
                    break
        return {
            'partner': partner, 'profile': profile, 'activities': activities,
            'timeline': timeline[:35], 'related_contacts': related_contacts,
            'enrichment_facts': enrichment_facts, 'pitchbook_url': pitchbook_url,
            'enrow_url': None, 'tracking': {'status': 'unavailable'},
            'prepared_followup': prepared_followup_for(partner['email']) if partner.get('email') else None,
            'queue_entries': queue_entries,
            'prepared_new_investor': prepared_new_investor,
        }
    finally:
        conn.close()


def save_profile(partner_id: int, values: dict, db_path: Path | None = None) -> None:
    stage = values.get('stage', 'prospect')
    fit_grade = values.get('fit_grade', 'unreviewed')
    if stage not in STAGES or fit_grade not in FIT_GRADES:
        raise ValueError('Invalid relationship stage or fit grade')
    minimum = values.get('check_size_min')
    maximum = values.get('check_size_max')
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError('Minimum check size exceeds maximum')
    conn = _connection(db_path)
    try:
        partner = conn.execute('SELECT firm FROM partners WHERE id=?', (partner_id,)).fetchone()
        if partner is None:
            raise ValueError('Unknown contact')
        with conn:
            conn.execute(
                "INSERT INTO crm_profiles(partner_id,stage,fit_grade,fit_rationale,next_follow_up_at,follow_up_reason,on_update_list) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(partner_id) DO UPDATE SET stage=excluded.stage, "
                "fit_grade=excluded.fit_grade,fit_rationale=excluded.fit_rationale, "
                "next_follow_up_at=excluded.next_follow_up_at,follow_up_reason=excluded.follow_up_reason, "
                "on_update_list=excluded.on_update_list, "
                "updated_at=CURRENT_TIMESTAMP",
                (partner_id, stage, fit_grade, values.get('fit_rationale'),
                 values.get('next_follow_up_at'), values.get('follow_up_reason'),
                 1 if values.get('on_update_list') else 0),
            )
            firm_key = _firm_key(partner['firm'])
            if firm_key:
                conn.execute(
                    "INSERT INTO crm_firms(firm_key,firm_name,check_size_min,check_size_max,fund_status) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(firm_key) DO UPDATE SET "
                    "check_size_min=excluded.check_size_min,check_size_max=excluded.check_size_max, "
                    "fund_status=excluded.fund_status,updated_at=CURRENT_TIMESTAMP",
                    (firm_key, partner['firm'], minimum, maximum, values.get('fund_status')),
                )
            due = values.get('next_follow_up_at')
            open_task = conn.execute(
                "SELECT id FROM crm_tasks WHERE partner_id=? AND status='open' "
                "ORDER BY id DESC LIMIT 1", (partner_id,),
            ).fetchone()
            if due and open_task:
                conn.execute('UPDATE crm_tasks SET due_at=?,note=? WHERE id=?',
                             (due, values.get('follow_up_reason'), open_task['id']))
            elif due:
                conn.execute('INSERT INTO crm_tasks(partner_id,title,due_at,note) VALUES(?,?,?,?)',
                             (partner_id, 'Follow up', due, values.get('follow_up_reason')))
            elif open_task:
                conn.execute("UPDATE crm_tasks SET status='complete',completed_at=CURRENT_TIMESTAMP WHERE id=?",
                             (open_task['id'],))
    finally:
        conn.close()


def add_activity(partner_id: int, activity_type: str, subject: str, snippet: str,
                 db_path: Path | None = None) -> None:
    if activity_type not in ACTIVITY_TYPES:
        raise ValueError('Invalid activity type')
    conn = _connection(db_path)
    try:
        with conn:
            conn.execute('INSERT INTO crm_activities(partner_id,activity_type,subject,snippet) VALUES(?,?,?,?)',
                         (partner_id, activity_type, subject[:180], snippet[:600]))
    finally:
        conn.close()


def complete_task(task_id: int, db_path: Path | None = None) -> int | None:
    conn = _connection(db_path)
    try:
        with conn:
            row = conn.execute('SELECT partner_id FROM crm_tasks WHERE id=? AND status=\'open\'', (task_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE crm_tasks SET status='complete',completed_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
            conn.execute('UPDATE crm_profiles SET next_follow_up_at=NULL,follow_up_reason=NULL WHERE partner_id=?',
                         (row['partner_id'],))
            return int(row['partner_id'])
    finally:
        conn.close()
