"""
DB CRUD helpers for partners and drafts.

All functions accept an optional db_path; defaults to config.DB_PATH.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vcoutreach.web.db import cursor_ctx, get_conn
from vcoutreach.web.config import DB_PATH, get_workspace

logger = logging.getLogger(__name__)


# ── Partners ──────────────────────────────────────────────────────────────────

def upsert_partner(
    partner_name: str,
    first_name: str,
    firm: str,
    email: str,
    tier: int,
    responsiveness: str | None,
    channel: str = 'email',
    email_uncertain: int = 0,
    notes: str | None = None,
    source: str = 'import',
    db_path: Path | None = None,
) -> int:
    """
    Insert or update a partner row by email (case-insensitive).

    Returns the partner's id.
    """
    email_lower = email.strip().lower()
    with cursor_ctx(db_path) as (conn, cur):
        cur.execute(
            """
            INSERT INTO partners
                (partner_name, first_name, firm, email, tier, responsiveness,
                 channel, email_uncertain, notes, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                partner_name      = excluded.partner_name,
                first_name        = excluded.first_name,
                firm              = excluded.firm,
                tier              = excluded.tier,
                responsiveness    = excluded.responsiveness,
                channel           = excluded.channel,
                email_uncertain   = excluded.email_uncertain,
                notes             = excluded.notes,
                source            = excluded.source
            """,
            (partner_name, first_name, firm, email_lower, tier, responsiveness,
             channel, email_uncertain, notes, source),
        )
        # Fetch the id (works for both insert and update)
        row = conn.execute(
            'SELECT id FROM partners WHERE email = ? COLLATE NOCASE', (email_lower,)
        ).fetchone()
        from vcoutreach.contacts import _slug
        cur.execute('UPDATE partners SET slug=COALESCE(slug,?) WHERE id=?', (_slug(email_lower),row['id']))
        return row['id']


def get_partner(partner_id: int, db_path: Path | None = None) -> sqlite3.Row | None:
    conn = get_conn(db_path)
    try:
        return conn.execute(
            'SELECT * FROM partners WHERE id = ?', (partner_id,)
        ).fetchone()
    finally:
        conn.close()


def get_all_partners(db_path: Path | None = None) -> list[sqlite3.Row]:
    conn = get_conn(db_path)
    try:
        return conn.execute(
            'SELECT * FROM partners ORDER BY tier, id'
        ).fetchall()
    finally:
        conn.close()


# ── Drafts ────────────────────────────────────────────────────────────────────

def _apply_auto_approve_gate(
    draft_id: int,
    partner_id: int,
    hook: str,
    subject: str,
    db_path: Path | None = None,
) -> bool:
    """
    Evaluate quality gates for a newly inserted draft and flip status to
    'approved' if all gates pass, or record auto_approve_skip_reason if not.

    Gates (in order):
      1. partner.needs_enrichment == 1                          → skip: 'needs_enrichment'
      2. email_verification_status NOT IN ('valid','deliverable') → skip: 'needs_validator_confirmation'
         (Blocks: mx_pattern_match, mx_only, inconclusive, risky, invalid, NULL)
      3. enrichment source count < 2                            → skip: 'insufficient_enrichment'
      4. validate_hook or validate_subject fails                → skip: 'validation_failed'
      5. hallucination judge returns grounded=False             → skip: 'hallucination_detected'
      All pass → status='approved', approved_at=now, review_notes='auto-approved'

    Returns True if auto-approved, False otherwise.
    Errors are logged to data/logs/gate_errors.log and status left as 'pending'.
    """
    import json as _json
    from pathlib import Path as _Path

    def _gate_log(msg: str) -> None:
        try:
            log_dir = get_workspace().path('data','logs')
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(get_workspace().path('data','logs','gate_errors.log'), 'a', encoding='utf-8') as _f:
                from datetime import datetime as _dt, timezone as _tz
                _f.write(f'{_dt.now(_tz.utc).isoformat()} draft gate stopped; review workspace settings\n')
        except Exception:
            pass

    try:
        conn = get_conn(db_path)
        try:
            partner = conn.execute(
                'SELECT * FROM partners WHERE id=?', (partner_id,)
            ).fetchone()
        finally:
            conn.close()

        if partner is None:
            _gate_log(f'draft={draft_id} partner={partner_id} not found - leaving pending')
            return False

        skip_reason: str | None = None
        hallucination_result: dict | None = None

        # Gate 1: needs_enrichment flag
        if partner['needs_enrichment'] == 1:
            skip_reason = 'needs_enrichment'

        # Gate 2: email verification status (hardened: only valid/deliverable/pb_verified allowed)
        # Blocks: mx_pattern_match, mx_only, inconclusive, risky, invalid, and NULL
        elif partner['email_verification_status'] not in ('valid', 'deliverable', 'pb_verified'):
            skip_reason = 'needs_validator_confirmation'

        else:
            # Gate 3: enrichment source count
            conn = get_conn(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT source) AS cnt
                    FROM enrichment
                    WHERE partner_id=?
                      AND facts_json IS NOT NULL
                      AND facts_json != '[]'
                    """,
                    (partner_id,),
                ).fetchone()
            finally:
                conn.close()

            sources = row['cnt'] if row else 0

            if sources < 2:
                skip_reason = 'insufficient_enrichment'
            else:
                # Gate 4: validate hook and subject
                from vcoutreach.web.llm.validation import validate_hook, validate_subject
                hook_ok, _hook_reason = validate_hook(hook)
                subj_ok, _subj_reason = validate_subject(subject)

                if not hook_ok or not subj_ok:
                    skip_reason = 'validation_failed'
                else:
                    # Gate 5a: personal-stat deterministic gate (v2.0 drafts only)
                    conn = get_conn(db_path)
                    try:
                        bv_row = conn.execute(
                            'SELECT boilerplate_version FROM drafts WHERE id=?',
                            (draft_id,),
                        ).fetchone()
                    finally:
                        conn.close()

                    boilerplate_version = bv_row['boilerplate_version'] if bv_row else None

                    if boilerplate_version == '2.0':
                        from vcoutreach.web.llm.hallucination_judge import check_personal_claims
                        personal_result = check_personal_claims(hook)
                        if not personal_result['ok']:
                            skip_reason = 'personal_stat_violation'

                    if skip_reason is None:
                        # Gate 5b: hallucination judge (LLM-based, target-grounding)
                        from vcoutreach.web.enrichment.orchestrator import get_enrichment_facts
                        from vcoutreach.web.llm.hallucination_judge import judge_hook_grounded

                        facts = get_enrichment_facts(partner_id, db_path=db_path)
                        try:
                            hallucination_result = judge_hook_grounded(hook, facts)
                        except Exception as judge_exc:
                            # Fail-closed: if judge raises unexpectedly, treat as unsupported
                            _gate_log(
                                f'draft={draft_id} partner={partner_id} judge_raised={judge_exc!r}'
                            )
                            hallucination_result = {
                                'grounded': False,
                                'unsupported_claims': [f'judge_raised: {judge_exc}'],
                                'confidence': 0.0,
                                'claims': [],
                            }

                        # Always store the judge result
                        conn = get_conn(db_path)
                        try:
                            conn.execute(
                                'UPDATE drafts SET hallucination_check=? WHERE id=?',
                                (_json.dumps(hallucination_result), draft_id),
                            )
                            conn.commit()
                        finally:
                            conn.close()

                        # Use any_unsupported (partner-claims-only flag) if present;
                        # fall back to legacy grounded=False for backward compat.
                        if 'any_unsupported' in hallucination_result:
                            has_hallucination = hallucination_result['any_unsupported']
                        else:
                            has_hallucination = not hallucination_result.get('grounded', False)
                        if has_hallucination:
                            skip_reason = 'hallucination_detected'
                        else:
                            skip_reason = None

        # Apply outcome
        now = datetime.now(timezone.utc).isoformat()
        if skip_reason is None:
            conn = get_conn(db_path)
            try:
                conn.execute(
                    """
                    UPDATE drafts
                    SET status='approved', approved_at=?, review_notes='auto-approved',
                        auto_approve_skip_reason=NULL
                    WHERE id=?
                    """,
                    (now, draft_id),
                )
                conn.commit()
            finally:
                conn.close()
            logger.info(
                'auto_approve_gate: draft=%d partner=%d AUTO-APPROVED', draft_id, partner_id
            )
            return True
        else:
            conn = get_conn(db_path)
            try:
                conn.execute(
                    'UPDATE drafts SET auto_approve_skip_reason=? WHERE id=?',
                    (skip_reason, draft_id),
                )
                conn.commit()
            finally:
                conn.close()
            logger.info(
                'auto_approve_gate: draft=%d partner=%d skipped reason=%s',
                draft_id, partner_id, skip_reason,
            )
            return False

    except Exception as exc:
        _gate_log(
            f'draft={draft_id} partner={partner_id} gate_crash={exc!r}'
        )
        logger.warning(
            'auto_approve_gate: unexpected error for draft=%d partner=%d: %s',
            draft_id, partner_id, exc,
        )
        return False


