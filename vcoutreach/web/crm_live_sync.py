"""Explicit, bounded Gmail metadata refresh using this workspace's credentials."""
from __future__ import annotations
from datetime import date,datetime,timedelta,timezone
from email.utils import getaddresses
from pathlib import Path
from vcoutreach.web.db import get_conn,cursor_ctx
SOURCE='gmail_readonly'

def sync_status(db_path: Path) -> dict | None:
    conn=get_conn(db_path)
    try:
        row=conn.execute('SELECT checked_at,scanned_messages,matched_messages,scan_complete FROM crm_sync_state WHERE source=?',(SOURCE,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def refresh_inbox(db_path: Path, *, days: int = 30, cap: int = 300) -> dict:
    if not 1<=days<=90 or not 1<=cap<=1000:
        raise ValueError('Invalid inbox scan bounds.')
    from vcoutreach.web.gmail.api import get_service
    service=get_service()
    conn=get_conn(db_path)
    try:
        contacts={row['email'].lower():row['id'] for row in conn.execute('SELECT id,email FROM partners')}
    finally:
        conn.close()
    query=f'after:{(date.today()-timedelta(days=days)).isoformat()} before:{(date.today()+timedelta(days=1)).isoformat()}'
    checked=matched=inserted=0;page=None;complete=False
    while checked<cap:
        args={'userId':'me','q':query,'maxResults':min(100,cap-checked)}
        if page:
            args['pageToken']=page
        response=service.users().messages().list(**args).execute()
        for item in response.get('messages',[]):
            if checked>=cap:
                break
            message=service.users().messages().get(userId='me',id=item['id'],format='metadata',metadataHeaders=['From']).execute()
            checked+=1
            headers=message.get('payload',{}).get('headers',[])
            values=[h.get('value','') for h in headers if h.get('name','').lower()=='from']
            addresses=getaddresses(values)
            if len(addresses)!=1:
                continue
            address=addresses[0][1].lower();partner_id=contacts.get(address)
            if partner_id is None:
                continue
            matched+=1
            observed=datetime.fromtimestamp(int(message.get('internalDate','0'))/1000,timezone.utc).isoformat()
            with cursor_ctx(db_path) as (_,cur):
                cur.execute('INSERT OR IGNORE INTO crm_mail_evidence(message_id,partner_id,observed_at,thread_id,source_mailbox) VALUES (?,?,?,?,?)',
                    (item['id'],partner_id,observed,message.get('threadId'),SOURCE))
                inserted+=cur.rowcount
        page=response.get('nextPageToken')
        if not page:
            complete=True;break
    now=datetime.now(timezone.utc).isoformat()
    with cursor_ctx(db_path) as (_,cur):
        cur.execute('''INSERT INTO crm_sync_state(source,checked_at,scanned_messages,matched_messages,scan_complete) VALUES (?,?,?,?,?)
            ON CONFLICT(source) DO UPDATE SET checked_at=excluded.checked_at,scanned_messages=excluded.scanned_messages,
            matched_messages=excluded.matched_messages,scan_complete=excluded.scan_complete''',(SOURCE,now,checked,matched,int(complete)))
    return dict(checked_at=now,scanned_messages=checked,matched_messages=matched,inserted=inserted,scan_complete=complete)
