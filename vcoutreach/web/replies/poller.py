"""
Gmail reply poller for VC outreach.

Algorithm:
1. Load all sent drafts (status='sent', gmail_message_id IS NOT NULL).
2. For each sent message, fetch metadata to get threadId.
3. Group by threadId (dedupe).
4. For each thread: fetch full thread, find messages where sender != our address.
5. For each new reply (not in replies table): extract text, classify, save.
6. Fallback: also search INBOX for mail from known partner emails in last 7 days.

Returns a summary dict.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Our sender address - fetched from Gmail profile at poll-time
_MY_EMAIL: str | None = None


def _get_my_email(service) -> str:
    profile = service.users().getProfile(userId='me').execute()
    return profile.get('emailAddress', '').lower()


def _get_header(message: dict, name: str) -> str:
    """Extract a header value from a Gmail message resource."""
    headers = message.get('payload', {}).get('headers', [])
    name_lower = name.lower()
    for h in headers:
        if h.get('name', '').lower() == name_lower:
            return h.get('value', '')
    return ''


def _parse_sender(from_header: str) -> tuple[str, str]:
    """
    Parse 'Display Name <email@example.com>' into (name, email).
    Falls back to (from_header, from_header) if no angle brackets.
    """
    m = re.match(r'^(.*?)\s*<([\w.+\-]+@[\w.\-]+\.\w+)>', from_header.strip())
    if m:
        name = m.group(1).strip().strip('"')
        email = m.group(2).lower()
        return name, email
    # bare email
    m2 = re.search(r'[\w.+\-]+@[\w.\-]+\.\w+', from_header)
    if m2:
        email = m2.group(0).lower()
        return email, email
    return from_header, from_header


def _extract_body_text(message: dict) -> str:
    """Recursively extract text/plain body from a Gmail message payload."""
    payload = message.get('payload', {})

    def _recurse(part: dict) -> str:
        mime = part.get('mimeType', '')
        data = part.get('body', {}).get('data', '')
        if mime == 'text/plain' and data:
            try:
                return base64.urlsafe_b64decode(data + '==').decode('utf-8', errors='replace')
            except Exception:
                return ''
        for sub in part.get('parts', []):
            result = _recurse(sub)
            if result:
                return result
        return ''

    return _recurse(payload)


def _parse_received_at(message: dict) -> str | None:
    """Return ISO-8601 timestamp from the 'Date' header, or from internalDate."""
    date_str = _get_header(message, 'Date')
    if date_str:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(date_str)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    # Fallback: internalDate is milliseconds since epoch
    internal = message.get('internalDate')
    if internal:
        try:
            ts = int(internal) / 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    return None


def _already_saved(gmail_message_id: str) -> bool:
    """Return True if this message id is already in the replies table."""
    from vcoutreach.web.db import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            'SELECT id FROM replies WHERE gmail_message_id = ?',
            (gmail_message_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _lookup_partner_and_draft(from_email: str, thread_id: str):
    """
    Try to find partner_id and draft_id for this reply.

    First, check if any sent draft's gmail_message_id appears in this thread
    (most reliable). Fallback: match partner by from_email.

    Returns (partner_id, draft_id) - either can be None.
    """
    from vcoutreach.web.db import get_conn
    conn = get_conn()
    try:
        # Check by from_email against partners
        partner_row = conn.execute(
            'SELECT id FROM partners WHERE LOWER(email) = ? COLLATE NOCASE LIMIT 1',
            (from_email.lower(),),
        ).fetchone()
        partner_id = partner_row['id'] if partner_row else None

        draft_id = None
        if partner_id:
            draft_row = conn.execute(
                "SELECT id FROM drafts WHERE partner_id = ? AND status IN ('sent', 'live_sent', 'test_sent') ORDER BY id DESC LIMIT 1",
                (partner_id,),
            ).fetchone()
            draft_id = draft_row['id'] if draft_row else None

        return partner_id, draft_id
    finally:
        conn.close()


def _get_partner_name(partner_id: int | None) -> str | None:
    if not partner_id:
        return None
    from vcoutreach.web.db import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            'SELECT partner_name FROM partners WHERE id = ?', (partner_id,)
        ).fetchone()
        return row['partner_name'] if row else None
    finally:
        conn.close()


def _process_reply_message(message: dict, service, my_email: str) -> dict | None:
    """
    Given a Gmail message that is a candidate reply, classify and save it.

    Returns summary dict on success, None if skipped (already saved or is ours).
    """
    msg_id = message.get('id', '')
    thread_id = message.get('threadId', '')

    from_header = _get_header(message, 'From')
    from_name, from_email = _parse_sender(from_header)

    # Skip our own messages
    if from_email == my_email:
        return None

    # Skip if already in DB
    if _already_saved(msg_id):
        return None

    subject = _get_header(message, 'Subject')
    snippet = message.get('snippet', '')
    body_text = _extract_body_text(message)
    received_at = _parse_received_at(message)

    partner_id, draft_id = _lookup_partner_and_draft(from_email, thread_id)
    partner_name = _get_partner_name(partner_id)

    # Classify
    from vcoutreach.web.replies.classifier import classify_reply
    classification = classify_reply(body_text or snippet, partner_name=partner_name)

    # Save
    from vcoutreach.web.replies.loader import save_reply
    reply_id = save_reply(
        gmail_thread_id=thread_id,
        gmail_message_id=msg_id,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        snippet=snippet,
        body_text=body_text,
        received_at=received_at,
        partner_id=partner_id,
        draft_id=draft_id,
        label=classification['label'],
        classification_confidence=classification['confidence'],
        classification_summary=classification['summary'],
        suggested_action=classification['suggested_action'],
    )

    return {
        'reply_id': reply_id,
        'gmail_message_id': msg_id,
        'from_email': from_email,
        'subject': subject,
        'label': classification['label'],
        'partner_id': partner_id,
    }


def poll_gmail_for_replies(limit: int | None = None) -> dict:
    """
    Poll Gmail for replies to sent VC outreach emails.

    Returns:
        {
            'threads_scanned': N,
            'new_replies': M,
            'errors': [...],
            'replies': [list of new reply summaries],
        }
    """
    from vcoutreach.web.gmail.api import get_service, get_message, get_thread, list_threads_in_inbox, GmailAuthNotReady
    from vcoutreach.web.db import get_conn

    try:
        service = get_service()
    except GmailAuthNotReady as exc:
        return {
            'threads_scanned': 0,
            'new_replies': 0,
            'errors': [f'Gmail auth not ready: {exc}'],
            'replies': [],
        }

    my_email = _get_my_email(service)
    errors: list[str] = []
    new_replies: list[dict] = []
    thread_ids_seen: set[str] = set()

    # ── Primary pass: walk sent drafts → get threadIds ─────────────────────
    conn = get_conn()
    try:
        sent_rows = conn.execute(
            "SELECT id, partner_id, gmail_message_id FROM drafts "
            "WHERE status IN ('sent', 'live_sent', 'test_sent') AND gmail_message_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    cap = limit if limit else 200  # hard cap to avoid quota abuse
    processed = 0

    for row in sent_rows:
        if processed >= cap:
            break
        sent_msg_id = row['gmail_message_id']
        try:
            msg_meta = get_message(sent_msg_id, format='metadata')
            thread_id = msg_meta.get('threadId', '')
        except Exception as exc:
            errors.append(f'get_message({sent_msg_id}): {exc}')
            continue

        if not thread_id or thread_id in thread_ids_seen:
            continue
        thread_ids_seen.add(thread_id)

        try:
            thread = get_thread(thread_id)
        except Exception as exc:
            errors.append(f'get_thread({thread_id}): {exc}')
            continue

        processed += 1
        for msg in thread.get('messages', []):
            result = _process_reply_message(msg, service, my_email)
            if result:
                new_replies.append(result)

    # ── Fallback pass: inbox search from known partner emails ─────────────
    conn = get_conn()
    try:
        partner_emails = conn.execute(
            "SELECT email FROM partners WHERE email IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    if partner_emails:
        email_list = [r['email'] for r in partner_emails if r['email']]
        # Gmail OR queries work up to ~200 terms; 47 is fine
        from_clause = ' OR '.join(f'from:{e}' for e in email_list[:50])
        fallback_query = f'in:inbox newer_than:7d ({from_clause})'
        try:
            thread_stubs = list_threads_in_inbox(fallback_query, max_results=50)
            for stub in thread_stubs:
                tid = stub.get('id', '')
                if not tid or tid in thread_ids_seen:
                    continue
                thread_ids_seen.add(tid)
                try:
                    thread = get_thread(tid)
                except Exception as exc:
                    errors.append(f'fallback get_thread({tid}): {exc}')
                    continue
                for msg in thread.get('messages', []):
                    result = _process_reply_message(msg, service, my_email)
                    if result:
                        new_replies.append(result)
        except Exception as exc:
            errors.append(f'Fallback inbox search failed: {exc}')

    return {
        'threads_scanned': len(thread_ids_seen),
        'new_replies': len(new_replies),
        'errors': errors[:20],
        'replies': new_replies,
    }