def upsert_draft(
    partner_id: int,
    hook: str,
    subject: str,
    boilerplate_version: str,
    sender_account: str = 'sender',
    db_path: Path | None = None,
    skip_auto_approve: bool = False,
) -> int:
    """
    Insert draft for (partner_id, boilerplate_version) or update hook/subject if exists.
    Does NOT change status if draft already exists (preserves user's review state).

    After insert/update, runs the auto-approve gate unless skip_auto_approve=True.
    The gate may flip status to 'approved' if all quality checks pass; otherwise
    the draft stays 'pending' and auto_approve_skip_reason is recorded.

    Returns draft id.
    """
    with cursor_ctx(db_path) as (conn, cur):
        cur.execute(
            """
            INSERT INTO drafts
                (partner_id, hook, subject, boilerplate_version, sender_account, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(partner_id, boilerplate_version) DO UPDATE SET
                hook           = excluded.hook,
                subject        = excluded.subject,
                sender_account = excluded.sender_account
            """,
            (partner_id, hook, subject, boilerplate_version, sender_account),
        )
        row = conn.execute(
            'SELECT id FROM drafts WHERE partner_id=? AND boilerplate_version=?',
            (partner_id, boilerplate_version),
        ).fetchone()
        draft_id = row['id']

    if not skip_auto_approve:
        try:
            _apply_auto_approve_gate(draft_id, partner_id, hook, subject, db_path)
        except Exception as gate_exc:
            logger.warning(
                'auto_approve_gate crashed for draft %d / partner %d: %s',
                draft_id, partner_id, gate_exc,
            )

    return draft_id


