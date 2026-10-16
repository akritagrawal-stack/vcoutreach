"""
Dedup guard: prevents re-emailing partners who have already been contacted.

Functions:
- already_contacted_partner_ids(conn) -> set[int]
    Returns the set of partner IDs that have at least one draft with
    status in ('live_sent', 'sent') or with live_sent_at IS NOT NULL.

- filter_uncontacted(conn, partner_ids) -> list[int]
    Returns only the IDs from partner_ids that are NOT in the
    already-contacted set, preserving original order.
"""
from __future__ import annotations

import sqlite3


def already_contacted_partner_ids(conn: sqlite3.Connection) -> set[int]:
    """
    Return the set of partner IDs that must never receive a new campaign email.

    A partner is considered already contacted if any of their drafts satisfies:
      - status IN ('live_sent', 'sent'), OR
      - live_sent_at IS NOT NULL
    """
    rows = conn.execute(
        """
        SELECT DISTINCT partner_id
        FROM drafts
        WHERE status IN ('live_sent', 'sent')
           OR live_sent_at IS NOT NULL
        """
    ).fetchall()
    contacted = {row[0] for row in rows}
    for table, clause in (('web_send_journal', "mode='live' AND state IN ('sending','sent','uncertain')"),
                          ('crm_send_evidence', "evidence_status='sent_confirmed'")):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            contacted.update(row[0] for row in conn.execute(f'SELECT partner_id FROM {table} WHERE {clause}') if row[0] is not None)
    return contacted


def filter_uncontacted(conn: sqlite3.Connection, partner_ids: list[int]) -> list[int]:
    """
    Return only the partner IDs from partner_ids that have NOT been contacted.

    Preserves the original order of partner_ids.
    """
    contacted = already_contacted_partner_ids(conn)
    return [pid for pid in partner_ids if pid not in contacted]
