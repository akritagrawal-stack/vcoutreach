"""Synchronous, journaled sends with workspace-scoped transport injection."""
from __future__ import annotations
import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from vcoutreach.journal import Journal
from vcoutreach.send.gate import _exclusive_send_lock
from vcoutreach.web.config import get_workspace, daily_send_cap, send_mode
from vcoutreach.web.db import get_conn, cursor_ctx

class SendRefused(ValueError):
    pass

GMAIL_NOT_CONNECTED = 'Gmail is not connected. Open Settings and choose Connect Gmail, then try again.'

def _injected_transport():
    from flask import current_app, has_app_context
    return current_app.extensions.get('send_transport') if has_app_context() else None

def gmail_connection_reason():
    """Check local authorization for a read-only plan without refreshing tokens."""
    if _injected_transport() is not None:
        return ''
    from vcoutreach.web.gmail.app_password import configured, load_credentials
    if configured():
        try:
            load_credentials()
        except Exception:
            return GMAIL_NOT_CONNECTED
        return ''
    import json
    try:
        token = json.loads(get_workspace().path('data', '.gmail_token.json').read_text(encoding='utf-8'))
        if not isinstance(token, dict) or not token.get('refresh_token') or not token.get('client_id') or not token.get('client_secret'):
            return GMAIL_NOT_CONNECTED
    except (OSError, ValueError):
        return GMAIL_NOT_CONNECTED
    return ''

def require_gmail_connection():
    """Prepare authorization before reserving an attempt or scheduling a send."""
    if _injected_transport() is not None:
        return None
    from vcoutreach.web.gmail.api import get_service
    try:
        return get_service()
    except Exception:
        raise SendRefused(GMAIL_NOT_CONNECTED) from None

def allow_retry_after_sent_check(partner_id, attempt_id):
    """Retain uncertain history while recording an explicit manual Sent check."""
    workspace = get_workspace()
    journal = Journal(workspace.path('.vcoutreach', 'journal.jsonl'))
    workspace.path('.vcoutreach', 'send.lock')
    with _exclusive_send_lock(journal):
        with cursor_ctx() as (conn, cur):
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM web_send_journal WHERE id=? AND partner_id=? AND state='uncertain'",
                               (attempt_id, partner_id)).fetchone()
            if row is None:
                raise SendRefused('No uncertain attempt found. Nothing was changed.')
            if row['message_id'] or journal.has(Journal.key(row['recipient'], row['lane']), row['mode']):
                raise SendRefused('A successful send receipt exists. Duplicate send refused.')
            cur.execute("UPDATE web_send_journal SET state='retryable',completed_at=CURRENT_TIMESTAMP WHERE id=? AND state='uncertain'", (attempt_id,))
            cur.execute('INSERT INTO crm_activities(partner_id,activity_type,subject,snippet) VALUES (?,?,?,?)',
                        (partner_id, 'note', 'Uncertain send marked retryable',
                         f"I checked Sent, it did not go out, allow retry. Attempt {attempt_id}, {row['mode']} {row['lane']}, target {row['target_email']}."))

def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f'{seconds} sec'
    minutes, seconds = divmod(seconds,60)
    return f'{minutes} min' + (f' {seconds} sec' if seconds else '')

def estimate_send_duration(num_drafts: int) -> dict:
    return {'min_seconds':num_drafts*2,'max_seconds':num_drafts*32,'avg_seconds':num_drafts*17,
            'eta_min_str':format_duration(num_drafts*2),'eta_max_str':format_duration(num_drafts*32),
            'eta_avg_str':format_duration(num_drafts*17)}

def count_drafts_today() -> int:
    conn=get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM web_send_journal WHERE date(created_at)=date('now') AND state IN ('sending','sent','uncertain')").fetchone()[0]
    finally:
        conn.close()

def _transport_send(message, service=None):
    transport=_injected_transport()
    if transport is not None:
        receipt=transport.send(message) if hasattr(transport,'send') else transport(message)
        if isinstance(receipt,dict):
            return str(receipt.get('message_id') or receipt.get('id') or '')
        return str(getattr(receipt,'message_id',receipt) or '')
    from vcoutreach.web.gmail.api import get_service
    service = service if service is not None else get_service()
    result=service.users().messages().send(userId='me',body={'raw':base64.urlsafe_b64encode(message.as_bytes()).decode('ascii')}).execute()
    return str(result.get('id') or '')

