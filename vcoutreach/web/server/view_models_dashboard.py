"""View model for the Contacts dashboard (GET /crm).

Pure read helpers: they shape rows from vcoutreach.web.crm_store plus the local drafts and replies tables into what
crm_dashboard.html draws (tabs, KPIs, next actions, one status pill and one next step per row). Nothing here sends mail,
writes to the database or calls a live API. Opens and clicks are estimates and are always marked as such.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from vcoutreach.web import crm_store
from vcoutreach.web.db import get_conn

PAGE_SIZE = 100
MAX_GATHER_PAGES = 60  # 6000 contacts; beyond this the derived tabs are computed on the first 6000.

# The five count tabs. The first three are served by the store, the last three by predicates in this module.
TAB_DEFS = (
    ('current', 'All'),
    ('not_contacted', 'Not contacted'),
    ('unreplied', 'Awaiting reply'),
    ('replied', 'Replied'),
    ('followup_due', 'Follow-up due'),
)
# Everything else lives under "More views".
MORE_VIEWS = (
    ('followup_next', 'Prepared review queue'),
    ('followup_watchlist', 'Follow-up watchlist'),
    ('new_research', 'New research'),
    ('hot', 'Hot'),
    ('warm', 'Warm'),
    ('due', 'Due'),
    ('follow_up_candidates', 'Follow-up candidates'),
    ('investor_list', 'Contact updates'),
    ('best_fit', 'Best fit'),
    ('best_fit_awaiting', 'Best fit awaiting reply'),
    ('reviewed_fit', 'Reviewed fit'),
    ('new_outreach', 'New outreach'),
    ('sent', 'Sent email history'),
    ('all', 'All contacts, including archived'),
)
CUSTOM_VIEWS = {'not_contacted', 'replied', 'followup_due'}
ALL_VIEWS = {key for key, _ in TAB_DEFS} | {key for key, _ in MORE_VIEWS} | {'sent_emails'}

_NEEDS_LABELS = ('interested', 'needs_more_info', 'needs_info', 'other', 'unclassified', 'unlabeled')
_SENT_STATUSES = {'sent', 'live_sent'}
SEND_PLAN_HREF = '/crm/send-plan'
CAMPAIGN_STEP_1_HREF = '/crm/drafts#campaignContacts'


# ---- small helpers --------------------------------------------------------------------------------------------

def iso_utc(value) -> str | None:
    """ISO-8601 with an explicit UTC offset, or None when the value is not a date. Naive values are UTC."""
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00').replace(' ', 'T', 1))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _time(value) -> dict | None:
    iso = iso_utc(value)
    if not iso:
        return None
    return {'iso': iso, 'fallback': iso[:10]}


def time_tag(value, fallback: str = 'Date unknown'):
    """Markup for a local-relative <time data-rel>; plain fallback text when the value is not a date."""
    from markupsafe import Markup, escape
    when = _time(value)
    if not when:
        return Markup(escape(fallback))
    return Markup('<time datetime="%s" data-rel>%s</time>') % (when['iso'], when['fallback'])


def campaign_words() -> dict:
    """{'person': 'investor', 'people': 'investors'} for the active campaign, 'contact(s)' when unknown."""
    return _campaign_words()


def _sent_flag(row: dict) -> bool:
    return bool(row.get('last_sent_at') or row.get('has_confirmed_send')
                or row.get('latest_draft_status') in _SENT_STATUSES)


def _replied_flag(row: dict) -> bool:
    return bool(row.get('last_reply_at') or row.get('latest_reply_label'))


def _follow_up_candidate(row: dict, cutoff: str) -> bool:
    return bool(row.get('is_current') and row.get('stage') == 'contacted'
                and row.get('last_sent_at') and row['last_sent_at'][:10] <= cutoff
                and not row.get('last_reply_at') and not row.get('last_mail_evidence_at')
                and not row.get('next_follow_up_at'))


def _followup_due(row: dict, today: str, cutoff: str) -> bool:
    if not row.get('is_current'):
        return False
    due = bool(row.get('next_follow_up_at') and row['next_follow_up_at'][:10] <= today)
    return due or _follow_up_candidate(row, cutoff)


def _predicate(view: str):
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    if view == 'not_contacted':
        return lambda r: bool(r.get('is_current') and not _sent_flag(r) and not _replied_flag(r))
    if view == 'replied':
        return lambda r: bool(r.get('is_current') and _replied_flag(r))
    if view == 'followup_due':
        return lambda r: _followup_due(r, today, cutoff)
    return lambda r: True


def _sort_key(view: str):
    if view == 'replied':
        return lambda r: (r.get('last_reply_at') or r.get('last_mail_evidence_at') or '', (r.get('name') or '').casefold()), True
    if view == 'followup_due':
        return lambda r: (r.get('next_follow_up_at') or r.get('last_sent_at') or '9999', (r.get('name') or '').casefold()), False
    return lambda r: ((r.get('name') or '').casefold(),), False


def _gather(db_path, *, view='current', **filters) -> tuple[list[dict], dict]:
    """Every row of a store view across pages (bounded), plus the store counts of the first page."""
    rows: list[dict] = []
    first_counts: dict = {}
    page = 1
    while page <= MAX_GATHER_PAGES:
        chunk, counts = crm_store.list_contacts(view=view, page=page, db_path=db_path, **filters)
        if not first_counts:
            first_counts = counts
        rows.extend(chunk)
        if page >= counts['pages']:
            break
        page += 1
    return rows, first_counts


def list_for_view(db_path, *, view: str, q: str = '', stage: str = '', page: int = 1, tag: str = '', draft: str = ''):
    """Same return shape as crm_store.list_contacts, also for the three tabs the store has no view for."""
    if view not in CUSTOM_VIEWS:
        return crm_store.list_contacts(q=q, view=view, stage=stage, page=page, tag=tag, draft=draft, db_path=db_path)
    rows, counts = _gather(db_path, view='current', q=q, stage=stage, tag=tag, draft=draft)
    rows = [r for r in rows if _predicate(view)(r)]
    key, reverse = _sort_key(view)
    rows.sort(key=key, reverse=reverse)
    pages = max((len(rows) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = max(1, min(page, pages))
    counts = dict(counts)
    counts.update({'matching': len(rows), 'page': page, 'pages': pages})
    return rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], counts


# ---- data for the page ----------------------------------------------------------------------------------------

def _draft_index() -> tuple[dict, int]:
    """(slug -> draft row, number of drafts waiting for approval). Failures degrade to no drafts."""
    try:
        from vcoutreach.web import drafts_service
        drafts = drafts_service.list_drafts()
    except Exception:
        return {}, 0
    index = {str(d.get('slug')): d for d in drafts if d.get('slug')}
    pending = sum(1 for d in drafts if d.get('status') == 'ok' and d.get('approval_state') == 'pending')
    return index, pending


def _replies_to_answer(db_path) -> int:
    marks = ','.join('?' for _ in _NEEDS_LABELS)
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM replies WHERE COALESCE(user_reviewed,0)=0 "
            f"AND COALESCE(NULLIF(user_label,''),NULLIF(label,''),'unclassified') IN ({marks})",
            _NEEDS_LABELS,
        ).fetchone()
        return int(row['n'])
    except Exception:
        return 0
    finally:
        conn.close()


def _opened_ids(db_path) -> set[int]:
    """Partner ids with an estimated open. Opens come from tracking pixels, so they stay estimates."""
    try:
        from vcoutreach.web.crm_analytics import lead_scores
        leads = lead_scores(db_path)
    except Exception:
        return set()
    out = set()
    for lead in leads:
        if any(str(reason).startswith('Opened') for reason in (lead.get('reasons') or [])):
            out.add(lead.get('partner_id'))
    return out


def _campaign_words() -> dict:
    try:
        from vcoutreach.web import drafts_service
        words = drafts_service.campaign_kind()['words']
    except Exception:
        words = {}
    return {
        'person': words.get('audience_word') or 'contact',
        'people': words.get('audience_word_plural') or 'contacts',
    }


def _gmail_chip(db_path) -> dict:
    try:
        from vcoutreach.web.crm_live_sync import sync_status
        status = sync_status(db_path)
    except Exception:
        status = None
    if not status:
        return {'when': None, 'tip': 'Gmail has not been checked for replies yet. Use the refresh button to check now.'}
    tip = f"{status.get('scanned_messages') or 0} recent messages checked, {status.get('matched_messages') or 0} matched your contacts."
    if not status.get('scan_complete'):
        tip += ' The scan stopped at its message limit, so older mail may be missing.'
    return {'when': _time(status.get('checked_at')), 'tip': tip}


def decorate_row(row: dict, drafts: dict, opened: set, today: str, cutoff: str) -> dict:
    """Attach one status pill and one next step to a contact row."""
    row = dict(row)
    label = (row.get('latest_reply_label') or '').strip()
    draft = drafts.get(str(row.get('slug'))) or {}
    editor = draft.get('editor_url') or '/crm/drafts'
    sent = _sent_flag(row)
    replied = _replied_flag(row)
    contact_href = f"/crm/{row['partner_id']}"
    est = False
    if label == 'bounce':
        pill = ('Bounced', 'pk-pill--danger')
        step = {'label': 'Fix address', 'href': contact_href, 'kind': 'button'}
    elif label == 'interested':
        pill = ('Interested', 'pk-pill--success')
        step = {'label': 'Answer reply', 'href': '/replies?tab=needs', 'kind': 'button'}
    elif replied:
        pill = ('Replied', 'pk-pill--info')
        if label in {'out_of_office', 'auto_reply', 'pass'}:
            step = {'label': 'Open contact', 'href': contact_href, 'kind': 'link'}
        else:
            step = {'label': 'Answer reply', 'href': '/replies?tab=needs', 'kind': 'button'}
    elif sent:
        if row.get('partner_id') in opened:
            pill, est = ('Opened', 'pk-pill--info'), True
        else:
            pill = ('Sent', 'pk-pill--sent')
        if _followup_due(row, today, cutoff):
            step = {'label': 'Follow up', 'href': contact_href, 'kind': 'button'}
        else:
            step = {'label': 'Wait for reply', 'href': '', 'kind': 'text'}
    elif draft.get('approval_state') == 'approved' and draft.get('status') == 'ok':
        pill = ('Approved', 'pk-pill--approved')
        step = {'label': 'Send', 'href': SEND_PLAN_HREF, 'kind': 'button'}
    elif draft.get('status') == 'blocked':
        pill = ('Draft blocked', 'pk-pill--blocked')
        step = {'label': 'Fix draft', 'href': editor, 'kind': 'button'}
    elif draft:
        pill = ('Draft ready', 'pk-pill--needs-review')
        step = {'label': 'Review draft', 'href': editor, 'kind': 'button'}
    else:
        pill = ('Not contacted', 'pk-pill--neutral')
        step = {'label': 'Write draft', 'href': '/crm/drafts#writeDrafts', 'kind': 'button'}
    row['pill'] = {'label': pill[0], 'cls': pill[1], 'est': est}
    row['next_step'] = step
    row['when'] = _time(row.get('last_sent_at') if sent else row.get('last_activity_at'))
    row['when_label'] = 'Sent' if sent else 'Active'
    row['follow_up'] = _time(row.get('next_follow_up_at'))
    return row


def _tags(rows: list[dict], current: str) -> list[str]:
    seen = {}
    for row in rows:
        for part in str(row.get('tags') or '').split(','):
            part = part.strip()
            if part:
                seen.setdefault(part.casefold(), part)
    if current and current.casefold() not in seen:
        seen[current.casefold()] = current
    return sorted(seen.values(), key=str.casefold)


def dashboard_context(db_path, *, view: str, q: str = '', stage: str = '', page: int = 1, tag: str = '', draft: str = '') -> dict:
    """Everything crm_dashboard.html needs, in one dict."""
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    rows, counts = list_for_view(db_path, view=view, q=q, stage=stage, page=page, tag=tag, draft=draft)
    # Unfiltered rows feed the tab counts, KPIs and the tag list.
    every, base_counts = _gather(db_path, view='current')
    current_total = base_counts.get('current', len(every))
    sent_total = sum(1 for r in every if _sent_flag(r))
    replied_total = sum(1 for r in every if _replied_flag(r))
    replied_of_sent = sum(1 for r in every if _sent_flag(r) and _replied_flag(r))
    due_total = sum(1 for r in every if _followup_due(r, today, cutoff))
    not_contacted_total = sum(1 for r in every if r.get('is_current') and not _sent_flag(r) and not _replied_flag(r))
    drafts, drafts_to_approve = _draft_index()
    replies_to_answer = _replies_to_answer(db_path)
    opened = _opened_ids(db_path)
    words = _campaign_words()

    tab_counts = {
        'current': current_total,
        'not_contacted': not_contacted_total,
        'unreplied': base_counts.get('unreplied', 0),
        'replied': replied_total,
        'followup_due': due_total,
    }
    tabs = [{'key': key, 'label': label, 'count': tab_counts[key], 'active': view == key} for key, label in TAB_DEFS]
    more_active = view in {key for key, _ in MORE_VIEWS} or view == 'sent_emails'

    if sent_total:
        reply_rate = {'text': f'{round(100 * replied_of_sent / sent_total)}%', 'na': False,
                      'caption': f'{replied_of_sent} of {sent_total} sent'}
    else:
        reply_rate = {'text': 'No sends yet', 'na': True, 'caption': 'Appears after your first send'}
    kpis = [
        {'label': f"Contacts", 'value': str(current_total), 'caption': f'{words["people"].capitalize()} in this workspace', 'na': False, 'href': ''},
        {'label': 'Sent', 'value': str(sent_total), 'caption': 'Emails sent or confirmed', 'na': False, 'href': ''},
        {'label': 'Replied', 'value': str(replied_total), 'caption': 'Contacts who wrote back', 'na': False, 'href': ''},
        {'label': 'Reply rate', 'value': reply_rate['text'], 'caption': reply_rate['caption'], 'na': reply_rate['na'], 'href': ''},
    ]

    next_actions = []
    if replies_to_answer:
        next_actions.append({'count': replies_to_answer, 'text': 'reply to answer' if replies_to_answer == 1 else 'replies to answer', 'label': 'Answer replies',
                             'href': '/replies?tab=needs'})
    if drafts_to_approve:
        next_actions.append({'count': drafts_to_approve, 'text': 'draft to approve' if drafts_to_approve == 1 else 'drafts to approve', 'label': 'Review drafts',
                             'href': '/crm/drafts?status=pending'})
    if due_total:
        next_actions.append({'count': due_total, 'text': 'follow-up due' if due_total == 1 else 'follow-ups due', 'label': 'See follow-ups',
                             'href': f'/crm?view=followup_due'})

    no_contacts = not current_total and not every
    if no_contacts:
        primary = {'label': 'Import contacts', 'href': CAMPAIGN_STEP_1_HREF}
    elif drafts_to_approve:
        primary = {'label': 'Review drafts', 'href': '/crm/drafts?status=pending'}
    elif replies_to_answer:
        primary = {'label': 'Answer replies', 'href': '/replies?tab=needs'}
    else:
        primary = {'label': 'Review drafts', 'href': '/crm/drafts'}

    return {
        'rows': [decorate_row(r, drafts, opened, today, cutoff) for r in rows],
        'counts': counts,
        'tabs': tabs,
        'more_views': [{'key': k, 'label': l} for k, l in MORE_VIEWS],
        'more_active': more_active,
        'kpis': kpis,
        'next_actions': next_actions,
        'primary': primary,
        'gmail': _gmail_chip(db_path),
        'words': words,
        'no_contacts': no_contacts,
        'tag_options': _tags(every, tag),
        'has_filters': bool(q or stage or tag or draft),
        'first_shown': (counts['page'] - 1) * PAGE_SIZE + 1 if counts.get('matching') else 0,
        'step_one_href': CAMPAIGN_STEP_1_HREF,
    }
