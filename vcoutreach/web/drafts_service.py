"""Engine-backed contacts, draft files and read-only send plans."""
from __future__ import annotations
import csv
import io
import json
import re
import secrets
from dataclasses import asdict, replace
from pathlib import Path
from vcoutreach.contacts import load_contacts
from vcoutreach.drafts_io import read_draft, write_draft, write_index
from vcoutreach.lint import lint_draft
from vcoutreach.sender import load_sender
from vcoutreach.types import Contact, Draft, Lane, ContactsError, OutreachError
from vcoutreach.web.config import get_workspace, daily_send_cap, send_mode
from vcoutreach.web.db import get_conn, cursor_ctx

COLUMNS = ('id','name','email','org','role','facts','source_urls','lane','thread_subject','in_reply_to','references')
SENDER_FIELDS = ('name','title','org','website','signature','product_lines','traction_lines','ask_line','approved_claims','forbidden_phrases','first_touch_subject')
SLUG = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

def valid_slug(slug):
    if not isinstance(slug, str) or not SLUG.fullmatch(slug) or len(slug) > 240:
        raise ValueError('Invalid contact id.')
    return slug

def parse_csv(text, *, label='your CSV'):
    workspace = get_workspace()
    path = workspace.path('data', 'import-' + secrets.token_hex(8) + '.csv')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(text, encoding='utf-8')
        parsed = load_contacts(path)
        if not parsed:
            raise ContactsError('no contacts found in ' + label)
        return parsed
    except ContactsError as exc:
        message = str(exc).replace(path.name, label)
        if label == 'the form':
            message = re.sub(r': row \d+:', ':', message)
        raise ContactsError(message) from None
    finally:
        path.unlink(missing_ok=True)

def form_contact(values):
    output = io.StringIO()
    names = COLUMNS + ('first_name',)
    writer = csv.DictWriter(output, fieldnames=names)
    writer.writeheader()
    writer.writerow({name: values.get(name, '') for name in names})
    parsed = parse_csv(output.getvalue(), label='the form')
    if not parsed:
        raise ContactsError('name is required')
    return parsed[0]

def contact_from_row(row):
    from vcoutreach.contacts import _slug
    row = dict(row)
    return Contact(id=row.get('slug') or _slug(row['email']), name=row['partner_name'],
        email=row['email'], org=row.get('company') or row.get('firm') or '', role=row.get('role') or '',
        facts=tuple(json.loads(row.get('facts') or '[]')), source_urls=tuple(json.loads(row.get('source_urls') or '[]')),
        lane=Lane(row.get('lane') or 'first_touch'), thread_subject=row.get('thread_subject') or '',
        in_reply_to=row.get('in_reply_to') or '', references=row.get('refs') or '', preferred_name=row.get('first_name') or '')

def contacts(db_path=None):
    conn = get_conn(db_path)
    try:
        return [contact_from_row(row) for row in conn.execute('SELECT * FROM partners ORDER BY id')]
    finally:
        conn.close()

