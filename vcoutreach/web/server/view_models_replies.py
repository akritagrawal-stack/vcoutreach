"""View models for the Replies and Scheduled pages.

Pure helpers: they shape rows already loaded by vcoutreach.web.replies.loader (and the scheduled_sends table) into
what the templates draw. Nothing here sends mail or calls a live API. The only writes are the bulk read/unread update
and the "last checked" timestamp, both local SQLite.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from vcoutreach.web.db import get_conn

LAST_CHECKED_KEY = 'replies_last_checked_at'

# Effective label -> (display text, pill tone class suffix, tab bucket)
_LABELS = {
    'interested': ('Interested', 'success', 'needs'),
    'needs_more_info': ('Needs more info', 'info', 'needs'),
    'needs_info': ('Needs more info', 'info', 'needs'),
    'other': ('Other', 'neutral', 'needs'),
    'unclassified': ('Not classified', 'neutral', 'needs'),
    'unlabeled': ('Not classified', 'neutral', 'needs'),
    'out_of_office': ('Out of Office', 'warning', 'auto'),
    'auto_reply': ('Auto reply', 'warning', 'auto'),
    'bounce': ('Bounce', 'danger', 'bounced'),
    'pass': ('Pass', 'neutral', 'not-interested'),
}

TABS = (
    ('needs', 'Needs response'),
    ('all', 'All'),
    ('auto', 'Out of office or auto'),
    ('bounced', 'Bounced'),
    ('not-interested', 'Not interested'),
)

EMPTY_COPY = {
    'needs': ('Nothing needs a response', 'Interested, needs-more-info and unclassified replies show up here.'),
    'all': ('No replies yet', 'Replies to your outreach appear here after you check for new replies.'),
    'auto': ('No out of office or automatic replies', 'Vacation and auto-responder messages are sorted here.'),
    'bounced': ('No bounces', 'Delivery failures are sorted here so you can fix the address.'),
    'not-interested': ('No one has passed', 'Replies labeled Not interested are sorted here.'),
}


def effective_label(row) -> str:
    return str(row.get('user_label') or row.get('label') or '').strip()


def label_meta(label: str) -> dict:
    text, tone, bucket = _LABELS.get(label, (None, None, None))
    if text is None:
        if label:
            return {'text': label.replace('_', ' ').capitalize(), 'tone': 'neutral', 'bucket': 'needs'}
        return {'text': 'Not classified', 'tone': 'neutral', 'bucket': 'needs'}
    return {'text': text, 'tone': tone, 'bucket': bucket}


def label_options(labels, current: str) -> list[dict]:
    """Options for the pill select. The current label is always present and selected."""
    seen = []
    opts = []
    for value in labels:
        if value in seen:
            continue
        seen.append(value)
        opts.append({'value': value, 'text': label_meta(value)['text'], 'selected': value == current})
    if current and current not in seen:
        opts.insert(0, {'value': current, 'text': label_meta(current)['text'], 'selected': True, 'disabled': True})
    if not current:
        opts.insert(0, {'value': '', 'text': 'Set a label', 'selected': True, 'disabled': False})
    return opts


def iso_utc(value) -> str | None:
    """Return an ISO-8601 string with an explicit UTC offset, or None when the value is not a date. Naive values are UTC."""
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


def utc_text(value) -> str:
    iso = iso_utc(value)
    if not iso:
        return 'Not set'
    return iso[:16].replace('T', ' ') + ' UTC'


def get_last_checked() -> str | None:
    conn = get_conn()
    try:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (LAST_CHECKED_KEY,)).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    return iso_utc(row['value']) if row else None


def record_last_checked() -> None:
    conn = get_conn()
    try:
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (LAST_CHECKED_KEY, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def set_reviewed(reply_ids, reviewed: bool) -> int:
    """Set user_reviewed explicitly (not a toggle) for the given reply ids. Returns the number of rows touched."""
    ids = [int(i) for i in reply_ids if str(i).isdigit()]
    if not ids:
        return 0
    marks = ','.join('?' for _ in ids)
    conn = get_conn()
    try:
        cur = conn.execute(f'UPDATE replies SET user_reviewed=? WHERE id IN ({marks})', [1 if reviewed else 0, *ids])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def safe_replies_next(value) -> str | None:
    """A return path is only honored when it is the Replies list (optionally with a query string)."""
    text = str(value or '').strip()
    if text == '/replies' or text.startswith('/replies?'):
        if '//' in text or '\\' in text or '\n' in text or '\r' in text:
            return None
        return text
    return None


def build_replies_view(rows, tab: str | None, label_filter: str, labels) -> dict:
    """Bucket replies into the five tabs, count them, and prepare each row for drawing."""
    prepared = []
    counts = {key: 0 for key, _ in TABS}
    for raw in rows:
        row = dict(raw)
        label = effective_label(row)
        meta = label_meta(label)
        row['effective_label'] = label
        row['pill'] = meta
        row['bucket'] = meta['bucket']
        row['options'] = label_options(labels, label)
        row['unread'] = not row.get('user_reviewed')
        stamp = row.get('received_at') or row.get('discovered_at')
        row['received_iso'] = iso_utc(stamp)
        row['received_text'] = utc_text(stamp)
        row['display_name'] = row.get('partner_name') or row.get('from_name') or row.get('from_email') or 'Unknown contact'
        counts['all'] += 1
        counts[meta['bucket']] += 1
        prepared.append(row)

    chosen = tab if tab in counts else None
    if label_filter:
        chosen = 'all'
    if chosen is None:
        chosen = 'needs' if counts['needs'] else 'all'

    if label_filter:
        shown = [r for r in prepared if r['effective_label'] == label_filter]
    elif chosen == 'all':
        shown = prepared
    else:
        shown = [r for r in prepared if r['bucket'] == chosen]

    tabs = [{'key': key, 'text': text, 'count': counts[key], 'active': key == chosen} for key, text in TABS]
    empty_title, empty_text = EMPTY_COPY[chosen]
    return {
        'rows': shown,
        'tabs': tabs,
        'active_tab': chosen,
        'total': counts['all'],
        'unread': sum(1 for r in prepared if r['unread']),
        'shown_unread': sum(1 for r in shown if r['unread']),
        'label_filter': label_filter,
        'label_filter_text': label_meta(label_filter)['text'] if label_filter else '',
        'empty_title': empty_title,
        'empty_text': empty_text,
        'return_to': f'/replies?label={quote(label_filter)}' if label_filter else f'/replies?tab={chosen}',
    }


# ── Scheduled sends ───────────────────────────────────────────────────────────────────────────────────────────────

SCHEDULED_TABS = (
    ('upcoming', 'Upcoming'),
    ('done', 'Done'),
    ('failed', 'Failed'),
    ('all', 'All'),
)

_SCHED_STATUS = {
    'pending': ('Pending', 'info', 'upcoming'),
    'running': ('Running', 'warning', 'upcoming'),
    'completed': ('Done', 'success', 'done'),
    'failed': ('Failed', 'danger', 'failed'),
    'cancelled': ('Cancelled', 'neutral', 'done'),
}


def build_scheduled_view(rows, tab: str | None) -> dict:
    prepared = []
    counts = {key: 0 for key, _ in SCHEDULED_TABS}
    for raw in rows:
        row = dict(raw)
        status = str(row.get('status') or '')
        text, tone, bucket = _SCHED_STATUS.get(status, (status or 'Unknown', 'neutral', 'done'))
        result = row.get('result') or {}
        failed = 0
        if isinstance(result, dict):
            try:
                failed = int(result.get('failed', 0) or 0)
            except (TypeError, ValueError):
                failed = 0
        if status == 'completed' and failed:
            bucket = 'failed'
        row['status_text'] = text
        row['status_tone'] = tone
        row['bucket'] = bucket
        row['scheduled_iso'] = iso_utc(row.get('scheduled_for'))
        row['scheduled_text'] = utc_text(row.get('scheduled_for'))
        row['estimated_text'] = utc_text(row.get('estimated_completion')) if row.get('estimated_completion') else ''
        row['mode_live'] = row.get('mode') == 'live'
        counts['all'] += 1
        counts[bucket] += 1
        prepared.append(row)

    chosen = tab if tab in counts else None
    if chosen is None:
        chosen = 'upcoming' if counts['upcoming'] else 'all'
    shown = prepared if chosen == 'all' else [r for r in prepared if r['bucket'] == chosen]
    tabs = [{'key': key, 'text': text, 'count': counts[key], 'active': key == chosen} for key, text in SCHEDULED_TABS]
    return {'rows': shown, 'tabs': tabs, 'active_tab': chosen, 'total': counts['all']}


def wants_json(req) -> bool:
    """True for the inline-save fetch calls in replies.js (they send Accept: application/json)."""
    accept = req.headers.get('Accept', '')
    return 'application/json' in accept or req.headers.get('X-Requested-With', '') == 'fetch'


def tab_counts(rows, labels) -> dict:
    """Counts for the five tabs plus total unread, for the JSON reply after an inline save."""
    view = build_replies_view(rows, 'all', '', labels)
    return {'counts': {t['key']: t['count'] for t in view['tabs']}, 'unread': view['unread']}
