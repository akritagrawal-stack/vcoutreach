"""Opt-in scheduler. No thread or external call is started on module import."""
from __future__ import annotations
import json
import threading
from datetime import datetime, timezone
from vcoutreach.web.config import get_workspace
from vcoutreach.web.db import get_conn,cursor_ctx

_schedulers=[]

def get_db_conn():
    return get_conn()

def _poll_scheduled_sends():
    if get_workspace().value('OUTREACH_SCHEDULER')!='1':
        return
    conn=get_conn()
    try:
        rows=conn.execute("SELECT * FROM scheduled_sends WHERE status='pending' ORDER BY scheduled_for,id").fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            instant=datetime.fromisoformat(row['scheduled_for'].replace('Z','+00:00'))
            if instant.tzinfo is None:
                instant=instant.replace(tzinfo=timezone.utc)
            if instant>datetime.now(timezone.utc):
                continue
            ids=json.loads(row['draft_ids'])
            if not isinstance(ids,list) or not all(type(item) is int for item in ids):
                raise ValueError()
            if row['mode']=='live' and row['confirmation']!=f'SCHEDULE LIVE FOR {len(ids)}':
                raise ValueError()
        except (ValueError,TypeError):
            with cursor_ctx() as (_,cur):
                cur.execute("UPDATE scheduled_sends SET status='failed',result_json=? WHERE id=?",(json.dumps({'errors':['Invalid schedule confirmation or date.']}),row['id']))
            continue
        _execute_scheduled_send(row['id'],ids,row['mode'])

def _execute_scheduled_send(row_id: int, draft_ids: list, mode: str):
    """Require opt-in even for direct calls, before claiming or journaling a send."""
    if get_workspace().value('OUTREACH_SCHEDULER')!='1':
        return
    with cursor_ctx() as (_,cur):
        cur.execute("UPDATE scheduled_sends SET status='running',started_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(row_id,))
        claimed=cur.rowcount==1
    if not claimed:
        return
    from vcoutreach.web.gmail.push_worker import send_with_throttle_safe
    result=send_with_throttle_safe(draft_ids,mode=mode)
    with cursor_ctx() as (_,cur):
        cur.execute("UPDATE scheduled_sends SET status=?,completed_at=CURRENT_TIMESTAMP,result_json=? WHERE id=?",
            ('completed' if not result['failed'] else 'failed',json.dumps(result),row_id))

def _poll_replies_job(limit: int = 20):
    if get_workspace().value('OUTREACH_SCHEDULER')!='1':
        return
    from vcoutreach.web.replies.poller import poll_gmail_for_replies
    return poll_gmail_for_replies(limit=limit)

def _auto_enrich_job(batch_size: int = 5):
    if get_workspace().value('OUTREACH_SCHEDULER')!='1':
        return
    from vcoutreach.web.enrichment.orchestrator import enrich_partner
    conn=get_conn()
    try:
        rows=conn.execute('SELECT id FROM partners WHERE needs_enrichment=1 AND dropped_reason IS NULL LIMIT ?', (batch_size,)).fetchall()
    finally:
        conn.close()
    for row in rows:
        enrich_partner(row['id'])

def start_scheduler(app=None):
    workspace=app.extensions['workspace'] if app else get_workspace()
    if workspace.value('OUTREACH_SCHEDULER')!='1' or app is None:
        return None
    if 'outreach_scheduler' in app.extensions:
        return app.extensions['outreach_scheduler']
    stop=threading.Event()
    def loop():
        ticks=0
        while not stop.wait(60):
            with app.app_context():
                try:
                    _poll_scheduled_sends()
                    if ticks%10==0:
                        _poll_replies_job()
                    if ticks%240==0:
                        _auto_enrich_job()
                except Exception:
                    app.logger.warning('Scheduled work unavailable. Check workspace credentials.')
            ticks+=1
    thread=threading.Thread(target=loop,name='outreach-scheduler',daemon=True)
    thread.start()
    app.extensions['outreach_scheduler']=(stop,thread)
    _schedulers.append((stop,thread))
    return stop,thread

def stop_scheduler():
    for stop,thread in _schedulers:
        stop.set()
        thread.join(timeout=1)
    _schedulers.clear()

def count_pending_scheduled() -> int:
    conn=get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM scheduled_sends WHERE status='pending'").fetchone()[0]
    finally:
        conn.close()
