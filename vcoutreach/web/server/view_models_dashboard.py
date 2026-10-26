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

SPARK_DAYS = 7
STAGE_SEGMENTS = {'prospect': 1, 'hold': 1, 'contacted': 2, 'warm': 3, 'meeting': 4, 'passed': 0}

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

def _draft_index() -> tuple[dict, int, int]:
    """(slug -> draft row, drafts waiting for approval, drafts approved to send). Failures degrade to no drafts."""
    try:
        from vcoutreach.web import drafts_service
        drafts = drafts_service.list_drafts()
    except Exception:
        return {}, 0, 0
    index = {str(d.get('slug')): d for d in drafts if d.get('slug')}
    pending = sum(1 for d in drafts if d.get('status') == 'ok' and d.get('approval_state') == 'pending')
    approved = sum(1 for d in drafts if d.get('status') == 'ok' and d.get('approval_state') == 'approved')
    return index, pending, approved


# ---- sparklines and funnel ------------------------------------------------------------------------------------

def _spark(values, label: str, tone: str = '') -> dict:
    """Points for a vz-spark svg (viewBox 0 0 100 28), same geometry as the analytics sparklines.

    An empty or all-zero series is marked ghost so the template draws a flat dashed line and nothing is faked.
    """
    values = [max(int(v or 0), 0) for v in (values or [])]
    n = len(values)
    peak = max(values) if values else 0
    if n < 2 or peak <= 0:
        return {'ghost': True, 'tone': tone, 'label': f'{label}: nothing in the last {SPARK_DAYS} days',
                'points': '0,26 100,26', 'last_y': 26.0}
    pts, last_y = [], 26.0
    for idx, value in enumerate(values):
        last_y = 26 - value / peak * 22
        pts.append(f'{idx * 100 / (n - 1):.2f},{last_y:.2f}')
    return {'ghost': False, 'tone': tone, 'label': f'{label}, last {n} days, highest day {peak}',
            'points': ' '.join(pts), 'last_y': last_y}


def _day_keys(now: datetime, days: int) -> list[str]:
    return [(now - timedelta(days=days - 1 - i)).date().isoformat() for i in range(days)]


def _created_by_day(db_path, table: str, now: datetime, days: int, *, cumulative: bool) -> list[int]:
    """Rows of a table by created_at day over the last `days` days, optionally as a running total."""
    keys = _day_keys(now, days)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(f'SELECT substr(created_at,1,10) AS day FROM {table} WHERE created_at IS NOT NULL').fetchall()
    except Exception:
        return [0] * days
    finally:
        conn.close()
    per_day = {key: 0 for key in keys}
    before = 0
    for row in rows:
        day = row['day']
        if day in per_day:
            per_day[day] += 1
        elif day and day < keys[0]:
            before += 1
    values = [per_day[key] for key in keys]
    if cumulative:
        total = before
        out = []
        for value in values:
            total += value
            out.append(total)
        return out
    return values