def _send(draft_id, mode):
    from vcoutreach.contacts import _EMAIL_RE
    from vcoutreach.lint import lint_draft
    from vcoutreach.sender import load_sender
    from vcoutreach.web.boilerplate_loader import get_boilerplate
    from vcoutreach.web.email_assembler import assemble_email
    from vcoutreach.web.pipeline.loader import mark_test_sent,mark_live_sent
    from vcoutreach.web.drafts_service import get_engine_draft
    workspace=get_workspace()
    if mode not in {'test','live'}:
        raise SendRefused('Unknown send mode.')
    if mode=='live' and send_mode()!='live':
        raise SendRefused('Switch to LIVE mode before sending.')
    test_to=workspace.value('TEST_RECIPIENT_EMAIL')
    if mode=='test' and not test_to:
        raise SendRefused('Set TEST_RECIPIENT_EMAIL before sending in TEST mode.')
    if mode=='test' and not _EMAIL_RE.fullmatch(test_to):
        raise SendRefused('TEST_RECIPIENT_EMAIL must be a single valid email address.')
    journal=Journal(workspace.path('.vcoutreach','journal.jsonl'))
    workspace.path('.vcoutreach','send.lock')
    workspace.path('.vcoutreach','journal.jsonl.lock')
    with _exclusive_send_lock(journal):
        conn=get_conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('''SELECT d.*,p.partner_name,p.first_name,p.email,p.firm,p.slug,p.dropped_reason,p.channel,
                p.email_verification_status,cp.stage,
                (SELECT COALESCE(NULLIF(r.user_label,''),r.label) FROM replies r WHERE r.partner_id=p.id ORDER BY r.received_at DESC,r.id DESC LIMIT 1) AS latest_reply_label FROM drafts d JOIN partners p ON p.id=d.partner_id
                LEFT JOIN crm_profiles cp ON cp.partner_id=p.id WHERE d.id=?''',(draft_id,)).fetchone()
            if row is None:
                raise SendRefused('Draft not found.')
            row=dict(row)
            if row['status']=='blocked' or row['boilerplate_version'].startswith('engine-') and row['auto_approve_skip_reason']:
                raise SendRefused('Draft is blocked. Save and check it before sending.')
            from vcoutreach.web.llm.hook_grounding import hook_is_ungrounded, grounding_state
            if hook_is_ungrounded(row):
                raise SendRefused('This hook is not backed by the saved research ('+grounding_state(row)+'). Regenerate the hook or edit it before sending.')
            if row['stage'] in {'passed','hold'} or (not row['stage'] and row['latest_reply_label'] in {'pass','passed','bounce'}) or row['dropped_reason']:
                raise SendRefused('Contact is passed, on hold, or suppressed. Nothing was sent.')
            if row['channel']!='email' or not _EMAIL_RE.fullmatch(row['email'] or ''):
                raise SendRefused('Contact needs a valid email address.')
            if mode=='live':
                if row['status']!='approved':
                    raise SendRefused('Approve this draft before a LIVE send.')
                if row['email_verification_status'] not in {'valid','deliverable','pb_verified'}:
                    raise SendRefused('Verify the contact email before a LIVE send.')
                if row['live_sent_at']:
                    raise SendRefused('Already sent live. Duplicate send refused.')
            sender=load_sender(workspace.path('sender.json'))
            lane='follow_up' if row['is_reply'] else 'first_touch'
            body=row['hook']; subject=row['subject']
            in_reply_to=row['in_reply_to_message_id'] or ''
            refs=in_reply_to
            if row['boilerplate_version'].startswith('engine-'):
                draft=get_engine_draft(row['slug'])
                body,subject,lane,in_reply_to,refs=draft.body,draft.subject,draft.lane.value,draft.in_reply_to,draft.references
                if draft.status!='ok':
                    raise SendRefused('Draft is blocked. Save and check it before sending.')
            elif not row['boilerplate_version'].startswith('prepared-'):
                assembled=assemble_email(row['hook'],row['first_name'],get_boilerplate(),row['subject'],row['firm'])
                body,subject=assembled['body_text'],assembled['subject']
            if any(i.severity=='error' for i in lint_draft(subject,body,sender,None)):
                raise SendRefused('Draft failed send-time lint. Review it before sending.')
            if lane=='follow_up' and not in_reply_to:
                raise SendRefused('Follow-up needs an In-Reply-To message id.')
            key=Journal.key(row['email'],lane)
            if journal.has(key,mode) or conn.execute("SELECT 1 FROM web_send_journal WHERE recipient=? AND lane=? AND mode=? AND state IN ('sending','sent','uncertain')",(row['email'].lower(),lane,mode)).fetchone():
                raise SendRefused('Already attempted or sent. Duplicate send refused.')
            cap=daily_send_cap()
            used=conn.execute("SELECT COUNT(*) FROM web_send_journal WHERE date(created_at)=date('now') AND state IN ('sending','sent','uncertain')").fetchone()[0]
            used=max(used,journal.count_today('live')+journal.count_today('test'))
            if used>=cap:
                raise SendRefused('Daily send cap reached. Nothing was sent.')
            target=test_to if mode=='test' else row['email']
            if mode=='test' and target.casefold()==row['email'].casefold():
                raise SendRefused('Test recipient must differ from the contact email.')
            from_email=workspace.value('SENDER_EMAIL') or (test_to if mode=='test' else '')
            if not _EMAIL_RE.fullmatch(from_email):
                raise SendRefused('Set SENDER_EMAIL to a valid address before sending.')
            message=EmailMessage()
            message['From']=from_email; message['To']=target
            message['Subject']=f'[TEST to {row["email"]}] {subject}' if mode=='test' else subject
            from email.utils import formatdate,make_msgid
            message['Date']=formatdate(localtime=False);message['Message-ID']=make_msgid(domain='outreach-signal.local')
            if in_reply_to:
                message['In-Reply-To']=in_reply_to;message['References']=refs or in_reply_to
            message.set_content(body)
            message.add_alternative(''.join('<p>'+escape(p).replace('\n','<br>')+'</p>' for p in body.split('\n\n')),subtype='html')
            from vcoutreach.web.server.workspace_settings import load_settings
            deck=load_settings().get('pitch_deck_path') or workspace.value('PITCH_DECK_PATH')
            if deck:
                path=workspace.path(deck)
                if not path.is_file():
                    raise SendRefused('Configured pitch deck is missing.')
                message.add_attachment(path.read_bytes(),maintype='application',subtype='pdf',filename=path.name)
            service=require_gmail_connection()
            cursor=conn.execute("INSERT INTO web_send_journal(draft_id,partner_id,recipient,lane,mode,state,target_email) VALUES (?,?,?,?,?,'sending',?)",
                (draft_id,row['partner_id'],row['email'].lower(),lane,mode,target))
            attempt_id=cursor.lastrowid
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally:
            conn.close()
        try:
            message_id=_transport_send(message, service)
            if not message_id:
                raise SendRefused('Transport returned no delivery receipt.')
            journal.record(key,mode,contact_id=row['slug'] or str(row['partner_id']),lane=lane,recipient=row['email'],routed_to=target,message_id=message_id)
            with cursor_ctx() as (_,cur):
                cur.execute("UPDATE web_send_journal SET state='sent',completed_at=CURRENT_TIMESTAMP,message_id=? WHERE id=?",(message_id,attempt_id))
            if mode=='test':
                mark_test_sent(draft_id,message_id,target)
            else:
                mark_live_sent(draft_id,message_id,target,subject)
                from vcoutreach.web.crm_store import add_activity
                add_activity(row['partner_id'], 'note', 'email_sent_by_app', subject)
                with cursor_ctx() as (conn,cur):
                    batch = conn.execute('SELECT b.name FROM batches b JOIN contact_batches cb ON cb.batch_id=b.id WHERE cb.contact_id=? ORDER BY b.id DESC LIMIT 1', (row['partner_id'],)).fetchone()
                    cur.execute("INSERT OR IGNORE INTO crm_send_evidence(partner_id,batch_key,evidence_status,observed_at,source_path) VALUES (?,?, 'sent_confirmed',CURRENT_TIMESTAMP,'gmail_transport')",
                        (row['partner_id'],batch['name'] if batch else 'delivery-'+lane+'-'+datetime.now(timezone.utc).date().isoformat()))
            return message_id
        except Exception:
            with cursor_ctx() as (_,cur):
                cur.execute("UPDATE web_send_journal SET state='uncertain' WHERE id=? AND state='sending'",(attempt_id,))
            raise SendRefused('Send outcome is uncertain. Check Sent before any retry.') from None