def get_draft(draft_id: int, db_path: Path | None = None) -> sqlite3.Row | None:
    conn = get_conn(db_path)
    try:
        return conn.execute(
            'SELECT * FROM drafts WHERE id = ?', (draft_id,)
        ).fetchone()
    finally:
        conn.close()


def get_draft_for_partner(partner_id: int, db_path: Path | None = None) -> sqlite3.Row | None:
    """Return the most recent draft for a partner."""
    conn = get_conn(db_path)
    try:
        return conn.execute(
            'SELECT * FROM drafts WHERE partner_id=? ORDER BY id DESC LIMIT 1',
            (partner_id,),
        ).fetchone()
    finally:
        conn.close()


def get_all_drafts_with_partners(db_path: Path | None = None) -> list[sqlite3.Row]:
    """Join drafts + partners for list view. Ordered by p.id ASC."""
    conn = get_conn(db_path)
    try:
        return conn.execute(
            """
            SELECT d.*, p.partner_name, p.first_name, p.firm, p.email AS partner_email,
                   p.tier, p.responsiveness, p.channel, p.email_uncertain, p.notes,
                   p.verification_status, p.email_verification_status, p.dropped_reason
            FROM drafts d
            JOIN partners p ON p.id = d.partner_id
            ORDER BY p.id ASC
            """
        ).fetchall()
    finally:
        conn.close()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table using PRAGMA."""
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    return any(r[1] == column for r in rows)


def get_all_drafts_with_partners_filtered(
    show_dropped: bool = False,
    db_path: Path | None = None,
) -> tuple[list[sqlite3.Row], dict]:
    """
    Join drafts + partners for list view.
    - Ordered by p.id ASC (fixes sort order complaint)
    - Hides dropped partners by default (show_dropped=True to include)
    - Includes verification_status and dropped_reason if columns exist (tolerant)
    - Returns (rows, counts_dict) where counts_dict has 'verified' and 'dropped'
    """
    conn = get_conn(db_path)
    try:
        has_dropped = _has_column(conn, 'partners', 'dropped_reason')
        has_verif = _has_column(conn, 'partners', 'verification_status')
        has_email_verif = _has_column(conn, 'partners', 'email_verification_status')

        # Build SELECT for optional columns
        extra_select = ''
        if has_verif:
            extra_select += ', p.verification_status'
        if has_dropped:
            extra_select += ', p.dropped_reason'
        if has_email_verif:
            extra_select += ', p.email_verification_status, p.email_verification_confidence, p.email_verified_at'

        # Build WHERE for dropped filter
        where_clause = ''
        if has_dropped and not show_dropped:
            where_clause = 'WHERE p.dropped_reason IS NULL'

        query = f"""
            SELECT d.*, p.partner_name, p.first_name, p.firm, p.email AS partner_email,
                   p.tier, p.responsiveness, p.channel, p.email_uncertain, p.notes
                   {extra_select}
            FROM drafts d
            JOIN partners p ON p.id = d.partner_id
            {where_clause}
            ORDER BY p.id ASC
        """
        # NOTE: d.* already includes live_sent_at, live_sent_to, live_message_id,
        # live_subject, test_sent_count, last_test_sent_at after migration 009.
        raw_rows = conn.execute(query).fetchall()
        # Convert to plain dicts so templates can use .get() safely
        rows = [dict(r) for r in raw_rows]

        # Compute counts for counter display
        counts: dict = {}
        try:
            if has_verif:
                verif_rows = conn.execute(
                    """SELECT verification_status, COUNT(*) AS cnt FROM partners
                       GROUP BY verification_status"""
                ).fetchall()
                verified_count = sum(
                    r['cnt'] for r in verif_rows
                    if r['verification_status'] in ('verified_strong', 'verified')
                )
                counts['verified'] = verified_count
            else:
                counts['verified'] = 0
        except Exception:
            counts['verified'] = 0

        try:
            if has_dropped:
                dropped_row = conn.execute(
                    'SELECT COUNT(*) AS cnt FROM partners WHERE dropped_reason IS NOT NULL'
                ).fetchone()
                counts['dropped'] = dropped_row['cnt'] if dropped_row else 0
            else:
                counts['dropped'] = 0
        except Exception:
            counts['dropped'] = 0

        return rows, counts
    finally:
        conn.close()


def update_draft_hook_subject(
    draft_id: int,
    hook: str,
    subject: str,
    db_path: Path | None = None,
    skip_gate: bool = False,
) -> None:
    """
    Save hook/subject edits. Does NOT change status unless skip_gate=False.

    If skip_gate=False (default), runs the auto-approve gate after updating.
    The gate may flip status to 'approved' if all quality checks pass; otherwise
    the draft stays 'pending' and auto_approve_skip_reason is recorded.

    Set skip_gate=True to bypass the gate (e.g. in bulk migrations where you'll
    run gate separately or want to preserve existing status).
    """
    # First, get partner_id from the draft so we can apply gate
    conn = get_conn(db_path)
    try:
        draft = conn.execute(
            'SELECT partner_id,boilerplate_version FROM drafts WHERE id=?', (draft_id,)
        ).fetchone()
        if draft is None:
            raise ValueError(f'Draft {draft_id} not found.')
        partner_id = draft['partner_id']
    finally:
        conn.close()

    if draft['boilerplate_version'].startswith('engine-'):
        from vcoutreach.web.drafts_service import get_engine_draft,save_draft
        partner=get_partner(partner_id,db_path)
        previous=get_engine_draft(partner['slug'])
        # Legacy editor exposes the whole body. Hook regeneration supplies only one paragraph.
        body=hook
        if '\n\n' not in hook:
            parts=previous.body.split('\n\n')
            if len(parts)>1:
                parts[1]=hook
                body='\n\n'.join(parts)
        save_draft(partner['slug'],{'subject':subject,'body':body})
        return

    # Update hook/subject
    with cursor_ctx(db_path) as (_, cur):
        cur.execute(
            'UPDATE drafts SET hook=?, subject=? WHERE id=?',
            (hook, subject, draft_id),
        )

    # Apply gate unless caller opted out
    if not skip_gate:
        try:
            _apply_auto_approve_gate(draft_id, partner_id, hook, subject, db_path)
        except Exception as gate_exc:
            logger.warning(
                'auto_approve_gate crashed for draft %d / partner %d: %s',
                draft_id, partner_id, gate_exc,
            )


def set_draft_status(
    draft_id: int,
    status: str,
    review_notes: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Set draft status. Also sets approved_at if status='approved'."""
    now = datetime.now(timezone.utc).isoformat()
    with cursor_ctx(db_path) as (_, cur):
        if status == 'approved':
            cur.execute(
                'UPDATE drafts SET status=?, review_notes=?, approved_at=? WHERE id=?',
                (status, review_notes, now, draft_id),
            )
        else:
            cur.execute(
                'UPDATE drafts SET status=?, review_notes=? WHERE id=?',
                (status, review_notes, draft_id),
            )