def kpi_series(db_path, now: datetime | None = None, days: int = SPARK_DAYS) -> dict:
    """Seven day series for the KPI sparklines and the next-action tiles, from the analytics series helper.

    Keys: contacts, sent, replied, reply_rate, drafts. Every failure degrades to zeros, which draw as ghost sparks.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    zeros = [0] * days
    out = {'contacts': zeros, 'sent': zeros, 'replied': zeros, 'reply_rate': zeros, 'drafts': zeros}
    try:
        from vcoutreach.web.server import view_models_analytics as analytics
        series = analytics.build_series(analytics._load_evidence(db_path), now, days)
        out['sent'] = list(series['sent'])
        out['replied'] = list(series['replied'])
        out['reply_rate'] = [round(100 * r / s) if s else 0 for r, s in zip(series['replied'], series['sent'])]
    except Exception:
        pass
    out['contacts'] = _created_by_day(db_path, 'partners', now, days, cumulative=True)
    out['drafts'] = _created_by_day(db_path, 'drafts', now, days, cumulative=False)
    return out


def _funnel(rows: list[dict], drafts: dict) -> list[dict]:
    """Contacts > Drafted > Approved > Sent > Replied, shaped for the vz-fun svg rows of the analytics page."""
    counts = {'Contacts': 0, 'Drafted': 0, 'Approved': 0, 'Sent': 0, 'Replied': 0}
    for row in rows:
        draft = drafts.get(str(row.get('slug'))) or {}
        sent = _sent_flag(row)
        counts['Contacts'] += 1
        counts['Drafted'] += 1 if (draft or sent) else 0
        counts['Approved'] += 1 if (draft.get('approval_state') == 'approved' or sent) else 0
        counts['Sent'] += 1 if sent else 0
        counts['Replied'] += 1 if _replied_flag(row) else 0
    top = max(counts['Contacts'], 1)
    stages = []
    for label, value in counts.items():
        width = max(100 * value / top, 1.2)
        stages.append({'label': label, 'value': value, 'zero': value == 0, 'w': round(width, 2),
                       'x': round((100 - width) / 2, 2), 'pct': f'{round(100 * value / top)}%'})
    for idx, stage in enumerate(stages):
        nxt = stages[idx + 1] if idx + 1 < len(stages) else None
        stage['join'] = (f"{stage['x']},26 {stage['x'] + stage['w']:.2f},26 "
                         f"{nxt['x'] + nxt['w']:.2f},32 {nxt['x']},32") if nxt else ''
    return stages


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
        step = {'label': 'Fix address', 'href': contact_href, 'kind': 'button', 'tone': 'primary'}
    elif label == 'interested':
        pill = ('Interested', 'pk-pill--success')
        step = {'label': 'Answer reply', 'href': '/replies?tab=needs', 'kind': 'button', 'tone': 'primary'}
    elif replied:
        pill = ('Replied', 'pk-pill--info')
        if label in {'out_of_office', 'auto_reply', 'pass'}:
            step = {'label': 'Open contact', 'href': contact_href, 'kind': 'button', 'tone': 'ghost'}
        else:
            step = {'label': 'Answer reply', 'href': '/replies?tab=needs', 'kind': 'button', 'tone': 'primary'}
    elif sent:
        if row.get('partner_id') in opened:
            pill, est = ('Opened', 'pk-pill--info'), True
        else:
            pill = ('Sent', 'pk-pill--sent')
        if _followup_due(row, today, cutoff):
            step = {'label': 'Follow up', 'href': f"/crm/drafts?type=follow_up&contact={row['partner_id']}",
                    'kind': 'button', 'tone': 'secondary'}
        else:
            step = {'label': 'Wait for reply', 'href': contact_href, 'kind': 'button', 'tone': 'ghost'}
    elif draft.get('approval_state') == 'approved' and draft.get('status') == 'ok':
        pill = ('Approved', 'pk-pill--approved')
        step = {'label': 'Send', 'href': SEND_PLAN_HREF, 'kind': 'button', 'tone': 'secondary'}
    elif draft.get('status') == 'blocked':
        pill = ('Draft blocked', 'pk-pill--blocked')
        step = {'label': 'Fix draft', 'href': editor, 'kind': 'button', 'tone': 'primary'}
    elif draft:
        pill = ('Draft ready', 'pk-pill--needs-review')
        step = {'label': 'Review draft', 'href': editor, 'kind': 'button', 'tone': 'primary'}
    else:
        pill = ('Not contacted', 'pk-pill--neutral')
        step = {'label': 'Write draft', 'href': '/crm/drafts#writeDrafts', 'kind': 'button', 'tone': 'secondary'}
    row['pill'] = {'label': pill[0], 'cls': pill[1], 'est': est}
    row['icon'] = ('alert-triangle' if label == 'bounce' else 'message' if replied else 'mail' if sent
                   else 'pencil' if draft else 'users')
    row['next_step'] = step
    row['stage_label'] = str(row.get('stage') or '').replace('_', ' ').title() or 'Unknown'
    row['stage_segments'] = STAGE_SEGMENTS.get(str(row.get('stage') or ''), 0)
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
    drafts, drafts_to_approve, drafts_approved = _draft_index()
    replies_to_answer = _replies_to_answer(db_path)
    opened = _opened_ids(db_path)
    words = _campaign_words()
    series = kpi_series(db_path)
    no_draft_total = sum(1 for r in every if r.get('is_current') and not drafts.get(str(r.get('slug'))) and not _sent_flag(r))

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
        {'label': 'Contacts', 'value': str(current_total), 'caption': f'{words["people"].capitalize()} in this workspace', 'na': False,
         'href': '/crm', 'spark': _spark(series['contacts'], 'Contacts in your workspace')},
        {'label': 'Sent', 'value': str(sent_total), 'caption': 'Emails sent or confirmed', 'na': False,
         'href': '/crm/analytics', 'spark': _spark(series['sent'], 'Emails sent per day')},
        {'label': 'Replied', 'value': str(replied_total), 'caption': 'Contacts who wrote back', 'na': False,
         'href': '/replies', 'spark': _spark(series['replied'], 'Replies per day, counted on the day the email went out', 'vz-spark--success')},
        {'label': 'Reply rate', 'value': reply_rate['text'], 'caption': reply_rate['caption'], 'na': reply_rate['na'],
         'href': '/crm/analytics#dims', 'spark': _spark(series['reply_rate'], 'Reply rate per day', 'vz-spark--success')},
    ]

    # Exactly three tiles, in priority order. Empty ones sink to the bottom as ghost rows; the first live one is primary.
    def _plural(n, one, many):
        return one if n == 1 else many
    candidates = [
        {'key': 'replies', 'count': replies_to_answer, 'text': _plural(replies_to_answer, 'reply to answer', 'replies to answer'),
         'label': 'Answer replies', 'href': '/replies?view=unread', 'spark': _spark(series['replied'], 'Replies per day')},
        {'key': 'review', 'count': drafts_to_approve, 'text': _plural(drafts_to_approve, 'draft to review', 'drafts to review'),
         'label': 'Review drafts', 'href': '/crm/drafts?status=pending#draftsTable', 'spark': _spark(series['drafts'], 'Drafts written per day')},
        {'key': 'send', 'count': drafts_approved, 'text': _plural(drafts_approved, 'approved draft to send', 'approved drafts to send'),
         'label': 'Send approved', 'href': '/partners?status=approved', 'spark': _spark(series['sent'], 'Emails sent per day')},
        {'key': 'followups', 'count': due_total, 'text': _plural(due_total, 'follow-up due', 'follow-ups due'),
         'label': 'See follow-ups', 'href': '/crm/analytics#followups', 'spark': _spark(series['sent'], 'Emails sent per day')},
        {'key': 'write', 'count': no_draft_total, 'text': _plural(no_draft_total, f'{words["person"]} without a draft', f'{words["people"]} without a draft'),
         'label': 'Write drafts', 'href': '/crm/drafts#writeDrafts', 'spark': _spark(series['drafts'], 'Drafts written per day')},
    ]
    live = [c for c in candidates if c['count']]
    idle = [c for c in candidates if not c['count']]
    no_contacts = not current_total and not every
    caught_up = None
    if no_contacts or not live:
        next_actions = []
        if not no_contacts:
            if no_draft_total:
                caught_up = {'text': 'You are caught up. Next: write drafts for '
                                     f'{no_draft_total} {_plural(no_draft_total, words["person"], words["people"])} without one.',
                             'label': 'Write drafts', 'href': '/crm/drafts#writeDrafts'}
            else:
                caught_up = {'text': 'You are caught up. Nothing needs your attention right now.',
                             'label': 'See analytics', 'href': '/crm/analytics'}
    else:
        next_actions = [dict(c, ghost=False, primary=(idx == 0)) for idx, c in enumerate(live[:3])]
        for c in idle:
            if len(next_actions) >= 3:
                break
            next_actions.append(dict(c, ghost=True, primary=False))

    if no_contacts:
        primary = {'label': 'Import contacts', 'href': CAMPAIGN_STEP_1_HREF}
    elif next_actions and next_actions[0]['key'] != 'write':
        primary = {'label': next_actions[0]['label'], 'href': next_actions[0]['href']}
    else:
        primary = {'label': 'Review drafts', 'href': '/crm/drafts'}

    funnel = _funnel([r for r in every if r.get('is_current')], drafts)

    return {
        'rows': [decorate_row(r, drafts, opened, today, cutoff) for r in rows],
        'counts': counts,
        'tabs': tabs,
        'more_views': [{'key': k, 'label': l} for k, l in MORE_VIEWS],
        'more_active': more_active,
        'kpis': kpis,
        'next_actions': next_actions,
        'caught_up': caught_up,
        'funnel': funnel,
        'primary': primary,
        'gmail': _gmail_chip(db_path),
        'words': words,
        'no_contacts': no_contacts,
        'tag_options': _tags(every, tag),
        'has_filters': bool(q or stage or tag or draft),
        'first_shown': (counts['page'] - 1) * PAGE_SIZE + 1 if counts.get('matching') else 0,
        'step_one_href': CAMPAIGN_STEP_1_HREF,
    }