def import_contacts(items, db_path=None, batch_name='Import'):
    items = list(items)
    seen_emails, seen_ids = set(), set()
    with cursor_ctx(db_path) as (conn, cur):
        existing = {row['email'].casefold() for row in conn.execute('SELECT email FROM partners')}
        existing_ids = {row['slug'].casefold() for row in conn.execute('SELECT slug FROM partners WHERE slug IS NOT NULL')}
        for contact in items:
            valid_slug(contact.id)
            email = contact.email.lower()
            if email in existing or email in seen_emails:
                raise ContactsError(f'duplicate email {email!r}')
            if contact.id.casefold() in existing_ids or contact.id.casefold() in seen_ids:
                raise ContactsError(f'duplicate id {contact.id!r}')
            seen_emails.add(email); seen_ids.add(contact.id.casefold())
        cur.execute('INSERT INTO batches(name) VALUES (?)', (batch_name[:160],))
        batch_id = cur.lastrowid
        for contact in items:
            cur.execute('''INSERT INTO partners(partner_name,first_name,firm,email,slug,role,facts,source_urls,lane,thread_subject,in_reply_to,refs,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (contact.name, contact.first_name, contact.org, contact.email.lower(),
                contact.id, contact.role, json.dumps(contact.facts), json.dumps(contact.source_urls), contact.lane.value,
                contact.thread_subject, contact.in_reply_to, contact.references, 'import'))
            cur.execute('INSERT INTO contact_batches(contact_id,batch_id) VALUES (?,?)', (cur.lastrowid, batch_id))
    return len(items)

def csv_safe_cell(value):
    """Keep spreadsheet applications from interpreting exported text as formulas."""
    text = str(value) if value is not None else ''
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text

def export_csv():
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=COLUMNS)
    writer.writeheader()
    for contact in contacts():
        row = {key: getattr(contact, key) for key in COLUMNS}
        row.update(facts=' | '.join(contact.facts), source_urls=' | '.join(contact.source_urls), lane=contact.lane.value)
        writer.writerow({key: csv_safe_cell(value) for key, value in row.items()})
    return output.getvalue()

def engine_settings():
    from vcoutreach.config import load_settings
    workspace = get_workspace()
    return replace(load_settings(env={**__import__('os').environ, **workspace.env()}, dotenv_path=workspace.env_path),
                   workdir=workspace.root, send_mode=send_mode(), daily_send_cap=daily_send_cap(), gmail_token_path=workspace.path('data','.gmail_token.json'),
                   gmail_client_secrets_path=workspace.path('data','.gmail_credentials.json'))

def sender_dict():
    raw = get_workspace().read_json('sender.json', {}) or {}
    defaults = {name: '' for name in SENDER_FIELDS}
    defaults.update(product_lines=[], traction_lines=[], forbidden_phrases=[], approved_claims={})
    return {**defaults, **raw}

def save_sender(values):
    workspace = get_workspace()
    output = sender_dict()
    for name in SENDER_FIELDS:
        if name not in values:
            continue
        value = values[name]
        if name in ('product_lines','traction_lines','forbidden_phrases') and isinstance(value,str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
        elif name == 'approved_claims' and isinstance(value,str):
            try:
                value = json.loads(value or '{}')
            except json.JSONDecodeError:
                raise ValueError('Approved claims: enter a valid JSON object with claim names and values.') from None
        output[name] = value
    temporary = workspace.path('data', 'sender-' + secrets.token_hex(8) + '.json')
    try:
        temporary.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
        load_sender(temporary)
        temporary.replace(workspace.path('sender.json'))
    except OutreachError as exc:
        message = str(exc).replace('sender file ' + temporary.name, 'the sender profile').replace(temporary.name, 'the sender profile')
        raise ValueError(message) from None
    finally:
        temporary.unlink(missing_ok=True)
    return output

def _sender():
    return load_sender(get_workspace().path('sender.json'))

def _draft_paths():
    workspace = get_workspace()
    folder = workspace.drafts_dir
    folder.mkdir(exist_ok=True)
    workspace.path('drafts','blocked')
    paths = list(folder.glob('*.md')) + list((folder/'blocked').glob('*.md'))
    result = []
    for path in paths:
        valid_slug(path.stem)
        result.append(workspace.path(path))
    return sorted(result, key=lambda p: str(p))

def _lint(draft, contact=None):
    # The sender must exist before any draft can pass the gate.
    from vcoutreach.types import LintIssue
    issues = tuple(lint_draft(draft.subject, draft.body, _sender(), contact))
    if not draft.body.strip():
        issues += (LintIssue('empty_body','Write a body before approving this draft.'),)
    if not draft.subject.strip():
        issues += (LintIssue('empty_subject','Write a subject before approving this draft.'),)
    if contact is not None and not contact.facts:
        issues += (LintIssue('insufficient_facts','Add verified contact facts before drafting.'),)
    return replace(draft, issues=issues, status='blocked' if any(i.severity=='error' for i in issues) else 'ok')

def list_drafts():
    workspace = get_workspace()
    all_contacts = {c.id: c for c in contacts()}
    conn = get_conn()
    try:
        ids = {row['slug']: row['id'] for row in conn.execute('SELECT id,slug FROM partners')}
    finally:
        conn.close()
    result = []
    for path in _draft_paths():
        draft = read_draft(path)
        contact = all_contacts.get(draft.contact_id)
        try:
            draft = _lint(draft, contact)
        except OutreachError:
            from vcoutreach.types import LintIssue
            draft = replace(draft,status='blocked',issues=(LintIssue('sender_required','Save a sender profile before checking drafts.'),))
        row = asdict(draft)
        row.update(slug=draft.contact_id, partner_id=ids.get(draft.contact_id),
            recipient_name=contact.name if contact else draft.to, recipient_email=draft.to,
            firm=contact.org if contact else '', word_count=len(draft.body.split()),
            lane=draft.lane.value, editor_url='/crm/drafts/' + draft.contact_id,
            draft_type=draft.lane.value, draft_type_label=draft.lane.value.replace('_',' ').title())
        result.append(row)
    return sorted(result,key=lambda row:(row['status']!='ok',row['slug']))

def get_engine_draft(slug):
    valid_slug(slug)
    workspace = get_workspace()
    for subdir in ('', 'blocked'):
        path = workspace.path('drafts',subdir,slug + '.md')
        if path.is_file():
            return read_draft(path)
    raise ValueError('Draft not found.')

def _check_write_tree():
    # Engine writes and deletes case-insensitive sibling files. Check every sibling first.
    workspace = get_workspace()
    for folder in ('drafts','drafts/blocked'):
        path = workspace.path(folder)
        if path.exists():
            for sibling in path.iterdir():
                workspace.path(sibling)
    workspace.path('drafts/index.jsonl')

def write_drafts(slugs=(), provider='auto', overwrite=False):
    from vcoutreach.pipeline import draft_all
    from vcoutreach.providers import resolve_provider
    workspace = get_workspace()
    _check_write_tree()
    previous = {row['slug'] for row in list_drafts()}
    selected = set(slugs)
    items = [c for c in contacts() if (not selected or c.id in selected) and (overwrite or c.id not in previous)]
    if not items:
        return []
    # Isolate pipeline pruning to this run; merge results into the canonical draft tree.
    temporary = workspace.path('data', 'draft-run-' + secrets.token_hex(8))
    temporary.mkdir()
    try:
        drafts = draft_all(items, _sender(), resolve_provider(engine_settings(),provider),temporary)
        for draft in drafts:
            write_draft(workspace.drafts_dir,draft)
            _sync_legacy(draft)
        write_index(workspace.drafts_dir,[get_engine_draft(row['slug']) for row in list_drafts()])
        return drafts
    finally:
        import shutil
        shutil.rmtree(temporary)

def save_draft(slug, values):
    old = get_engine_draft(slug)
    # To is always bound to the current contact, never a writable recipient field.
    contact = next((c for c in contacts() if c.id == slug),None)
    if contact is None:
        raise ValueError('Contact not found.')
    draft = replace(old,to=contact.email,subject=values.get('subject',old.subject),body=values.get('body',old.body),
        lane=Lane(values.get('lane',old.lane.value)),in_reply_to=values.get('in_reply_to',old.in_reply_to),
        references=values.get('references',old.references))
    draft = _lint(draft,contact)
    _check_write_tree()
    write_draft(get_workspace().drafts_dir,draft)
    _sync_legacy(draft)
    write_index(get_workspace().drafts_dir,[get_engine_draft(row['slug']) for row in list_drafts()])
    return draft

def _sync_legacy(draft):
    from vcoutreach.web.pipeline.loader import upsert_draft
    conn=get_conn()
    try:
        row=conn.execute('SELECT id FROM partners WHERE slug=?',(draft.contact_id,)).fetchone()
    finally:
        conn.close()
    if row:
        draft_id=upsert_draft(row['id'],draft.body,draft.subject,'engine-' + draft.lane.value,skip_auto_approve=True)
        with cursor_ctx() as (_,cur):
            cur.execute("UPDATE drafts SET status='pending',approved_at=NULL,auto_approve_skip_reason=?,is_reply=?,in_reply_to_message_id=? WHERE id=?",
                (None if draft.status=='ok' else ';'.join(i.code for i in draft.issues),int(draft.lane==Lane.FOLLOW_UP),draft.in_reply_to,draft_id))

def send_plan():
    from vcoutreach.send.gate import plan_sends
    from vcoutreach.journal import Journal
    workspace=get_workspace()
    settings=engine_settings();mode=settings.send_mode
    rows=list_drafts()
    drafts=[replace(get_engine_draft(row['slug']),status=row['status']) for row in rows]
    actual_journal=Journal(workspace.path('.vcoutreach','journal.jsonl'))
    # Plan through the engine without its execution-only live switches.
    class PlanJournal:
        def has(self,key,ignored):return actual_journal.has(key,mode)
        def count_today(self,ignored):return actual_journal.count_today(mode)
    report=plan_sends(drafts,replace(settings,send_mode='test',test_recipient_email=''),_sender(),PlanJournal())
    conn=get_conn()
    try:
        stages={row['slug']:dict(row) for row in conn.execute('SELECT p.slug,p.email,p.dropped_reason,p.email_verification_status,cp.stage, (SELECT d.status FROM drafts d WHERE d.partner_id=p.id ORDER BY d.id DESC LIMIT 1) AS review_state FROM partners p LEFT JOIN crm_profiles cp ON cp.partner_id=p.id')}
        reservations={(row['recipient'],row['lane']) for row in conn.execute("SELECT recipient,lane FROM web_send_journal WHERE mode=? AND state IN ('sending','sent','uncertain')",(mode,))}
        used=conn.execute("SELECT COUNT(*) FROM web_send_journal WHERE date(created_at)=date('now') AND state IN ('sending','sent','uncertain')").fetchone()[0]
    finally:
        conn.close()
    used=max(used,actual_journal.count_today('test')+actual_journal.count_today('live'))
    planned=[]
    skipped=[{'slug':s.draft.contact_id,'reason':s.reason,'detail':s.detail} for s in report.skipped]
    from vcoutreach.contacts import _EMAIL_RE
    from vcoutreach.web.gmail.push_worker import gmail_connection_reason
    readiness=[]
    if mode=='test' and not _EMAIL_RE.fullmatch(settings.test_recipient_email or ''):
        readiness.append('Set TEST_RECIPIENT_EMAIL to a valid email in Settings, under Keys and connection settings.')
    connection_reason=gmail_connection_reason()
    if connection_reason:
        readiness.append(connection_reason)
    for item in report.planned:
        contact=stages.get(item.draft.contact_id,{})
        reason=''
        if readiness:
            reason=' '.join(readiness)
        elif item.draft.contact_id not in stages:
            reason='Contact not found.'
        elif contact.get('stage') in {'passed','hold'} or contact.get('dropped_reason'):
            reason='Contact is passed, on hold, or suppressed.'
        elif (item.draft.to.lower(),item.draft.lane.value) in reservations:
            reason='A send was already attempted or completed.'
        elif mode=='live' and contact.get('review_state')!='approved':
            reason='Approve the draft before a LIVE send.'
        elif mode=='live' and contact.get('email_verification_status') not in {'valid','deliverable','pb_verified'}:
            reason='Verify the contact email before a LIVE send.'
        elif used+len(planned)>=settings.daily_send_cap:
            reason='Daily send cap reached.'
        if reason:
            skipped.append({'slug':item.draft.contact_id,'reason':'workspace_gate','detail':reason})
        else:
            planned.append({'slug':item.draft.contact_id,'to':item.draft.to if mode=='live' else settings.test_recipient_email or 'TEST_RECIPIENT_EMAIL not set',
                'subject':item.draft.subject if mode=='live' else item.subject,'mode':mode})
    return {'planned':planned,'skipped':skipped,'sent':[]}

def register_routes(app):
    from flask import request,render_template,redirect,url_for,flash,abort,Response
    from vcoutreach.web.crm_store import STAGES,save_profile,add_activity
    from vcoutreach.web.keys import write_env,KEY_NAMES

    @app.post('/crm/drafts/write')
    def crm_drafts_write():
        selected=request.form.getlist('slugs') or request.form.getlist('slug')
        selected=[slug.strip() for raw in selected for slug in raw.split(',') if slug.strip()]
        try:
            result=write_drafts(selected,request.form.get('provider','auto'),request.form.get('overwrite')=='1')
            flash(f'Wrote {len(result)} drafts.','success')
        except OutreachError as exc:
            flash(str(exc).splitlines()[0], 'error')
        except ValueError:
            flash('Draft generation failed. Check sender, facts, provider keys and model settings.','error')
        return redirect(url_for('crm_drafts'))

    @app.route('/crm/drafts/<slug>',methods=['GET','POST'])
    def crm_draft_editor(slug):
        try:
            valid_slug(slug)
            get_engine_draft(slug)
        except ValueError:
            abort(404)
        if request.method=='POST':
            try:
                draft=save_draft(slug,request.form)
                if request.form.get('action')=='review' or request.form.get('reviewed')=='1':
                    conn=get_conn()
                    try:
                        row=conn.execute('SELECT id FROM partners WHERE slug=?',(slug,)).fetchone()
                    finally:
                        conn.close()
                    if row:
                        add_activity(row['id'],'note','Draft reviewed',draft.subject)
                flash('Draft saved with issues. Review the checks below.' if draft.status=='blocked' else 'Draft saved and checked.',
                      'warning' if draft.status=='blocked' else 'success')
            except (ValueError,OutreachError):
                flash('Could not save draft. Check sender profile, lane and message fields.','error')
            return redirect(url_for('crm_draft_editor',slug=slug))
        rows=list_drafts()
        row=next((row for row in rows if row['slug']==slug),None)
        if row is None:
            abort(404)
        contact=next((asdict(c) for c in contacts() if c.id==slug),{})
        return render_template('crm_draft.html',draft=row,contact=contact,issues=row['issues'],drafts=rows,active_page='drafts')

    @app.get('/crm/send-plan')
    def crm_send_plan():
        error=None
        try:
            plan=send_plan()
        except (OutreachError,ValueError):
            plan={'planned':[],'skipped':[],'sent':[]}
            error='Save a sender profile and check drafts to prepare a send plan.'
        rows=[{**row,'status':'Planned'} for row in plan['planned']]+[{**row,'status':'Held'} for row in plan['skipped']]
        return render_template('crm_send_plan.html',plan=plan,rows=rows,
            summary={'planned':len(plan['planned']),'held':len(plan['skipped'])},error=error,active_page='send_plan')

    @app.route('/crm/new',methods=['GET','POST'])
    def crm_new():
        errors=[]; values=request.form.to_dict() if request.method=='POST' else {}
        if request.method=='POST':
            try:
                if values.get('stage','prospect') not in STAGES:
                    raise ValueError('Choose a valid stage.')
                contact=form_contact(values)
                import_contacts([contact])
                with cursor_ctx() as (conn,cur):
                    partner_id=conn.execute('SELECT id FROM partners WHERE slug=?',(contact.id,)).fetchone()[0]
                    cur.execute('UPDATE partners SET tags=?,notes=?,next_action=? WHERE id=?',
                        (values.get('tags',''),values.get('notes',''),values.get('next_action',''),partner_id))
                save_profile(partner_id,{'stage':values.get('stage','prospect')})
                flash('Contact created.','success')
                return redirect(url_for('crm_contact',partner_id=partner_id))
            except (OutreachError,ValueError) as exc:
                errors=str(exc).splitlines()
        return render_template('crm_form.html',contact=values,errors=errors,stages=STAGES,active_page='crm'),400 if errors else 200

    @app.post('/crm/import')
    def crm_import():
        upload=request.files.get('file') or request.files.get('csv_file')
        try:
            text=upload.read().decode('utf-8-sig') if upload else request.form.get('csv_text','')
            parsed=parse_csv(text)
            from datetime import datetime
            name = Path(upload.filename).stem if upload and upload.filename else 'Import ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            count=import_contacts(parsed,batch_name=name)
            flash(f'Imported {count} contacts.','success')
        except (OutreachError,ValueError,UnicodeError) as exc:
            # CSV errors identify the user's input and retain its row numbers.
            for message in str(exc).splitlines()[:25]:
                flash(message,'error')
        return redirect(url_for('crm_dashboard'))

    @app.get('/crm/export.csv')
    def crm_export():
        return Response(export_csv(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=contacts.csv'})

    @app.post('/crm/<int:partner_id>/delete')
    def crm_contact_delete(partner_id):
        conn=get_conn()
        try:
            row=conn.execute('SELECT * FROM partners WHERE id=?',(partner_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            abort(404)
        contact=contact_from_row(row)
        if request.form.get('confirm_text',request.form.get('confirm',''))!=contact.id:
            flash('Type the contact id to confirm deletion: '+contact.id,'error')
            return redirect(url_for('crm_contact',partner_id=partner_id))
        workspace=get_workspace()
        _check_write_tree()
        removed=workspace.path('drafts','removed');removed.mkdir(exist_ok=True)
        for folder in ('','blocked'):
            source=workspace.path('drafts',folder,contact.id+'.md')
            if source.exists():
                source.replace(workspace.path('drafts','removed',contact.id+'-'+secrets.token_hex(4)+'.md'))
        with cursor_ctx() as (_,cur):
            # Preserve immutable send and reply evidence while removing the contact link.
            cur.execute('UPDATE replies SET partner_id=NULL WHERE partner_id=?',(partner_id,))
            cur.execute('DELETE FROM partners WHERE id=?',(partner_id,))
        flash('Contact deleted. Draft files were archived.','success')
        return redirect(url_for('crm_dashboard'))

    @app.post('/settings/sender')
    def save_sender_profile():
        try:
            save_sender(request.form)
            flash('Sender profile saved.','success')
        except (ValueError,OutreachError) as exc:
            flash(str(exc).splitlines()[0], 'error')
        return redirect(url_for('workspace_settings'))

    @app.post('/settings/keys')
    def save_keys():
        try:
            if request.form.get('key') or request.form.get('name'):
                values={request.form.get('key') or request.form.get('name'):request.form.get('value','')}
            else:
                unknown=set(request.form)-KEY_NAMES-{'csrf_token'}
                if unknown:
                    raise ValueError('Unsupported setting name.')
                values={key:request.form[key] for key in KEY_NAMES if request.form.get(key)}
            write_env(values)
            flash('Settings saved. Key values are never displayed.','success')
        except ValueError as exc:
            flash(str(exc),'error')
        return redirect(url_for('workspace_settings'))

    @app.post('/sample/load')
    def sample_load():
        workspace=get_workspace()
        if contacts() or workspace.path('sender.json').exists() or _draft_paths():
            flash('Load the sample only into an empty workspace. Existing data was kept.','error')
            return redirect(url_for('crm_dashboard'))
        sample=Path(__file__).parent/'sample'
        save_sender(json.loads((sample/'sender.json').read_text(encoding='utf-8')))
        count=import_contacts(load_contacts(sample/'contacts.sample.csv'),batch_name='Sample first touch')
        with cursor_ctx() as (conn,cur):
            cur.execute("INSERT INTO batches(name) VALUES ('Sample follow-up')")
            batch=cur.lastrowid
            cur.execute("INSERT INTO contact_batches(contact_id,batch_id) SELECT id,? FROM partners WHERE lane='follow_up'",(batch,))
        from vcoutreach.web.replies.loader import save_reply
        for reply in json.loads((sample/'replies.json').read_text(encoding='utf-8')):
            conn=get_conn()
            try:
                row=conn.execute('SELECT id FROM partners WHERE email=?',(reply['from_email'],)).fetchone()
            finally:
                conn.close()
            save_reply(gmail_thread_id='sample-thread',gmail_message_id='sample-'+reply['from_email'],
                from_email=reply['from_email'],from_name=reply['from_name'],subject=reply['subject'],
                snippet=reply['body'],body_text=reply['body'],received_at='2026-01-01T12:00:00+00:00',
                partner_id=row['id'] if row else None,draft_id=None,label=reply['label'],
                classification_confidence=None,classification_summary='Fictional sample',suggested_action='Review reply')
        workspace.path('facts.json').write_text(json.dumps({'approved_claims':{},'forbidden_phrases':[]},indent=2)+'\n',encoding='utf-8')
        write_drafts(provider='template')
        flash(f'Loaded {count} fictional sample contacts, a sender profile, drafts and a reply.','success')
        return redirect(url_for('crm_dashboard'))

    @app.post('/replies/add')
    def reply_add():
        from vcoutreach.web.replies.loader import save_reply
        email=request.form.get('from_email','').strip().lower()
        from vcoutreach.contacts import _EMAIL_RE
        if not _EMAIL_RE.fullmatch(email):
            abort(400)
        conn=get_conn()
        try:
            row=conn.execute('SELECT id FROM partners WHERE email=?',(email,)).fetchone()
        finally:
            conn.close()
        save_reply('manual','manual-'+secrets.token_hex(16),email,request.form.get('from_name',''),request.form.get('subject',''),
            request.form.get('body','')[:500],request.form.get('body',''),__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
            row['id'] if row else None,None,'unlabeled',None,None,None)
        flash('Reply added. Choose a label after reviewing it.','success')
        return redirect(url_for('replies_list'))