def mark_drafted(
    draft_id: int,
    gmail_draft_id: str,
    db_path: Path | None = None,
) -> None:
    """Mark a draft as pushed to Gmail Drafts folder (legacy - draft preview mode)."""
    now = datetime.now(timezone.utc).isoformat()
    with cursor_ctx(db_path) as (_, cur):
        cur.execute(
            "UPDATE drafts SET status='drafted', gmail_message_id=?, drafted_at=? WHERE id=?",
            (gmail_draft_id, now, draft_id),
        )


def mark_sent(
    draft_id: int,
    gmail_message_id: str,
    db_path: Path | None = None,
) -> None:
    """Mark a draft as sent - sets status='sent', sent_at, and gmail_message_id."""
    now = datetime.now(timezone.utc).isoformat()
    with cursor_ctx(db_path) as (_, cur):
        cur.execute(
            "UPDATE drafts SET status='sent', gmail_message_id=?, sent_at=? WHERE id=?",
            (gmail_message_id, now, draft_id),
        )


def mark_sent_with_address(
    draft_id: int,
    gmail_message_id: str,
    sent_to_address: str,
    db_path: Path | None = None,
) -> None:
    """Mark a draft as sent, recording the actual recipient address.

    Legacy function preserved for backward compatibility.
    For new code prefer mark_test_sent() or mark_live_sent().
    """
    now = datetime.now(timezone.utc).isoformat()
    with cursor_ctx(db_path) as (conn, cur):
        # If this is a live send (live_sent_at IS NULL), also set live columns.
        # If live_sent_at already set, only update legacy cols if they reference
        # a test address - never overwrite live_sent_at.
        row = conn.execute(
            'SELECT live_sent_at FROM drafts WHERE id=?', (draft_id,)
        ).fetchone()
        live_already_set = row and row['live_sent_at'] is not None

        if not live_already_set:
            # First send ever - treat as live send (write live columns too)
            cur.execute(
                """UPDATE drafts
                   SET status='sent', gmail_message_id=?, sent_at=?, sent_to_address=?
                   WHERE id=?""",
                (gmail_message_id, now, sent_to_address, draft_id),
            )
        else:
            # live_sent_at already locked in - only update legacy cols
            cur.execute(
                """UPDATE drafts
                   SET status='sent', gmail_message_id=?, sent_at=?, sent_to_address=?
                   WHERE id=?""",
                (gmail_message_id, now, sent_to_address, draft_id),
            )