def send_one_test(partner_id: int) -> str:
    from vcoutreach.web.pipeline.loader import get_draft_for_partner
    draft=get_draft_for_partner(partner_id)
    if draft is None:
        raise SendRefused('No draft found for this contact.')
    return _send(draft['id'],'test')

def send_one_real(partner_id: int, force: bool = False) -> str:
    from vcoutreach.web.pipeline.loader import get_draft_for_partner
    draft=get_draft_for_partner(partner_id)
    if draft is None:
        raise SendRefused('No draft found for this contact.')
    return _send(draft['id'],'live')

def send_with_throttle(draft_ids: list[int], mode: str | None = None) -> dict:
    mode=mode or send_mode()
    result={'sent':0,'failed':0,'skipped':0,'skipped_by_cap':0,'total_seconds':0,'errors':[],'mode':mode}
    for draft_id in dict.fromkeys(draft_ids):
        try:
            _send(draft_id,mode)
            result['sent']+=1
        except SendRefused as exc:
            result['failed']+=1;result['errors'].append(str(exc))
        except Exception:
            result['failed']+=1;result['errors'].append('Sending is unavailable. Check Gmail and workspace settings.')
    result['cap_remaining']=max(0,daily_send_cap()-count_drafts_today())
    return result

def send_with_throttle_safe(draft_ids: list[int], mode: str | None = None) -> dict:
    return send_with_throttle(draft_ids,mode)

def push_approved(limit: int | None = None) -> dict:
    from vcoutreach.web.pipeline.loader import get_approved_drafts
    return send_with_throttle([row['id'] for row in get_approved_drafts(limit)])
