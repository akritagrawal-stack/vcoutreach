"""Publik Signal workspace application and compatibility routes."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from vcoutreach.web.config import DB_PATH, WorkspacePath, configure_workspace, get_workspace, send_mode, daily_send_cap
from vcoutreach.web.crm_store import STAGES as CRM_STAGES
from vcoutreach.web.crm_store import add_activity as crm_add_activity
from vcoutreach.web.crm_store import complete_task as crm_complete_task
from vcoutreach.web.crm_store import get_contact as crm_get_contact
from vcoutreach.web.crm_store import list_contacts as crm_list_contacts
from vcoutreach.web.crm_store import save_profile as crm_save_profile
from vcoutreach.web.crm_tracking import TRACKING_ORIGIN, TrackingUnavailable, fetch_contact_tracking, fetch_tracking_snapshot
from vcoutreach.web.crm_live_sync import refresh_inbox as crm_refresh_inbox, sync_status as crm_sync_status
from vcoutreach.web.crm_analytics import (
    campaign_funnel as crm_campaign_funnel,
    insights as crm_insights,
    lead_scores as crm_lead_scores,
    segment_performance as crm_segment_performance,
    sync_tracking_events as crm_sync_tracking_events,
    tracking_coverage as crm_tracking_coverage,
)
from vcoutreach.web.crm_alternate_investors import find_alternate_investors
from vcoutreach.web.boilerplate_loader import get_boilerplate
from vcoutreach.web.email_assembler import assemble_email
from vcoutreach.web.pipeline.loader import (
    get_all_drafts_with_partners,
    get_draft,
    get_partner,
    get_status_counts,
    set_draft_status,
    update_draft_hook_subject,
)
from vcoutreach.web.server.workspace_settings import (
    AUDIENCE_TYPES,
    DISCOVERY_SOURCES,
    load_settings,
    reset_settings,
    save_settings,
    setup_instructions,
)
from vcoutreach.web.server.publik_campaign import filter_publik_replies, load_publik_campaign
from vcoutreach.web.server.gemini_quality import (
    GeminiQualityError,
    load_gemini_status,
    run_quality_probe,
)
from vcoutreach.web.server.runtime_secrets import (
    RuntimeSecretError,
    data_provider_statuses,
    gemini_key_configured,
    load_data_provider_key,
    provider_statuses,
    store_data_provider_key,
    store_provider_key,
    store_gemini_key,
)
from vcoutreach.web.server.sample_email import build_sample_email

from vcoutreach.web.keys import write_env, key_status, KEY_NAMES
from vcoutreach.web.security import install_security
from vcoutreach.web import drafts_service
from vcoutreach.web.db import init_db, get_conn, cursor_ctx

_PROJECT_ROOT = WorkspacePath()
_DEMO_RUN_ROOT = WorkspacePath('outputs','browser-runs')
_RELATIONSHIP_UPDATES_PATH = WorkspacePath('data','relationship-updates.json')
_OUTREACH_RUN_ROOT = WorkspacePath('outputs','outreach-runs')
_CRM_CURRENT_REVIEW_MANIFEST = WorkspacePath('outputs','review','manifest.json')
_SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / 'scripts'
logger = logging.getLogger(__name__)
_EMAIL_PATTERN = re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.I)

def _env(name, default=''):
    return get_workspace().value(name,default)

def _activate_pitchbook_browser(tabs=None):
    # Browser launcher handles focusing. Never execute arbitrary AppleScript.
    return bool(tabs)
def _bounded_int(value: str | None, default: int, minimum: int = 0, maximum: int = 9999) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))

def _crm_confirmed_send_count(db_path: Path) -> int:
    """Count linked send receipts the CRM has confirmed."""
    from vcoutreach.web.db import get_conn

    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM crm_send_evidence WHERE evidence_status = ?",
            ('sent_confirmed',),
        ).fetchone()
        return int(row['count'] if row else 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()

def _crm_inbound_match_count(db_path: Path) -> int:
    """Count exact-address inbound metadata matches, not investor replies."""
    from vcoutreach.web.db import get_conn

    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS count FROM crm_mail_evidence").fetchone()
        return int(row['count'] if row else 0)
    finally:
        conn.close()

def _pitchbook_cdp_port() -> int:
    return _bounded_int(
        _env('PITCHBOOK_CDP_PORT'),
        default=9223,
        minimum=1024,
        maximum=65535,
    )

def _pitchbook_cdp_url() -> str:
    return f'http://127.0.0.1:{_pitchbook_cdp_port()}'

def _pitchbook_cdp_tabs() -> list[dict]:
    """Return safe CDP tab metadata from the dedicated installed Chrome."""

    try:
        with urlopen(f'{_pitchbook_cdp_url()}/json/list', timeout=1.5) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]

def _pitchbook_page_tabs(tabs: list[dict] | None = None) -> list[dict]:
    """Return real PitchBook page targets, excluding workers and blobs."""

    source = tabs if tabs is not None else _pitchbook_cdp_tabs()
    return [
        row
        for row in source
        if row.get('type') == 'page'
        and 'pitchbook.com' in str(row.get('url', '')).lower()
    ]

def _load_relationship_updates() -> dict[str, dict]:
    try:
        payload = json.loads(_RELATIONSHIP_UPDATES_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict) and value.get('status') in {'due', 'complete'}
    }

def _write_relationship_updates(payload: dict[str, dict]) -> None:
    _RELATIONSHIP_UPDATES_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _RELATIONSHIP_UPDATES_PATH.parent.chmod(0o700)
    temporary = _RELATIONSHIP_UPDATES_PATH.with_suffix('.tmp')
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(',', ':')),
        encoding='utf-8',
    )
    temporary.chmod(0o600)
    temporary.replace(_RELATIONSHIP_UPDATES_PATH)
    _RELATIONSHIP_UPDATES_PATH.chmod(0o600)

def _redact_dashboard_text(value: str | None) -> str:
    return _EMAIL_PATTERN.sub('[email hidden]', value or '')

def _safe_outreach_state(payload: dict | None = None) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    allowed_statuses = {'idle', 'running', 'stopped', 'complete', 'attention_required'}
    status = str(source.get('status') or 'idle')
    if status not in allowed_statuses:
        status = 'attention_required'
    completed_steps = source.get('completed_steps')
    if not isinstance(completed_steps, list):
        completed_steps = []
    safe_steps = [
        step
        for step in completed_steps
        if step in {'source', 'research', 'verify', 'personalize', 'review'}
    ]
    counts = source.get('counts') if isinstance(source.get('counts'), dict) else {}
    return {
        'status': status,
        'mode': 'review_only',
        'started_at': str(source.get('started_at') or '')[:40],
        'completed_at': str(source.get('completed_at') or '')[:40],
        'current_step': str(source.get('current_step') or 'idle')[:40],
        'completed_steps': safe_steps,
        'counts': {
            name: _bounded_int(str(counts.get(name, 0)), 0, maximum=100000)
            for name in ('source', 'research', 'verify', 'personalize', 'review')
        },
        'delivery_authority': False,
        'provider_calls': False,
        'gmail_accessed': False,
    }

def _read_outreach_state(path: Path | None) -> dict[str, object]:
    if path is None:
        candidates = sorted(_OUTREACH_RUN_ROOT.glob('*/status.json'), reverse=True)
        path = candidates[0] if candidates else None
    if path is None:
        return _safe_outreach_state()
    try:
        path = get_workspace().path(path)
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError):
        return _safe_outreach_state({'status': 'attention_required'})
    return _safe_outreach_state(payload)



def _local_provider_statuses(data=False):
    from vcoutreach.web.server.runtime_secrets import MODEL_PROVIDERS,DATA_PROVIDERS
    specifications=DATA_PROVIDERS if data else MODEL_PROVIDERS
    return [dict(spec,id=name,configured=bool(_env(spec['env_var']))) for name,spec in specifications.items()]

def _selected_draft_ids():
    raw=request.form.getlist('draft_ids') or request.form.getlist('draft_ids[]')
    return list(dict.fromkeys(int(value) for value in raw if value.isdigit()))

def _show_send_result(result):
    flash(f"Sent {result['sent']}; refused or failed {result['failed']}.",'success' if result['sent'] else 'warning')
    for error in result.get('errors',[])[:5]:
        flash(error,'error')

def _gmail_connection_state() -> str:
    """Settings chip for Gmail. Reads local workspace files only (no network); never returns or logs token contents."""
    data_dir = get_workspace().path('data')
    token_path = data_dir / '.gmail_token.json'
    credentials_path = data_dir / '.gmail_credentials.json'
    token_exists = token_path.is_file()
    if token_exists:
        try:
            token = json.loads(token_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            token = None
        refresh_token = token.get('refresh_token') if isinstance(token, dict) else None
        if isinstance(refresh_token, str) and refresh_token.strip():
            return 'connected'
    if not token_exists and not credentials_path.is_file():
        return 'add OAuth client'
    return 'OAuth required'

def create_app(workspace=None, testing=False, send_transport=None) -> Flask:
    app = Flask(__name__, template_folder='templates', static_folder='static')
    workspace = configure_workspace(workspace or os.environ.get('OUTREACH_HOME') or Path.cwd())
    workspace.initialize()
    values = workspace.env()
    write_env({'FLASK_SECRET_KEY': values.get('FLASK_SECRET_KEY') or secrets.token_hex(32), 'SEND_MODE': 'test'}, workspace, internal=True)
    app.secret_key = workspace.env()['FLASK_SECRET_KEY']
    app.config.update(TESTING=testing, CRM_DB_PATH=workspace.db_path, WORKSPACE=workspace.root,
                      SESSION_NONCE=secrets.token_urlsafe(24))
    app.extensions['workspace'] = workspace
    app.extensions['send_transport'] = send_transport
    with app.app_context():
        init_db()
    install_security(app)
    drafts_service.register_routes(app)

    @app.get('/healthz')
    def healthz():
        return {'ok': True}

    # ── CSRF: ensure a per-session token exists ───────────────────────────────

    # ── CSRF: verify token on all state-mutating POST requests ───────────────

    # ── Context processor - expose csrf_token to every template ─────────────
    @app.context_processor
    def inject_csrf():
        return {'csrf_token': session.get('csrf', '')}

    # ── Context processor - inject unread_replies count into every template ──
    @app.context_processor
    def inject_unread_replies():
        from vcoutreach.web.replies.loader import count_unreviewed_by_label
        counts=count_unreviewed_by_label()
        return {'unread_replies':counts['total'],'unread_interested':counts['interested'],'unread_by_label':counts}

    # ── Context processor - inject send_mode + test_recipient globally ────────
    @app.context_processor
    def inject_send_mode():
        return {'g_send_mode':send_mode(),'g_test_recipient':_env('TEST_RECIPIENT_EMAIL')}

    @app.context_processor
    def inject_workspace():
        return {'g_workspace': load_settings()}

    # ── Context processor - inject sent_to_map for sent badge disambiguation ──
    @app.context_processor
    def inject_sent_to_map():
        """
        Returns a dict {draft_id: sent_to_address} for all drafts that have a
        sent_to_address cached. Templates use this to show 'test-sent' vs 'sent'.
        """
        try:
            from vcoutreach.web.db import get_conn
            conn = get_conn()
            try:
                rows = conn.execute(
                    'SELECT id, sent_to_address FROM drafts WHERE sent_to_address IS NOT NULL'
                ).fetchall()
            finally:
                conn.close()
            return {'sent_to_map': {r[0]: r[1] for r in rows}}
        except Exception:
            return {'sent_to_map': {}}

    # ── Context processor - inject candidates_unreviewed count globally ──────
    @app.context_processor
    def inject_candidates_count():
        """Count CSV rows not yet imported or skipped, for the nav badge."""
        try:
            import csv
            import json
            csv_path = _PROJECT_ROOT / 'data' / 'candidates.csv'
            tracker_path = _PROJECT_ROOT / 'data' / 'imported_candidates.json'
            try:
                tracker = json.loads(tracker_path.read_text(encoding='utf-8'))
            except (FileNotFoundError, json.JSONDecodeError):
                tracker = {'imported': [], 'skipped': []}
            already_reviewed = set(tracker.get('imported', [])) | set(tracker.get('skipped', []))
            total = 0
            if csv_path.exists():
                with open(csv_path, newline='', encoding='utf-8') as f:
                    total = sum(1 for _ in csv.DictReader(f))
            unreviewed = max(0, total - len(already_reviewed))
            return {'candidates_unreviewed': unreviewed}
        except Exception:
            return {'candidates_unreviewed': 0}

    # ── Context processor - inject pending scheduled sends count ────────────
    @app.context_processor
    def inject_pending_schedules():
        try:
            from vcoutreach.web.gmail.scheduler import count_pending_scheduled
            return {'pending_schedules': count_pending_scheduled()}
        except Exception:
            return {'pending_schedules': 0}

    # ── Context processor - inject PitchBook usage into every template ──────
    @app.context_processor
    def inject_pitchbook_usage():
        try:
            from vcoutreach.web.enrichment.pitchbook import _get_today_lookups, MAX_LOOKUPS_PER_DAY
            used = _get_today_lookups()
            pb_enabled = _env('PITCHBOOK_ENABLED', '0') == '1'
            return {
                'pitchbook_lookups_used': used,
                'pitchbook_lookups_cap': MAX_LOOKUPS_PER_DAY,
                'pitchbook_enabled': pb_enabled,
            }
        except Exception:
            return {
                'pitchbook_lookups_used': 0,
                'pitchbook_lookups_cap': 150,
                'pitchbook_enabled': False,
            }

    # ── Dashboard ─────────────────────────────────────────────────────────────


    @app.route('/')
    def dashboard():
        # The old operator-era dashboard has historical campaign counters.
        # Open the current investor CRM instead of showing stale totals.
        return redirect(url_for('crm_dashboard'))

    @app.get('/crm')
    def crm_dashboard():
        view = request.args.get('view', 'current').strip()
        if view not in {'current', 'all', 'warm', 'hot', 'due', 'follow_up_candidates', 'investor_list', 'unreplied', 'new_outreach', 'sent', 'sent_emails', 'best_fit', 'best_fit_awaiting', 'reviewed_fit', 'followup_watchlist', 'followup_next', 'new_research'}:
            view = 'current'
        stage = request.args.get('stage', '').strip()
        if stage not in CRM_STAGES:
            stage = ''
        query = request.args.get('q', '').strip()[:120]
        page = _bounded_int(request.args.get('page'), default=1, minimum=1, maximum=10000)
        tag = request.args.get('tag', '').strip()[:160]
        draft = request.args.get('draft', '')
        if draft not in {'ok', 'blocked', 'none'}:
            draft = ''
        conn = get_conn(app.config['CRM_DB_PATH'])
        try:
            batches = [dict(row) for row in conn.execute('SELECT name, created_at FROM batches ORDER BY id DESC')]
        finally:
            conn.close()
        rows, counts = crm_list_contacts(
            q=query, view=view, stage=stage, page=page, tag=tag, draft=draft,
            db_path=app.config['CRM_DB_PATH'],
        )
        try:
            leads = crm_lead_scores(app.config['CRM_DB_PATH'])
            hot_leads_preview = [row for row in leads if row['bucket'] == 'hot'][:5]
            replied_count = sum(1 for row in leads if row['bucket'] == 'replied')
        except Exception:
            app.logger.warning('Hot leads preview unavailable', exc_info=False)
            hot_leads_preview, replied_count = [], 0
        return render_template(
            'crm_dashboard.html', rows=rows, counts=counts,
            filters={'q': query, 'view': view, 'stage': stage, 'tag': tag, 'draft': draft}, batches=batches,
            stages=CRM_STAGES,
            confirmed_send_count=_crm_confirmed_send_count(app.config['CRM_DB_PATH']),
            inbound_match_count=_crm_inbound_match_count(app.config['CRM_DB_PATH']),
            inbox_sync_status=crm_sync_status(app.config['CRM_DB_PATH']),
            hot_leads_preview=hot_leads_preview,
            replied_count=replied_count,
            active_page='crm',
        )

    @app.post('/crm/refresh-inbox')
    def crm_refresh_inbox_route():
        try:
            result = crm_refresh_inbox(app.config['CRM_DB_PATH'])
        except Exception as exc:
            app.logger.warning('CRM inbox refresh unavailable: %s', type(exc).__name__)
            flash('Gmail inbox refresh unavailable. Local CRM records are unchanged.', 'warning')
        else:
            status = 'complete' if result['scan_complete'] else 'partial (message cap reached)'
            flash(f"Gmail inbox refresh {status}: {result['scanned_messages']} messages checked, "
                  f"{result['inserted']} new exact-address matches.", 'success')
        return redirect(url_for('crm_dashboard'))

    @app.get('/crm/drafts')
    def crm_drafts():
        drafts=drafts_service.list_drafts()
        status=request.args.get('status',request.args.get('type','all'))
        query=request.args.get('q','').strip()
        counts={key:sum(row['status']==key for row in drafts) for key in ('ok','blocked')}
        counts['all']=len(drafts)
        rows=[row for row in drafts if (status not in {'ok','blocked'} or row['status']==status)
              and (not query or query.casefold() in (row['recipient_name']+' '+row['subject']+' '+row['firm']).casefold())]
        from vcoutreach.providers import configured_providers
        providers=configured_providers(drafts_service.engine_settings())
        return render_template('crm_drafts.html',drafts=rows,draft_count=len(rows),draft_counts=counts,
            type_filter=status,status_filter=status,query=query,providers=['auto','template','publik','openai','anthropic'],
            provider_status=', '.join(providers) + ' configured; no key uses a placeholder template',active_page='drafts')

    @app.get('/crm/tracking')
    def crm_tracking():
        # GET only reads the snapshot cached by the explicit sync action.
        snapshot=get_workspace().read_json('data/tracking-snapshot.json')
        return render_template('crm_tracking.html',snapshot=snapshot,error=None if snapshot else 'Sync tracking to load your Worker data.',
            tracking_dashboard_url=_env('TRACKING_WORKER_URL',_env('TRACKING_ORIGIN')) + '/dashboard',active_page='tracking')

    @app.get('/crm/review-preview')
    def crm_review_preview():
        # Exported HTML is user-controlled. Render it as text, never execute scripts.
        preview=get_workspace().path('outputs','review','REVIEW_PREVIEW.html')
        if not preview.is_file():
            abort(404)
        return Response(preview.read_text(encoding='utf-8'),mimetype='text/plain')

    @app.get('/crm/analytics')
    def crm_analytics():
        db_path = app.config['CRM_DB_PATH']
        now = datetime.now(timezone.utc)
        leads = crm_lead_scores(db_path, now)
        buckets = {key: [] for key in ('hot', 'warm', 'engaged', 'cold', 'untracked', 'replied')}
        for row in leads:
            buckets.setdefault(row['bucket'], []).append(row)
        funnel = crm_campaign_funnel(db_path)
        segments = crm_segment_performance(db_path)
        coverage = crm_tracking_coverage(db_path, now)
        insight_lists = crm_insights(funnel, segments, coverage)
        from collections import Counter
        conn = get_conn(db_path)
        try:
            stage_counts = {stage: 0 for stage in CRM_STAGES}
            for row in conn.execute("SELECT COALESCE(cp.stage, 'prospect') AS stage, COUNT(*) AS n FROM partners p LEFT JOIN crm_profiles cp ON cp.partner_id=p.id GROUP BY COALESCE(cp.stage, 'prospect')"):
                stage_counts[row['stage']] = row['n']
            tag_counts = Counter()
            for row in conn.execute('SELECT tags FROM partners'):
                tag_counts.update({tag.strip() for tag in (row['tags'] or '').split(',') if tag.strip()})
        finally:
            conn.close()
        draft_rows = drafts_service.list_drafts()
        quality = Counter(row['status'] for row in draft_rows)
        lint_codes = Counter(issue['code'] for row in draft_rows for issue in row['issues'])
        return render_template(
            'crm_analytics.html',
            hot=buckets['hot'], warm=buckets['warm'], engaged=buckets['engaged'], cold=buckets['cold'],
            untracked=buckets['untracked'], replied=buckets['replied'],
            funnel=funnel, segments=segments, coverage=coverage, insights=insight_lists,
            stage_counts=stage_counts, tag_counts=tag_counts, quality=quality, lint_codes=lint_codes.most_common(),
            active_page='analytics',
        )

    @app.post('/crm/analytics/sync-tracking')
    def crm_analytics_sync_tracking():
        try:
            status_counts = crm_sync_tracking_events(app.config['CRM_DB_PATH'])
            snapshot = fetch_tracking_snapshot()
            get_workspace().path('data','tracking-snapshot.json').write_text(json.dumps(snapshot),encoding='utf-8')
        except Exception as exc:
            app.logger.warning('Tracking events sync unavailable: %s', type(exc).__name__)
            flash('Tracking events sync unavailable right now. Existing analytics are unchanged.', 'warning')
        else:
            flash(
                f"Tracking events sync: {status_counts.get('ok', 0)} ok, "
                f"{status_counts.get('endpoint_missing', 0)} endpoint not deployed yet, "
                f"{status_counts.get('unavailable', 0)} unavailable.", 'success',
            )
        return redirect(url_for('crm_analytics'))

    @app.get('/crm/<int:partner_id>')
    def crm_contact(partner_id: int):
        detail = crm_get_contact(partner_id, db_path=app.config['CRM_DB_PATH'])
        if detail is None:
            abort(404)
        detail['engine_contact'] = drafts_service.contact_from_row(detail['partner'])
        detail['enrow_url'] = url_for('email_verification', q=detail['partner']['partner_name'])
        if detail['partner'].get('email'):
            detail['tracking'] = {'status':'not_synced','reason':'Use Sync tracking to refresh Worker data.'}
        conn = get_conn(app.config['CRM_DB_PATH'])
        try:
            uncertain_sends = [dict(row) for row in conn.execute("SELECT id,mode,lane,target_email,created_at FROM web_send_journal WHERE partner_id=? AND state='uncertain' ORDER BY id", (partner_id,))]
        finally:
            conn.close()
        response = app.make_response(render_template('crm_contact.html', **detail, uncertain_sends=uncertain_sends, stages=CRM_STAGES, active_page='crm'))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/crm/<int:partner_id>/alternate-investors')
    def crm_alternate_investors(partner_id: int):
        if crm_get_contact(partner_id, db_path=app.config['CRM_DB_PATH']) is None:
            abort(404)
        try:
            result = find_alternate_investors(
                partner_id, db_path=app.config['CRM_DB_PATH'], include_official_team=False,
            )
        except Exception:
            app.logger.warning('Official firm-team scan unavailable')
            result = find_alternate_investors(partner_id, db_path=app.config['CRM_DB_PATH'])
            result['official_status'] = 'scan_unavailable'
        return render_template(
            'crm_alternate_investors.html', result=result,
            active_page='crm',
        )

    @app.post('/partners/<int:partner_id>/verify-email-manually')
    def partner_verify_email_manually(partner_id: int):
        from vcoutreach.contacts import _EMAIL_RE
        detail = crm_get_contact(partner_id, db_path=app.config['CRM_DB_PATH'])
        if detail is None:
            abort(404)
        if not _EMAIL_RE.fullmatch(detail['partner'].get('email') or ''):
            flash('Add a valid email address before marking it verified.', 'error')
        else:
            with cursor_ctx(app.config['CRM_DB_PATH']) as (_, cur):
                cur.execute("UPDATE partners SET email_verification_status='valid', email_verification_method='manual', email_verified_at=CURRENT_TIMESTAMP WHERE id=?", (partner_id,))
            crm_add_activity(partner_id, 'note', 'Email verified manually', 'I checked it myself: ' + detail['partner']['email'], db_path=app.config['CRM_DB_PATH'])
            flash('Email marked verified by your manual attestation.', 'success')
        return redirect(url_for('partners_list') if request.form.get('return_to') == 'partners' else url_for('crm_contact', partner_id=partner_id))

    @app.post('/crm/<int:partner_id>/resolve-send')
    def crm_resolve_uncertain_send(partner_id: int):
        from vcoutreach.web.gmail.push_worker import allow_retry_after_sent_check, SendRefused
        if crm_get_contact(partner_id, db_path=app.config['CRM_DB_PATH']) is None:
            abort(404)
        try:
            attempt_id = int(request.form.get('attempt_id', ''))
            if request.form.get('action') != 'allow_retry':
                raise ValueError()
        except ValueError:
            abort(400)
        try:
            allow_retry_after_sent_check(partner_id, attempt_id)
        except SendRefused as exc:
            flash(str(exc), 'error')
        else:
            flash('Sent check recorded. This attempt is retryable; review the draft before sending.', 'success')
        return redirect(url_for('crm_contact', partner_id=partner_id))

    @app.post('/crm/<int:partner_id>/profile')
    def crm_profile_update(partner_id: int):
        if crm_get_contact(partner_id, db_path=app.config['CRM_DB_PATH']) is None:
            abort(404)
        def money(name: str) -> int | None:
            raw = request.form.get(name, '').strip()
            if not raw:
                return None
            value = int(raw)
            if value < 0 or value > 1000000000:
                raise ValueError('Check size is outside the accepted range')
            return value
        try:
            fields = {}
            for name, limit in (('tags', 1000), ('notes', 10000), ('next_action', 1000)):
                if name in request.form:
                    value = request.form[name].strip()
                    if len(value) > limit or (name == 'tags' and any(c in value for c in '\r\n')):
                        raise ValueError('Invalid contact field.')
                    fields[name] = value
            due = request.form.get('next_follow_up_at', '').strip() or None
            if due:
                datetime.fromisoformat(due)
            crm_save_profile(partner_id, {
                'stage': request.form.get('stage', 'prospect').strip(),
                'fit_grade': request.form.get('fit_grade', 'unreviewed').strip().lower(),
                'fit_rationale': request.form.get('fit_rationale', '').strip()[:1000],
                'on_update_list': request.form.get('on_update_list') == '1',
                'check_size_min': money('check_size_min'),
                'check_size_max': money('check_size_max'),
                'fund_status': request.form.get('fund_status', '').strip()[:150],
                'next_follow_up_at': due,
                'follow_up_reason': request.form.get('follow_up_reason', '').strip()[:500],
            }, db_path=app.config['CRM_DB_PATH'])
        except (ValueError, OverflowError):
            flash('Check the stage, fit grade, check size, and follow-up date.', 'error')
        else:
            if fields:
                with cursor_ctx(app.config['CRM_DB_PATH']) as (_, cur):
                    assignments = ','.join(name + '=?' for name in fields)
                    cur.execute('UPDATE partners SET ' + assignments + ' WHERE id=?', (*fields.values(), partner_id))
            flash('Relationship profile saved locally.', 'success')
        return redirect(url_for('crm_contact', partner_id=partner_id))

    @app.post('/crm/<int:partner_id>/activity')
    def crm_activity_add(partner_id: int):
        if crm_get_contact(partner_id, db_path=app.config['CRM_DB_PATH']) is None:
            abort(404)
        try:
            crm_add_activity(
                partner_id,
                request.form.get('activity_type', '').strip(),
                request.form.get('subject', '').strip(),
                request.form.get('snippet', '').strip(),
                db_path=app.config['CRM_DB_PATH'],
            )
        except ValueError:
            flash('Choose a valid activity type.', 'error')
        else:
            flash('Activity saved locally.', 'success')
        return redirect(url_for('crm_contact', partner_id=partner_id))

    @app.post('/crm/tasks/<int:task_id>/complete')
    def crm_task_complete(task_id: int):
        partner_id = crm_complete_task(task_id, db_path=app.config['CRM_DB_PATH'])
        if partner_id is None:
            abort(404)
        flash('Follow-up marked complete.', 'success')
        return redirect(url_for('crm_contact', partner_id=partner_id))

    @app.get('/setup')
    @app.get('/settings')
    @app.get('/knowledge')
    def workspace_settings():
        settings=load_settings()
        from vcoutreach.web.enrichment.contact_provider_store import connection_health
        connections={'database':'ready','pitch deck':'ready' if settings.get('pitch_deck_path') and Path(settings['pitch_deck_path']).is_file() else 'not set',
                     'pitchbook':'connect on click','gmail':_gmail_connection_state(),'calendar':'manual snapshot'}
        return render_template('settings.html',settings=settings,audiences=AUDIENCE_TYPES,discovery_sources=DISCOVERY_SOURCES,
            instructions=setup_instructions(settings,pitchbook_ready=False,calendar_live=False),connections=connections,
            sample_email=build_sample_email(settings),gemini_status=load_gemini_status(configured=bool(_env('GEMINI_API_KEY'))),
            model_providers=_local_provider_statuses(),data_providers=_local_provider_statuses(data=True),data_provider_health=connection_health(),
            outreach_status=_safe_outreach_state(),outreach_cli='vcoutreach draft --contacts contacts.csv --sender sender.json',
            sender=drafts_service.sender_dict(),sender_fields=drafts_service.SENDER_FIELDS,key_status=key_status(),key_names=sorted(KEY_NAMES),active_page='settings')

    @app.post('/settings')
    def update_workspace_settings():
        try:
            save_settings(request.form.to_dict(flat=True))
            flash('Workspace configuration saved.','success')
        except (ValueError, OSError):
            flash('Invalid workspace settings. Files must stay inside the workspace.','error')
        return redirect(url_for('workspace_settings'))

    @app.post('/settings/reset')
    def reset_workspace_settings():
        reset_settings()
        flash('Workspace restored to the editable example configuration.', 'success')
        return redirect(url_for('workspace_settings'))

    @app.post('/demo/sample-email')
    def demo_sample_email():
        """Preview the current unsaved knowledge-base form without persisting it."""

        return build_sample_email(request.form.to_dict(flat=True))

    @app.post('/api/gemini/key')
    def store_dashboard_gemini_key():
        try:
            write_env({'GEMINI_API_KEY':request.form.get('api_key','')})
        except ValueError as exc:
            return {'ok':False,'error':str(exc)},400
        return {'ok':True,'status':'configured','storage':'workspace_env','secret_stored_in_workspace':True}

    @app.post('/api/model-provider/key')
    def store_dashboard_model_provider_key():
        provider=request.form.get('provider','').strip().lower()
        names={'gemini': 'GEMINI_API_KEY', 'openai': 'OPENAI_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'openrouter': 'OPENROUTER_API_KEY', 'publik': 'PUBLIK_API_KEY'}
        if provider not in names:
            return {'ok':False,'error':'Unknown provider.'},400
        try:
            write_env({names[provider]:request.form.get('api_key','')})
        except ValueError as exc:
            return {'ok':False,'error':str(exc)},400
        return {'ok':True,'provider':provider,'status':'configured','storage':'workspace_env','secret_stored_in_workspace':True}

    @app.post('/api/data-provider/key')
    def store_dashboard_data_provider_key():
        provider=request.form.get('provider','').strip().lower()
        names={'enrow': 'ENROW_API_KEY', 'apollo': 'APOLLO_API_KEY'}
        if provider not in names:
            return {'ok':False,'error':'Unknown provider.'},400
        try:
            write_env({names[provider]:request.form.get('api_key','')})
        except ValueError as exc:
            return {'ok':False,'error':str(exc)},400
        return {'ok':True,'provider':provider,'status':'configured','storage':'workspace_env','secret_stored_in_workspace':True}

    @app.post('/api/data-provider/test')
    def test_dashboard_data_provider():
        """Run a documented zero-credit provider authentication check."""

        provider = request.form.get('provider', '').strip().lower()
        try:
            key = load_data_provider_key(provider)
            if key is None:
                raise RuntimeSecretError('provider_key_not_configured')
            from vcoutreach.web.enrichment.contact_provider_clients import test_provider_connection
            from vcoutreach.web.enrichment.contact_provider_store import record_connection_health
            result = test_provider_connection(provider, api_key=key)
            record_connection_health(provider=provider, result=result)
        except (RuntimeSecretError, ValueError) as exc:
            return {
                'ok': False,
                'provider': provider[:24],
                'status': 'attention_required',
                'error_code': str(exc),
                'secret_stored_in_workspace': False,
                'credit_consuming': False,
                'delivery_authority': False,
            }, 400
        status_code = 200 if result.get('ok') else 409
        return {
            **result,
            'provider': provider,
            'secret_stored_in_workspace': False,
            'credit_consuming': False,
            'delivery_authority': False,
        }, status_code

    @app.get('/api/gemini/status')
    def dashboard_gemini_status():
        configured = bool(_env('GEMINI_API_KEY'))
        return load_gemini_status(configured=configured)

    @app.post('/api/gemini/test')
    def test_dashboard_gemini():
        """Benchmark anonymized campaign emails with no delivery authority."""

        try:
            result = run_quality_probe()
        except GeminiQualityError as exc:
            configured = bool(_env('GEMINI_API_KEY'))
            status = load_gemini_status(configured=configured)
            return {
                **status,
                'ok': False,
                'error_code': str(exc),
                'raw_content_stored': False,
                'delivery_authority': False,
            }, 400
        return result

    @app.post('/demo/outreach/start')
    def start_dashboard_outreach():
        """Start the local review-only outreach preparation CLI."""

        if send_mode() != 'test':
            return {
                'ok': False,
                'status': 'attention_required',
                'message': 'Return to TEST MODE before starting outreach preparation.',
                'delivery_authority': False,
            }, 409
        runs = app.extensions.setdefault('dashboard_outreach_runs', {})
        current = runs.get('current')
        process = current.get('process') if isinstance(current, dict) else None
        if process is not None and process.poll() is None:
            return {
                'ok': True,
                **_read_outreach_state(Path(str(current['state_path']))),
            }, 202

        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        run_dir = _OUTREACH_RUN_ROOT / stamp
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        run_dir.chmod(0o700)
        state_path = run_dir / 'status.json'
        log_path = run_dir / 'run.log'
        log_handle = log_path.open('xb')
        log_path.chmod(0o600)
        command = [
            sys.executable,
            str(_SCRIPTS_ROOT / 'publik_dashboard_outreach_run.py'),
            '--output',
            str(state_path),
            '--step-delay',
            '1.0',
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(_PROJECT_ROOT),
                env={**os.environ, **get_workspace().env(), 'SEND_MODE': 'test', 'OUTREACH_HOME': str(get_workspace().root)},
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            log_handle.close()
            return {
                'ok': False,
                'status': 'attention_required',
                'message': 'The local outreach preparation CLI could not start.',
                'delivery_authority': False,
            }, 500
        log_handle.close()
        runs['current'] = {
            'process': process,
            'state_path': str(state_path),
            'log_path': str(log_path),
        }
        return {
            'ok': True,
            'status': 'running',
            'mode': 'review_only',
            'delivery_authority': False,
            'message': 'Local outreach preparation started in draft-only mode.',
        }, 202

    @app.post('/demo/outreach/stop')
    def stop_dashboard_outreach():
        """Stop the active local review-only outreach process."""

        runs = app.extensions.setdefault('dashboard_outreach_runs', {})
        current = runs.get('current')
        process = current.get('process') if isinstance(current, dict) else None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        state_path = (
            Path(str(current.get('state_path')))
            if isinstance(current, dict) and current.get('state_path')
            else None
        )
        return {'ok': True, **_read_outreach_state(state_path)}

    @app.get('/demo/outreach/status')
    def dashboard_outreach_status():
        runs = app.extensions.setdefault('dashboard_outreach_runs', {})
        current = runs.get('current')
        state_path = (
            Path(str(current.get('state_path')))
            if isinstance(current, dict) and current.get('state_path')
            else None
        )
        return {'ok': True, **_read_outreach_state(state_path)}

    @app.post('/relationships/<int:partner_id>/toggle-update')
    def toggle_relationship_update(partner_id: int):
        """Add or remove an investor from the private post-meeting update queue."""

        partner = get_partner(partner_id)
        if partner is None:
            abort(404)
        updates = _load_relationship_updates()
        key = str(partner_id)
        current = updates.get(key, {})
        if current.get('status') == 'due':
            updates[key] = {
                'status': 'complete',
                'completed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
            flash('Post-meeting update marked complete.', 'success')
        else:
            updates[key] = {
                'status': 'due',
                'added_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            }
            flash('Investor added to the post-meeting update queue.', 'success')
        _write_relationship_updates(updates)
        return redirect(url_for('dashboard') + '#relationshipDesk')

    @app.post('/demo/pitchbook-visible')
    def demo_pitchbook_visible():
        """Launch the dedicated installed Chrome profile used by PitchBook."""

        runs = app.extensions.setdefault('visible_demo_runs', {})
        existing_tabs = _pitchbook_cdp_tabs()
        if existing_tabs:
            current = runs.setdefault('pitchbook', {
                'process': None,
                'started_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            })
            _activate_pitchbook_browser(existing_tabs)
            refresh_process = current.get('refresh_process')
            if refresh_process is None or refresh_process.poll() is not None:
                command=[sys.executable,str(_SCRIPTS_ROOT/'pb_cookie_refresher.py'),'--once','--no-navigate','--cdp-url',_pitchbook_cdp_url()]
                try:
                    refresh_process=subprocess.Popen(command,cwd=str(get_workspace().root),
                        env={**os.environ,**get_workspace().env(),'OUTREACH_HOME':str(get_workspace().root)},
                        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
                    current['refresh_process']=refresh_process
                except OSError:
                    return {'ok':False,'status':'attention_required','message':'PitchBook session refresh could not start.'},400
            refresh_code = refresh_process.poll() if refresh_process is not None else None
            if refresh_process is not None and refresh_code == 0:
                return {
                    'ok': True,
                    'status': 'session_ready',
                    'message': 'PitchBook is ready and its dedicated Chrome window is in front.',
                }
            return {
                'ok': True,
                'status': 'browser_open',
                'message': 'PitchBook is open and its dedicated Chrome window is in front.',
            }, 202
        current = runs.get('pitchbook')
        process = current.get('process') if isinstance(current, dict) else None
        if process is not None and process.poll() is None:
            return {
                'ok': True,
                'status': 'browser_open',
                'message': 'PitchBook browser is already open.',
            }, 202

        _DEMO_RUN_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        _DEMO_RUN_ROOT.chmod(0o700)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        log_path = _DEMO_RUN_ROOT / f'pitchbook-visible-{stamp}.log'
        log_handle = log_path.open('xb')
        log_path.chmod(0o600)
        command = [
            '/bin/zsh',
            str(_SCRIPTS_ROOT / 'launch_pb_chrome_mac.sh'),
        ]
        environment = {**os.environ, **get_workspace().env(), 'OUTREACH_HOME': str(get_workspace().root)}
        environment['PITCHBOOK_CDP_PORT'] = str(_pitchbook_cdp_port())
        try:
            process = subprocess.Popen(
                command,
                cwd=str(_PROJECT_ROOT),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError:
            log_handle.close()
            logger.warning('Could not launch visible PitchBook flow')
            return {
                'ok': False,
                'status': 'launch_failed',
                'message': 'The visible PitchBook browser could not be opened.',
            }, 500
        log_handle.close()
        runs['pitchbook'] = {
            'process': process,
            'started_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'log_path': str(log_path),
        }
        return {
            'ok': True,
            'status': 'browser_open',
            'message': 'Dedicated Google Chrome is opening. Complete PitchBook sign-in there.',
        }, 202

    @app.get('/demo/pitchbook-status')
    def demo_pitchbook_status():
        current=app.extensions.get('visible_demo_runs',{}).get('pitchbook',{})
        process=current.get('process')
        return {'ok':True,'status':'browser_open' if process is not None and process.poll() is None else 'idle',
                'message':'Use Open PitchBook to connect or refresh the browser session.'}

    # Contact provider management

    @app.get('/email-verification')
    def email_verification():
        from vcoutreach.web.enrichment.contact_provider_store import (
            connection_health,
            contact_queue,
            list_attempts,
            provider_summary,
        )

        query_value = request.args.get('q', '').strip()
        status_value = request.args.get('status', '').strip().lower()
        try:
            contacts = contact_queue(query=query_value, status=status_value, limit=150)
        except ValueError:
            status_value = ''
            contacts = contact_queue(query=query_value, limit=150)
        return render_template(
            'email_verification.html',
            contacts=contacts,
            attempts=list_attempts(limit=100),
            provider_summary=provider_summary(),
            data_providers=_local_provider_statuses(data=True),
            data_provider_health=connection_health(),
            filters={'q': query_value, 'status': status_value},
            active_page='verification',
        )

    @app.post('/email-verification/<int:partner_id>/run')
    def run_contact_provider(partner_id: int):
        provider = request.form.get('provider', '').strip().lower()
        operation = request.form.get('operation', '').strip().lower()
        allowed = {
            ('apollo', 'person_enrich'),
            ('enrow', 'email_find'),
            ('enrow', 'email_verify'),
        }
        if (provider, operation) not in allowed:
            flash('Provider operation is not allowed.', 'error')
            return redirect(url_for('email_verification'))

        partner_row = get_partner(partner_id)
        if partner_row is None:
            flash(f'Contact {partner_id} was not found.', 'error')
            return redirect(url_for('email_verification'))
        partner = dict(partner_row)
        try:
            key = load_data_provider_key(provider)
            if key is None:
                raise RuntimeSecretError('provider_key_not_configured')
            from vcoutreach.web.enrichment.contact_provider_clients import (
                run_apollo_person_enrichment,
                run_enrow_email_find,
                run_enrow_email_verification,
            )
            from vcoutreach.web.enrichment.contact_provider_store import record_attempt
            if provider == 'apollo':
                result = run_apollo_person_enrichment(partner, api_key=key)
            elif operation == 'email_find':
                result = run_enrow_email_find(partner, api_key=key)
            else:
                result = run_enrow_email_verification(partner, api_key=key)
            attempt = record_attempt(
                partner_id=partner_id,
                provider=provider,
                operation=operation,
                result=result,
                idempotency_key=f'{provider}:{operation}:{partner_id}:{secrets.token_hex(16)}',
            )
        except (RuntimeSecretError, ValueError) as exc:
            flash(f'Provider lookup stopped: check your settings.', 'error')
            return redirect(url_for('email_verification', q=partner.get('partner_name', '')))

        state = str(attempt.get('state') or 'unknown').replace('_', ' ')
        if attempt.get('candidate_disposition') == 'review_required':
            flash(f'{provider.title()} returned a different address. Review it before applying.', 'warning')
        elif attempt.get('send_gate_effect') == 'eligible':
            flash('Enrow verified the current address as deliverable.', 'success')
        elif attempt.get('send_gate_effect') == 'blocked':
            flash(f'Enrow verification returned {state}. The address remains blocked.', 'warning')
        else:
            flash(f'{provider.title()} completed with state: {state}.', 'success')
        return redirect(url_for('email_verification', q=partner.get('partner_name', '')))

    @app.post('/email-verification/attempts/<int:attempt_id>/apply')
    def apply_contact_provider_candidate(attempt_id: int):
        from vcoutreach.web.enrichment.contact_provider_store import apply_candidate

        try:
            result = apply_candidate(
                attempt_id=attempt_id,
                confirmation=request.form.get('confirmation', ''),
            )
        except ValueError as exc:
            flash(f'Candidate was not applied: check your settings.', 'error')
            return redirect(url_for('email_verification'))
        flash('Candidate address applied. It still requires a dedicated deliverability check.', 'success')
        return redirect(url_for('email_verification'))

    @app.post('/email-verification/attempts/<int:attempt_id>/dismiss')
    def dismiss_contact_provider_candidate(attempt_id: int):
        from vcoutreach.web.enrichment.contact_provider_store import dismiss_candidate

        try:
            dismiss_candidate(attempt_id=attempt_id)
        except ValueError as exc:
            flash(f'Candidate was not dismissed: check your settings.', 'error')
        else:
            flash('Candidate address dismissed. The evidence remains in history.', 'success')
        return redirect(url_for('email_verification'))

    # ── Partners list ─────────────────────────────────────────────────────────

    @app.route('/partners')
    def partners_list():
        from vcoutreach.web.pipeline.loader import get_all_partners
        rows=[dict(row) for row in get_all_drafts_with_partners()]
        with_drafts={row['partner_id'] for row in rows}
        for row in get_all_partners():
            if row['id'] not in with_drafts:
                item=dict(row);item.update(partner_id=row['id'],partner_email=row['email'],status='pending',hook='',subject='',draft_id=None)
                rows.append(item)
        status=request.args.get('status','');tier=request.args.get('tier','');query=request.args.get('q','')
        rows=[row for row in rows if (not status or row['status']==status)
            and (not tier or str(row.get('tier') or '')==tier)
            and (not query or query.casefold() in ' '.join(str(row.get(k) or '') for k in ('partner_name','firm','partner_email')).casefold())]
        counts=get_status_counts()
        for name in ('pending','approved','rejected','sent','test_sent','live_sent','drafted','manual_review','dropped','superseded'):
            counts.setdefault(name,0)
        return render_template('partners_list.html',partners=rows,rows=rows,status_filter=status,tier_filter=tier,
            search=query,query=query,counts=counts,active_page='partners')

    # ── Toggle send mode ──────────────────────────────────────────────────────

    @app.route('/toggle-send-mode', methods=['POST'])
    def toggle_send_mode():
        current=send_mode()
        target=request.form.get('mode') or request.form.get('send_mode') or ('live' if current=='test' else 'test')
        if target not in {'test','live'}:
            abort(400)
        if target=='live' and request.form.get('confirm_text','')!='GO LIVE':
            flash('To switch to LIVE mode, type exactly: GO LIVE','error')
        else:
            write_env({'SEND_MODE':target},internal=True)
            flash('Switched to '+target.upper()+' mode.','success')
        return redirect(url_for('workspace_settings'))

    # ── Partner detail ────────────────────────────────────────────────────────

    @app.route('/partners/<int:partner_id>')
    def partner_detail(partner_id: int):
        _partner_row = get_partner(partner_id)
        if _partner_row is None:
            flash(f'Partner {partner_id} not found.', 'error')
            return redirect(url_for('partners_list'))
        # Convert to dict so templates can use .get() safely (handles optional columns)
        partner = dict(_partner_row)

        # Get draft (most recent) - convert to dict so templates can use .get()
        # on optional columns added by migrations (e.g. live_sent_at from 009)
        from vcoutreach.web.pipeline.loader import get_draft_for_partner
        _draft_row = get_draft_for_partner(partner_id)
        draft = dict(_draft_row) if _draft_row is not None else None

        # Assemble email preview (always fresh - reflects current hook + boilerplate)
        assembled = None
        if draft:
            try:
                bp = get_boilerplate()
                assembled = assemble_email(
                    hook=draft['hook'],
                    first_name=partner['first_name'],
                    boilerplate=bp,
                    subject=draft['subject'],
                    recipient_firm=partner.get('firm'),
                )
            except Exception as exc:
                flash(f'Email assembly error: check your settings', 'error')

        if draft and draft['boilerplate_version'].startswith('engine-'):
            from html import escape
            engine_draft=drafts_service.get_engine_draft(partner['slug'])
            assembled={'subject':engine_draft.subject,'body_text':engine_draft.body,
                'body_html':''.join('<p>'+escape(p).replace('\n','<br>')+'</p>' for p in engine_draft.body.split('\n\n')),
                'social_proof_manual_review':False}

        # Load enrichment facts for display
        enrichment_facts = []
        try:
            from vcoutreach.web.enrichment.orchestrator import get_enrichment_facts
            enrichment_facts = get_enrichment_facts(partner_id) or []
        except Exception:
            pass

        return render_template('partner_detail.html',
                               partner=partner,
                               draft=draft,
                               assembled=assembled,
                               enrichment_facts=enrichment_facts,
                               active_page='partners')

    # ── Save hook/subject ─────────────────────────────────────────────────────

    @app.route('/partners/<int:partner_id>/update', methods=['POST'])
    def partner_update(partner_id: int):
        from vcoutreach.web.pipeline.loader import get_draft_for_partner
        draft = get_draft_for_partner(partner_id)
        if draft is None:
            flash('No draft found for this partner.', 'error')
            return redirect(url_for('partner_detail', partner_id=partner_id))

        hook = request.form.get('hook', '').strip()
        subject = request.form.get('subject', '').strip()
        if not hook:
            flash('Hook cannot be empty.', 'error')
            return redirect(url_for('partner_detail', partner_id=partner_id))
        if not subject:
            flash('Subject cannot be empty.', 'error')
            return redirect(url_for('partner_detail', partner_id=partner_id))

        update_draft_hook_subject(draft['id'], hook, subject)
        updated = get_draft_for_partner(partner_id)
        has_issues = bool(updated['auto_approve_skip_reason'])
        flash('Draft saved with issues. Review it in the draft editor.' if has_issues else 'Draft saved.',
              'warning' if has_issues else 'success')
        return redirect(url_for('partner_detail', partner_id=partner_id))

    # ── Approve ───────────────────────────────────────────────────────────────

    @app.route('/partners/<int:partner_id>/approve', methods=['POST'])
    def partner_approve(partner_id: int):
        from vcoutreach.web.pipeline.loader import get_draft_for_partner
        draft = get_draft_for_partner(partner_id)
        if draft is None:
            flash('No draft found for this partner.', 'error')
            return redirect(url_for('partner_detail', partner_id=partner_id))

        if draft['status'] == 'drafted':
            flash('Draft already pushed to Gmail.', 'info')
            return redirect(url_for('partner_detail', partner_id=partner_id))

        if draft['boilerplate_version'].startswith('engine-') and draft['auto_approve_skip_reason']:
            flash('Draft is blocked. Save and check it in the draft editor before approval.','error')
            return redirect(url_for('partner_detail',partner_id=partner_id))
        set_draft_status(draft['id'], 'approved')
        flash('Draft approved. Select it on Partners when you are ready to send.', 'success')
        return redirect(url_for('partner_detail', partner_id=partner_id))

    # ── Reject ────────────────────────────────────────────────────────────────

    @app.route('/partners/<int:partner_id>/reject', methods=['POST'])
    def partner_reject(partner_id: int):
        from vcoutreach.web.pipeline.loader import get_draft_for_partner
        draft = get_draft_for_partner(partner_id)
        if draft is None:
            flash('No draft found for this partner.', 'error')
            return redirect(url_for('partner_detail', partner_id=partner_id))

        review_notes = request.form.get('review_notes', '').strip() or None
        set_draft_status(draft['id'], 'rejected', review_notes=review_notes)
        flash('Draft rejected.', 'warning')
        return redirect(url_for('partner_detail', partner_id=partner_id))

    # ── Push to Gmail ─────────────────────────────────────────────────────────

    @app.route('/push', methods=['POST'])
    def push():
        from vcoutreach.web.pipeline.loader import get_approved_drafts
        from vcoutreach.web.gmail.push_worker import send_with_throttle
        limit=_bounded_int(request.form.get('limit'),daily_send_cap(),minimum=0)
        ids=[row['id'] for row in get_approved_drafts(limit)] if limit else []
        if send_mode()=='live' and (request.form.get('confirm_text') or request.form.get('confirm'))!=f'SEND BATCH OF {len(ids)}':
            flash(f'Type exactly: SEND BATCH OF {len(ids)}','error')
        else:
            _show_send_result(send_with_throttle(ids))
        return redirect(url_for('partners_list'))

    # ── Send-test (one partner, always to test recipient) ─────────────────────

    @app.route('/partners/<int:partner_id>/send-test', methods=['POST'])
    def partner_send_test(partner_id: int):
        from vcoutreach.web.gmail.push_worker import send_one_test,SendRefused
        try:
            send_one_test(partner_id)
            flash('Test email sent to the configured test recipient.','success')
        except SendRefused as exc:
            flash(str(exc),'error')
        except Exception:
            flash('Test sending is unavailable. Check Gmail and workspace settings.','error')
        return redirect(url_for('partner_detail',partner_id=partner_id))

    # ── Send (single partner, respects SEND_MODE) ────────────────────────────

    @app.route('/partners/<int:partner_id>/send', methods=['POST'])
    def partner_send(partner_id: int):
        from vcoutreach.web.gmail.push_worker import send_with_throttle
        from vcoutreach.web.pipeline.loader import get_draft_for_partner
        partner=get_partner(partner_id)
        draft=get_draft_for_partner(partner_id)
        if partner is None or draft is None:
            abort(404)
        if send_mode()=='live' and (request.form.get('confirm_text') or request.form.get('confirm'))!=f'SEND TO {partner["email"]}':
            flash(f'Type exactly: SEND TO {partner["email"]}','error')
        else:
            _show_send_result(send_with_throttle([draft['id']]))
        return redirect(url_for('partner_detail',partner_id=partner_id))

    # ── Bulk send batch ───────────────────────────────────────────────────────

    @app.route('/send-batch', methods=['POST'])
    def send_batch():
        from vcoutreach.web.gmail.push_worker import send_with_throttle
        ids=_selected_draft_ids()
        if not ids:
            flash('No drafts selected for sending.','error')
        elif send_mode()=='live' and (request.form.get('confirm_text') or request.form.get('confirm'))!=f'SEND BATCH OF {len(ids)}':
            flash(f'Type exactly: SEND BATCH OF {len(ids)}','error')
        else:
            _show_send_result(send_with_throttle(ids))
        return redirect(url_for('partners_list'))

    # ── Enrich partner (web search + LinkedIn + email guess) ─────────────────

    @app.route('/partners/<int:partner_id>/enrich', methods=['POST'])
    def partner_enrich(partner_id: int):
        from vcoutreach.web.enrichment.orchestrator import enrich_partner, get_enrichment_facts
        partner = get_partner(partner_id)
        if partner is None:
            flash(f'Partner {partner_id} not found.', 'error')
            return redirect(url_for('partners_list'))

        try:
            result = enrich_partner(partner_id)
            facts = get_enrichment_facts(partner_id)
            errors = result.get('errors', [])
            facts_count = len(facts)
            msg = (
                f'Enrichment complete: {facts_count} usable facts from '
                f'{", ".join(result.get("sources_run", []))}.'
            )
            if errors:
                msg += f' ({len(errors)} provider error(s); check credentials and service status.)'
            flash(msg, 'success' if not errors or facts_count > 0 else 'warning')
        except Exception as exc:
            flash(f'Enrichment failed: check your settings', 'error')

        return redirect(url_for('partner_detail', partner_id=partner_id))

    # ── PitchBook enrich (single partner) ────────────────────────────────────

    @app.route('/partners/<int:partner_id>/pitchbook-enrich', methods=['POST'])
    def partner_pitchbook_enrich(partner_id: int):
        pb_enabled = _env('PITCHBOOK_ENABLED', '0') == '1'
        if not pb_enabled:
            flash(
                'PitchBook enrichment is disabled. Open Settings, find the PitchBook section, start the browser session, then set PITCHBOOK_ENABLED=1 in the workspace .env.',
                'warning',
            )
            return redirect(url_for('partner_detail', partner_id=partner_id))

        partner = get_partner(partner_id)
        if partner is None:
            flash(f'Partner {partner_id} not found.', 'error')
            return redirect(url_for('partners_list'))

        from vcoutreach.web.enrichment.orchestrator import enrich_partner, get_enrichment_facts
        try:
            result = enrich_partner(partner_id, sources=['pitchbook'])
            pb_summary = result.get('per_source', {}).get('pitchbook', {})
            facts = get_enrichment_facts(partner_id)
            pb_facts = [f for f in facts if f.get('source', '').startswith('pitchbook')]
            errors = result.get('errors', [])

            msg = (
                f'PitchBook enrichment complete: {len(pb_facts)} facts, '
                f'{pb_summary.get("deals_count", 0)} deals extracted.'
            )
            if errors:
                msg += f' ({len(errors)} provider error(s); check credentials and service status.)'
            flash(msg, 'success' if not errors or len(pb_facts) > 0 else 'warning')
        except Exception as exc:
            flash(f'PitchBook enrichment failed: check your settings', 'error')

        return redirect(url_for('partner_detail', partner_id=partner_id))

    # ── Replies list ──────────────────────────────────────────────────────────

    @app.route('/replies')
    def replies_list():
        from vcoutreach.web.replies.loader import list_replies
        from vcoutreach.web.replies.classifier import VALID_LABELS
        label=request.args.get('label','')
        rows=[dict(row) for row in list_replies(label=label or None)]
        return render_template('replies_list.html',replies=rows,rows=rows,label_filter=label,labels=sorted(VALID_LABELS),all_labels=sorted(VALID_LABELS),active_page='replies')

    # ── Reply detail ──────────────────────────────────────────────────────────

    @app.route('/replies/<int:reply_id>')
    def reply_detail(reply_id: int):
        from vcoutreach.web.replies.loader import get_reply
        from vcoutreach.web.replies.classifier import VALID_LABELS as ALL_LABELS
        row = get_reply(reply_id)
        if row is None:
            flash(f'Reply {reply_id} not found.', 'error')
            return redirect(url_for('replies_list'))
        return render_template('reply_detail.html',
                               reply=row,
                               all_labels=sorted(ALL_LABELS),
                               active_page='replies')

    @app.route('/replies/<int:reply_id>/update-label', methods=['POST'])
    def reply_update_label(reply_id: int):
        from vcoutreach.web.replies.loader import update_reply_user_label
        user_label = request.form.get('user_label', '').strip()
        if user_label:
            try:
                update_reply_user_label(reply_id, user_label)
            except ValueError as exc:
                flash(str(exc), 'error')
            else:
                flash('Label updated.', 'success')
        return redirect(url_for('reply_detail', reply_id=reply_id))

    @app.route('/replies/<int:reply_id>/mark-reviewed', methods=['POST'])
    def reply_mark_reviewed(reply_id: int):
        from vcoutreach.web.replies.loader import mark_reply_reviewed
        mark_reply_reviewed(reply_id)
        flash('Reply review state updated.', 'success')
        return redirect(url_for('reply_detail', reply_id=reply_id))

    # ── Poll replies (manual trigger) ─────────────────────────────────────────

    @app.route('/poll-replies', methods=['POST'])
    def poll_replies():
        from vcoutreach.web.replies.poller import poll_gmail_for_replies
        limit_str = request.form.get('limit', '')
        limit = int(limit_str) if limit_str.isdigit() else None
        try:
            result = poll_gmail_for_replies(limit=limit)
        except Exception as exc:
            flash(f'Poll failed: check your settings', 'error')
            return redirect(url_for('replies_list'))

        n = result.get('new_replies', 0)
        threads = result.get('threads_scanned', 0)
        errs = result.get('errors', [])
        msg = f'Polled {threads} thread(s), found {n} new reply(s).'
        flash(msg, 'success' if not errs else 'warning')
        for e in errs[:3]:
            flash('A provider operation failed. Check credentials and provider status.', 'error')
        return redirect(url_for('replies_list'))

    # ── Regenerate hook with LLM ──────────────────────────────────────────────

    @app.route('/partners/<int:partner_id>/generate-hook', methods=['POST'])
    def partner_generate_hook(partner_id: int):
        from vcoutreach.web.pipeline.loader import get_draft_for_partner
        from vcoutreach.web.llm.hook_generator import generate_hook, HookGenerationError

        partner = get_partner(partner_id)
        if partner is None:
            flash(f'Partner {partner_id} not found.', 'error')
            return redirect(url_for('partners_list'))

        draft = get_draft_for_partner(partner_id)
        if draft is None:
            flash('No draft found for this partner.', 'error')
            return redirect(url_for('partner_detail', partner_id=partner_id))

        partner_dict = dict(partner)
        # Load any available enrichment facts
        enrichment_facts = None
        try:
            from vcoutreach.web.enrichment.orchestrator import get_enrichment_facts
            enrichment_facts = get_enrichment_facts(partner_id) or None
        except Exception:
            pass  # proceed without facts if enrichment not available

        try:
            result = generate_hook(partner_dict, enrichment_facts=enrichment_facts)
        except HookGenerationError as exc:
            flash(f'LLM hook generation failed: check your settings', 'error')
            return redirect(url_for('partner_detail', partner_id=partner_id))

        update_draft_hook_subject(draft['id'], result['hook'], result['subject'])
        flash(
            f'Hook regenerated via {result["model_used"]} '
            f'({result["attempts"]} attempt(s)). Review and approve when ready.',
            'success',
        )
        return redirect(url_for('partner_detail', partner_id=partner_id))

    # ── Candidates review ─────────────────────────────────────────────────────

    @app.route('/candidates')
    def candidates_list():
        import csv
        import json

        # Paths (relative to project root)
        csv_path = _PROJECT_ROOT / 'data' / 'candidates.csv'
        tracker_path = _PROJECT_ROOT / 'data' / 'imported_candidates.json'

        # Load tracker (which row indices were already imported or skipped)
        try:
            tracker = json.loads(tracker_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            tracker = {'imported': [], 'skipped': []}
        already_reviewed_indices = set(tracker.get('imported', [])) | set(tracker.get('skipped', []))

        # Load existing partners for duplicate detection (read-only)
        from vcoutreach.web.db import get_conn
        from vcoutreach.web.config import DB_PATH
        conn = get_conn(DB_PATH)
        try:
            existing = conn.execute(
                'SELECT partner_name, firm, email FROM partners'
            ).fetchall()
        finally:
            conn.close()

        # Build lookup sets: lower-cased emails and (name+firm) pairs
        existing_emails = {(r['email'] or '').strip().lower() for r in existing if r['email']}
        existing_name_firm = {
            ((r['partner_name'] or '').strip().lower(), (r['firm'] or '').strip().lower())
            for r in existing
        }

        def is_duplicate(row_email, row_name, row_firm):
            """Return True if email OR (name+firm) matches an existing partner."""
            if row_email and row_email.strip().lower() in existing_emails:
                return True
            nf = ((row_name or '').strip().lower(), (row_firm or '').strip().lower())
            if nf[0] and nf in existing_name_firm:
                return True
            return False

        def tier_int(tier_raw):
            """Map CSV tier letters A/B/C to DB integers 1 or 2."""
            t = (tier_raw or '').strip().upper()
            if t == 'A':
                return 1
            return 2  # B, C, or unknown all map to T2

        # Parse CSV; 0-indexed row order (header excluded)
        candidates = []
        if csv_path.exists():
            with open(csv_path, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row_idx, row in enumerate(reader):
                    # Skip already-reviewed rows
                    if row_idx in already_reviewed_indices:
                        continue

                    raw_email = (row.get('email') or '').strip()
                    raw_name = (row.get('partner_name') or '').strip()
                    raw_firm = (row.get('firm') or '').strip()
                    raw_confidence = (row.get('confidence') or '0').strip()
                    try:
                        confidence = float(raw_confidence)
                    except ValueError:
                        confidence = 0.0

                    # CSV has no email_uncertain column - treat as uncertain if email blank
                    email_uncertain = 1 if not raw_email else 0

                    # CSV 'channel' column is actually the source (e.g. pitchbook_coinvestor)
                    source = (row.get('channel') or '').strip()

                    tier_raw = (row.get('tier') or '').strip()

                    candidates.append({
                        'row_index': row_idx,  # 0-indexed data row (post-header)
                        'partner_name': raw_name,
                        'first_name': (row.get('first_name') or raw_name).strip(),
                        'firm': raw_firm or None,
                        'email': raw_email or None,
                        'email_uncertain': email_uncertain,
                        'pitchbook_id': (row.get('pitchbook_id') or '').strip() or None,
                        'tier_raw': tier_raw,
                        'tier': tier_int(tier_raw),
                        'source': source,
                        'why_fit': (row.get('why_fit') or '').strip() or None,
                        'linkedin_url': (row.get('linkedin_url') or '').strip() or None,
                        'confidence': confidence,
                        'already_in_db': is_duplicate(raw_email, raw_name, raw_firm),
                    })

        already_imported_count = len(already_reviewed_indices)
        return render_template('candidates_list.html',
                               candidates=candidates,
                               already_imported_count=already_imported_count,
                               active_page='candidates')

    @app.route('/candidates/import', methods=['POST'])
    def candidates_import():
        import csv
        path=get_workspace().path('data','candidates.csv')
        if not path.is_file():
            flash('No candidates CSV found in workspace data.','warning')
            return redirect(url_for('candidates_list'))
        selected={int(i) for i in request.form.getlist('row_indices') if i.isdigit()}
        if not selected:
            flash('No candidates selected.','warning')
            return redirect(url_for('candidates_list'))
        try:
            with path.open(encoding='utf-8-sig',newline='') as handle:
                rows=list(csv.DictReader(handle))
            contacts=[]
            for index,row in enumerate(rows):
                if index not in selected:
                    continue
                values={**row,'name':row.get('name') or row.get('partner_name',''),'org':row.get('org') or row.get('firm','')}
                contacts.append(drafts_service.form_contact(values))
            count=drafts_service.import_contacts(contacts,batch_name='Candidates import')
        except (ValueError,Exception) as exc:
            from vcoutreach.types import OutreachError
            if isinstance(exc,(ValueError,OutreachError)):
                for line in str(exc).splitlines()[:25]:
                    flash(line,'error')
            else:
                flash('Candidate import failed. Check the CSV fields.','error')
        else:
            tracker=get_workspace().read_json('data/imported_candidates.json',{'imported':[],'skipped':[]})
            tracker['imported']=sorted(set(tracker.get('imported',[]))|selected)
            get_workspace().path('data','imported_candidates.json').write_text(json.dumps(tracker),encoding='utf-8')
            flash(f'Imported {count} candidates.','success')
        return redirect(url_for('partners_list'))

    # ── Enrichment status dashboard ───────────────────────────────────────────

    @app.route('/enrichment')
    def enrichment_status():
        import json
        from vcoutreach.web.db import get_conn

        SOURCES = ['pitchbook_api', 'pitchbook', 'linkedin', 'web_search',
                   'web_search_v2', 'firm_website', 'email_guess']

        conn = get_conn()
        try:
            # All active (non-dropped) partners
            partners = conn.execute(
                """
                SELECT p.id, p.partner_name, p.firm, p.tier, p.verification_status
                FROM partners p
                WHERE p.dropped_reason IS NULL
                ORDER BY p.id
                """
            ).fetchall()

            # All enrichment rows (grouped by partner + source) - one pass
            enrich_rows = conn.execute(
                """
                SELECT partner_id, source, facts_json, fetched_at
                FROM enrichment
                ORDER BY partner_id, source
                """
            ).fetchall()

            # All drafts
            draft_rows = conn.execute(
                """
                SELECT partner_id, hook, updated_at
                FROM drafts
                ORDER BY partner_id
                """
            ).fetchall()

            # All pitchbook_api + pitchbook facts for hook-cite heuristic
            pb_fact_rows = conn.execute(
                """
                SELECT partner_id, facts_json
                FROM enrichment
                WHERE source IN ('pitchbook_api', 'pitchbook')
                  AND facts_json IS NOT NULL
                """
            ).fetchall()

        finally:
            conn.close()

        # Build enrichment index: {partner_id: {source: {'count': N, 'fetched_at': str}}}
        enrich_index: dict[int, dict] = {}
        for row in enrich_rows:
            pid = row['partner_id']
            src = row['source']
            fj = row['facts_json']
            fetched = row['fetched_at'] or ''

            if pid not in enrich_index:
                enrich_index[pid] = {}

            # Accumulate across multiple rows per source (web_search has many)
            existing = enrich_index[pid].get(src, {'count': 0, 'fetched_at': ''})

            fact_count = 0
            if fj:
                try:
                    facts = json.loads(fj)
                    if isinstance(facts, list):
                        fact_count = len(facts)
                except Exception:
                    fact_count = 1  # non-empty but unparseable = present

            new_count = existing['count'] + fact_count
            new_fetched = max(existing['fetched_at'], fetched) if existing['fetched_at'] else fetched
            enrich_index[pid][src] = {'count': new_count, 'fetched_at': new_fetched}

        # Build draft index: {partner_id: {'hook': str, 'updated_at': str}}
        draft_index: dict[int, dict] = {}
        for row in draft_rows:
            pid = row['partner_id']
            draft_index[pid] = {'hook': row['hook'] or '', 'updated_at': row['updated_at'] or ''}

        # Build PitchBook company-name index for cite heuristic
        # {partner_id: set_of_company_names}
        pb_companies: dict[int, set] = {}
        for row in pb_fact_rows:
            pid = row['partner_id']
            fj = row['facts_json']
            if not fj:
                continue
            try:
                facts = json.loads(fj)
                if not isinstance(facts, list):
                    continue
            except Exception:
                continue
            for fact in facts:
                ftype = fact.get('type', '')
                if ftype in ('pitchbook_recent_deal', 'pitchbook_board_seat'):
                    # Extract company name from text like "Company X (2022) - Series B"
                    text = fact.get('text', '') or fact.get('company', '') or ''
                    # Take first parentheses-bounded token or first word-cluster before dash
                    import re as _re
                    m = _re.match(r'^([^(]+?)(?:\s*\(|\s*-|$)', text.strip())
                    if m:
                        company = m.group(1).strip()
                        if company and len(company) > 2:
                            pb_companies.setdefault(pid, set()).add(company.lower())

        # Build rows for template
        import re as _re
        from datetime import datetime, timezone

        def _relative_time(ts_str: str) -> str:
            """Convert ISO timestamp to relative string."""
            if not ts_str:
                return '-'
            try:
                # Handle both naive and aware timestamps
                ts_str_clean = ts_str.replace('Z', '+00:00')
                if '+' in ts_str_clean[10:] or ts_str_clean.endswith('Z'):
                    dt = datetime.fromisoformat(ts_str_clean)
                    now = datetime.now(timezone.utc)
                else:
                    dt = datetime.fromisoformat(ts_str_clean)
                    now = datetime.now()
                diff = now - dt
                secs = int(diff.total_seconds())
                if secs < 0:
                    return 'just now'
                if secs < 3600:
                    return f'{secs // 60}m ago'
                if secs < 86400:
                    return f'{secs // 3600}h ago'
                return f'{secs // 86400}d ago'
            except Exception:
                return ts_str[:10] if len(ts_str) >= 10 else ts_str

        rows_out = []
        for p in partners:
            pid = p['id']
            src_map = enrich_index.get(pid, {})

            # Per-source presence
            source_data = {}
            for src in SOURCES:
                sd = src_map.get(src, {})
                count = sd.get('count', 0)
                source_data[src] = {'present': count > 0, 'count': count}

            total_facts = sum(sd['count'] for sd in source_data.values())

            # Last enriched timestamp
            fetched_times = [src_map[s]['fetched_at'] for s in src_map if src_map[s].get('fetched_at')]
            last_enriched_raw = max(fetched_times) if fetched_times else ''
            last_enriched = _relative_time(last_enriched_raw)

            # Hook data
            draft = draft_index.get(pid, {})
            hook_text = draft.get('hook', '')
            hook_updated = draft.get('updated_at', '')
            has_hook = bool(hook_text and hook_text.strip())

            # hook_cites_specific: does hook mention a company from PB facts?
            if not has_hook:
                hook_quality = 'no_hook'
            elif not fetched_times:
                hook_quality = 'no_enrichment'
            else:
                companies = pb_companies.get(pid, set())
                if companies:
                    hook_lower = hook_text.lower()
                    cited = any(c in hook_lower for c in companies if len(c) > 3)
                    hook_quality = 'cite_specific' if cited else 'generic'
                else:
                    hook_quality = 'unknown'

            rows_out.append({
                'partner_id': pid,
                'partner_name': p['partner_name'],
                'firm': p['firm'] or '-',
                'tier': p['tier'] or 0,
                'verification_status': p['verification_status'] or 'unverified',
                'source_data': source_data,
                'total_facts': total_facts,
                'has_hook': has_hook,
                'hook_quality': hook_quality,
                'last_enriched': last_enriched,
                'last_enriched_raw': last_enriched_raw,
                'hook_updated': hook_updated[:16].replace('T', ' ') if hook_updated else '-',
            })

        # Sort: weakest first (lowest total_facts)
        rows_out.sort(key=lambda r: r['total_facts'])

        # Quick stats
        total_partners = len(rows_out)
        fully_enriched = sum(
            1 for r in rows_out
            if sum(1 for s in SOURCES if r['source_data'][s]['present']) >= 4
            and r['hook_quality'] == 'cite_specific'
        )
        needs_pb = sum(
            1 for r in rows_out
            if not r['source_data']['pitchbook_api']['present']
            and not r['source_data']['pitchbook']['present']
        )
        needs_linkedin = sum(
            1 for r in rows_out
            if not r['source_data']['linkedin']['present']
        )
        needs_hook_regen = sum(
            1 for r in rows_out
            if r['hook_quality'] in ('generic', 'no_hook', 'unknown')
        )

        stats = {
            'total': total_partners,
            'fully_enriched': fully_enriched,
            'needs_pb': needs_pb,
            'needs_linkedin': needs_linkedin,
            'needs_hook_regen': needs_hook_regen,
        }

        return render_template(
            'enrichment_status.html',
            rows=rows_out,
            sources=SOURCES,
            stats=stats,
            active_page='enrichment',
        )

    # ── Scheduled sends ───────────────────────────────────────────────────────

    @app.route('/schedule-send', methods=['POST'])
    def schedule_send():
        from vcoutreach.web.db import cursor_ctx
        ids=_selected_draft_ids()
        mode=request.form.get('sched_mode',send_mode())
        if not ids:
            flash('No drafts selected for scheduling.','error')
            return redirect(url_for('partners_list'))
        if mode not in {'test','live'}:
            abort(400)
        if mode=='live' and (send_mode()!='live' or request.form.get('confirm_text','')!=f'SCHEDULE LIVE FOR {len(ids)}'):
            flash(f'Switch to LIVE and type exactly: SCHEDULE LIVE FOR {len(ids)}','error')
            return redirect(url_for('partners_list'))
        if mode=='test' and not _env('TEST_RECIPIENT_EMAIL'):
            flash('Set TEST_RECIPIENT_EMAIL before scheduling in TEST mode.','error')
            return redirect(url_for('partners_list'))
        from vcoutreach.web.gmail.push_worker import require_gmail_connection, SendRefused
        try:
            from vcoutreach.contacts import _EMAIL_RE
            if mode == 'test' and not _EMAIL_RE.fullmatch(_env('TEST_RECIPIENT_EMAIL')):
                raise SendRefused('TEST_RECIPIENT_EMAIL must be a single valid email address.')
            require_gmail_connection()
        except SendRefused as exc:
            flash(str(exc), 'error')
            return redirect(url_for('partners_list'))
        try:
            raw=request.form.get('scheduled_for') or (request.form.get('sched_date','')+' '+request.form.get('sched_time',''))
            instant=datetime.fromisoformat(raw)
            if instant.tzinfo is None:
                instant=instant.astimezone()
            instant=instant.astimezone(timezone.utc)
            if instant<datetime.now(timezone.utc)+timedelta(seconds=60):
                raise ValueError()
        except ValueError:
            flash('Choose a send time at least 60 seconds in the future.','error')
            return redirect(url_for('partners_list'))
        with cursor_ctx() as (_,cur):
            cur.execute("INSERT INTO scheduled_sends(draft_ids,mode,scheduled_for,estimated_completion,status,created_by,confirmation) VALUES (?,?,?,?,'pending','web',?)",
                (json.dumps(ids),mode,instant.isoformat(),(instant+timedelta(seconds=len(ids)*32)).isoformat(),request.form.get('confirm_text','')))
        flash('Send scheduled. The scheduler runs only with OUTREACH_SCHEDULER=1.','success')
        return redirect(url_for('scheduled_list'))

    @app.route('/scheduled')
    def scheduled_list():
        import json as _json
        from vcoutreach.web.db import get_conn as _get_conn
        from datetime import datetime as _dt

        conn = _get_conn()
        try:
            rows = conn.execute(
                """
                SELECT id, draft_ids, mode, scheduled_for, estimated_completion,
                       status, created_at, started_at, completed_at, result_json
                FROM scheduled_sends
                ORDER BY scheduled_for DESC
                LIMIT 100
                """
            ).fetchall()
        finally:
            conn.close()

        schedule_rows = []
        for r in rows:
            try:
                ids = _json.loads(r['draft_ids'])
                count = len(ids)
            except Exception:
                count = 0

            try:
                result = _json.loads(r['result_json']) if r['result_json'] else None
            except Exception:
                result = None

            schedule_rows.append({
                'id': r['id'],
                'count': count,
                'mode': r['mode'],
                'scheduled_for': r['scheduled_for'],
                'estimated_completion': r['estimated_completion'],
                'status': r['status'],
                'created_at': r['created_at'],
                'started_at': r['started_at'],
                'completed_at': r['completed_at'],
                'result': result,
            })

        return render_template('scheduled_list.html',
                               schedule_rows=schedule_rows,
                               active_page='scheduled')

    @app.route('/scheduled/<int:sched_id>/cancel', methods=['POST'])
    def scheduled_cancel(sched_id: int):
        from vcoutreach.web.db import get_conn as _get_conn

        conn = _get_conn()
        try:
            updated = conn.execute(
                """
                UPDATE scheduled_sends
                SET status = 'cancelled'
                WHERE id = ? AND status = 'pending'
                """,
                (sched_id,),
            ).rowcount
            conn.commit()
        finally:
            conn.close()

        if updated:
            flash(f'Scheduled send #{sched_id} cancelled.', 'success')
        else:
            flash(f'Cannot cancel #{sched_id} - it is no longer pending.', 'warning')

        return redirect(url_for('scheduled_list'))

    # ── Start APScheduler after app is ready ──────────────────────────────────
    from vcoutreach.web.gmail.scheduler import start_scheduler
    if workspace.value('OUTREACH_SCHEDULER') == '1':
        start_scheduler(app)

    return app