def mark_test_sent(
    draft_id: int,
    gmail_message_id: str,
    test_address: str,
    db_path: Path | None = None,
) -> None:
    """Record a test send (to test inbox).

    - Increments test_sent_count and updates last_test_sent_at.
    - Sets status='sent' and updates legacy sent_at/sent_to_address/gmail_message_id
      ONLY when live_sent_at IS NULL (i.e. no real send has happened yet).
    - NEVER overwrites live_sent_at / live_sent_to / live_message_id / live_subject.
    """
    now = datetime.now(timezone.utc).isoformat()
    with cursor_ctx(db_path) as (conn, cur):
        row = conn.execute(
            'SELECT live_sent_at, test_sent_count FROM drafts WHERE id=?', (draft_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f'Draft {draft_id} not found.')

        live_locked = row['live_sent_at'] is not None
        current_count = row['test_sent_count'] or 0

        if live_locked:
            # Live send already recorded - only update test counters; leave
            # legacy sent_at/gmail_message_id pointing at the live send data.
            cur.execute(
                """UPDATE drafts
                   SET test_sent_count = ?,
                       last_test_sent_at = ?
                   WHERE id=?""",
                (current_count + 1, now, draft_id),
            )
        else:
            # No live send yet - update all tracking columns
            cur.execute(
                """UPDATE drafts
                   SET status='test_sent',
                       gmail_message_id=?,
                       sent_at=?,
                       sent_to_address=?,
                       test_sent_count=?,
                       last_test_sent_at=?
                   WHERE id=?""",
                (gmail_message_id, now, test_address, current_count + 1, now, draft_id),
            )


def mark_live_sent(
    draft_id: int,
    gmail_message_id: str,
    live_address: str,
    subject: str,
    db_path: Path | None = None,
    force: bool = False,
) -> None:
    """Record a live send to a real partner email.

    Write-once: raises ValueError if live_sent_at is already set unless force=True.
    Updates legacy sent_at/sent_to_address/gmail_message_id for backwards compat.
    """
    now = datetime.now(timezone.utc).isoformat()
    with cursor_ctx(db_path) as (conn, cur):
        row = conn.execute(
            'SELECT live_sent_at FROM drafts WHERE id=?', (draft_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f'Draft {draft_id} not found.')

        if row['live_sent_at'] is not None:
            raise ValueError(
                f'Draft {draft_id} already has a live send recorded '
                f'(live_sent_at={row["live_sent_at"]}). '
                f'Pass force=True to override (admin only).'
            )

        cur.execute(
            """UPDATE drafts
               SET status='live_sent',
                   gmail_message_id=?,
                   sent_at=?,
                   sent_to_address=?,
                   live_sent_at=?,
                   live_sent_to=?,
                   live_message_id=?,
                   live_subject=?
               WHERE id=?""",
            (gmail_message_id, now, live_address,
             now, live_address, gmail_message_id, subject,
             draft_id),
        )


def get_status_counts(db_path: Path | None = None) -> dict[str, int]:
    """Return counts per status plus total."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            'SELECT status, COUNT(*) AS cnt FROM drafts GROUP BY status'
        ).fetchall()
        counts = {r['status']: r['cnt'] for r in rows}
        counts['total'] = sum(counts.values())
        return counts
    finally:
        conn.close()


def get_drafts_by_ids(draft_ids: list[int], db_path: Path | None = None) -> list[sqlite3.Row]:
    """Return drafts joined with partners for a list of draft IDs."""
    if not draft_ids:
        return []
    conn = get_conn(db_path)
    try:
        placeholders = ','.join('?' * len(draft_ids))
        return conn.execute(
            f"""
            SELECT d.*, p.partner_name, p.first_name, p.firm,
                   p.email AS partner_email, p.tier, p.channel,
                   p.email_verification_status
            FROM drafts d
            JOIN partners p ON p.id = d.partner_id
            WHERE d.id IN ({placeholders})
            ORDER BY d.id ASC
            """,
            draft_ids,
        ).fetchall()
    finally:
        conn.close()


def get_approved_drafts(limit: int | None = None, db_path: Path | None = None) -> list[sqlite3.Row]:
    """
    Return approved drafts joined with partners, ordered by id.

    Send-time gate (hardened email verification): filters out drafts for partners
    with weak email verification, even if the draft was previously auto-approved.
    Only sends to partners with email_verification_status IN ('valid', 'deliverable', 'pb_verified').

    Blocks: mx_pattern_match, mx_only, inconclusive, risky, invalid, NULL.
    """
    conn = get_conn(db_path)
    try:
        q = """
            SELECT d.*, p.partner_name, p.first_name, p.firm,
                   p.email AS partner_email, p.tier, p.channel,
                   p.email_verification_status
            FROM drafts d
            JOIN partners p ON p.id = d.partner_id
            WHERE d.status = 'approved'
              AND p.channel = 'email'
              AND p.dropped_reason IS NULL
              AND NOT EXISTS (SELECT 1 FROM crm_profiles cp WHERE cp.partner_id=p.id AND cp.stage IN ('passed','hold'))
              AND p.email_verification_status IN ('valid', 'deliverable', 'pb_verified')
            ORDER BY d.id ASC
        """
        if limit:
            q += f' LIMIT {int(limit)}'
        return conn.execute(q).fetchall()
    finally:
        conn.close()


def count_drafted_today(db_path: Path | None = None) -> int:
    """Count emails sent or drafted to Gmail today (UTC date). Used for daily cap."""
    today = datetime.now(timezone.utc).date().isoformat()
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM drafts
            WHERE (status IN ('sent', 'test_sent', 'live_sent') AND sent_at LIKE ?)
               OR (status='drafted' AND drafted_at LIKE ?)
            """,
            (f'{today}%', f'{today}%'),
        ).fetchone()
        return row['cnt']
    finally:
        conn.close()


def count_sent_today(db_path: Path | None = None) -> int:
    """Count emails actually sent today (UTC date)."""
    today = datetime.now(timezone.utc).date().isoformat()
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM drafts
               WHERE status IN ('sent', 'test_sent', 'live_sent') AND sent_at LIKE ?""",
            (f'{today}%',),
        ).fetchone()
        return row['cnt']
    finally:
        conn.close()
