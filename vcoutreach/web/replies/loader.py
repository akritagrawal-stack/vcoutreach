"""
DB helpers for the replies table.

save_reply()  - upsert one reply row (classify before calling)
list_replies() - list all replies, optional label filter
get_reply()   - single reply by id
count_unreviewed() - badge count for nav
update_reply_user_label() - manual override + mark reviewed
mark_reply_reviewed()     - flip user_reviewed=1
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from vcoutreach.web.db import cursor_ctx, get_conn
from vcoutreach.web.config import DB_PATH

logger = logging.getLogger(__name__)


def save_reply(
    gmail_thread_id: str,
    gmail_message_id: str,
    from_email: str | None,
    from_name: str | None,
    subject: str | None,
    snippet: str | None,
    body_text: str | None,
    received_at: str | None,
    partner_id: int | None,
    draft_id: int | None,
    label: str | None,
    classification_confidence: float | None,
    classification_summary: str | None,
    suggested_action: str | None,
    db_path: Path | None = None,
) -> int | None:
    """
    Insert or update a reply row.  Keyed on gmail_message_id (UNIQUE).

    Returns the reply row id, or None on error.
    """
    now = datetime.now(timezone.utc).isoformat()
    classified_at = now if label else None

    try:
        with cursor_ctx(db_path) as (conn, cur):
            cur.execute(
                """
                INSERT INTO replies (
                    partner_id, draft_id, gmail_thread_id, gmail_message_id,
                    from_email, from_name, subject, snippet, body_text,
                    received_at, label, classification_confidence,
                    classification_summary, suggested_action, classified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gmail_message_id) DO UPDATE SET
                    label                    = excluded.label,
                    classification_confidence = excluded.classification_confidence,
                    classification_summary   = excluded.classification_summary,
                    suggested_action         = excluded.suggested_action,
                    classified_at            = excluded.classified_at
                """,
                (
                    partner_id, draft_id, gmail_thread_id, gmail_message_id,
                    from_email, from_name, subject, snippet, body_text,
                    received_at, label, classification_confidence,
                    classification_summary, suggested_action, classified_at,
                ),
            )
            row = conn.execute(
                'SELECT id FROM replies WHERE gmail_message_id = ?',
                (gmail_message_id,),
            ).fetchone()
            return row['id'] if row else None
    except Exception as exc:
        logger.error('save_reply failed for %s: %s', gmail_message_id, exc)
        return None


def list_replies(
    label: str | None = None,
    db_path: Path | None = None,
) -> list:
    """
    Return all replies joined with partner info, newest first.
    Optionally filter by label.
    """
    conn = get_conn(db_path)
    try:
        base = """
            SELECT r.*,
                   p.partner_name, p.firm, p.first_name
            FROM replies r
            LEFT JOIN partners p ON p.id = r.partner_id
        """
        if label:
            rows = conn.execute(
                base + " WHERE COALESCE(NULLIF(r.user_label,''),r.label) = ? ORDER BY r.received_at DESC, r.id DESC",
                (label,),
            ).fetchall()
        else:
            rows = conn.execute(
                base + ' ORDER BY r.received_at DESC, r.id DESC'
            ).fetchall()
        return rows
    finally:
        conn.close()


def get_reply(reply_id: int, db_path: Path | None = None):
    """Return a single reply row joined with partner info."""
    conn = get_conn(db_path)
    try:
        return conn.execute(
            """
            SELECT r.*, p.partner_name, p.firm, p.first_name, p.email AS partner_email
            FROM replies r
            LEFT JOIN partners p ON p.id = r.partner_id
            WHERE r.id = ?
            """,
            (reply_id,),
        ).fetchone()
    finally:
        conn.close()


def count_unreviewed(db_path: Path | None = None) -> int:
    """Return number of replies not yet reviewed by the user (for nav badge)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            'SELECT COUNT(*) AS cnt FROM replies WHERE user_reviewed = 0'
        ).fetchone()
        return row['cnt'] if row else 0
    finally:
        conn.close()


def count_unreviewed_by_label(db_path: Path | None = None) -> dict:
    """
    Return breakdown of unreviewed replies by label.

    Returns:
        {
            'total': N,
            'interested': N,
            'needs_more_info': N,
            'pass': N,
            'auto_reply': N,
            'bounce': N,
            'other': N,
        }
    """
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(user_label, label, 'other') AS effective_label,
                   COUNT(*) AS cnt
            FROM replies
            WHERE user_reviewed = 0
            GROUP BY effective_label
            """
        ).fetchall()
        breakdown: dict[str, int] = {}
        total = 0
        for row in rows:
            lbl = row['effective_label'] or 'other'
            cnt = row['cnt'] or 0
            breakdown[lbl] = cnt
            total += cnt
        return {
            'total': total,
            'interested': breakdown.get('interested', 0),
            'needs_more_info': breakdown.get('needs_more_info', 0),
            'pass': breakdown.get('pass', 0),
            'auto_reply': breakdown.get('auto_reply', 0),
            'bounce': breakdown.get('bounce', 0),
            'out_of_office': breakdown.get('out_of_office', 0),
            'other': breakdown.get('other', 0),
        }
    except Exception as exc:
        logger.error('count_unreviewed_by_label failed: %s', exc)
        return {
            'total': 0, 'interested': 0, 'needs_more_info': 0,
            'pass': 0, 'auto_reply': 0, 'bounce': 0, 'out_of_office': 0, 'other': 0,
        }
    finally:
        conn.close()


def update_reply_user_label(
    reply_id: int,
    user_label: str,
    db_path: Path | None = None,
) -> None:
    """Set user's manual label override and mark as reviewed."""
    from vcoutreach.web.replies.classifier import VALID_LABELS
    if user_label not in VALID_LABELS | {'unlabeled', 'needs_info'}:
        raise ValueError('Invalid reply label.')
    with cursor_ctx(db_path) as (_, cur):
        cur.execute(
            'UPDATE replies SET user_label = ?, user_reviewed = 1 WHERE id = ?',
            (user_label, reply_id),
        )


def mark_reply_reviewed(reply_id: int, db_path: Path | None = None) -> None:
    """Toggle user_reviewed without changing user_label."""
    with cursor_ctx(db_path) as (_, cur):
        cur.execute(
            'UPDATE replies SET user_reviewed = 1 - COALESCE(user_reviewed, 0) WHERE id = ?',
            (reply_id,),
        )
