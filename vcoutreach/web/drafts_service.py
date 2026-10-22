"""Engine-backed contacts, draft files and read-only send plans."""
from __future__ import annotations
import csv
import io
import json
import logging
import re
import secrets
import unicodedata
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

def clean_contact_text(value, maximum, *, notes=False):
    text = ''.join(c for c in str(value or '') if
        (unicodedata.category(c) != 'Cc' or notes and c in '\n\t')
        and not ('\u202a' <= c <= '\u202e' or '\u2066' <= c <= '\u2069'))
    text = text.strip()
    if len(text) > maximum:
        raise ValueError(f'Contact field exceeds {maximum} characters.')
    return text

def friendly_message(value):
    text = str(value)
    if 'Gmail is not connected.' in text:
        return re.sub(r'Gmail is not connected\..*', 'Gmail is not connected. Open Settings and choose Connect Gmail.', text)
    if 'linkedin' in text.lower() and any(word in text.lower() for word in ('not seeded', 'not set up', 'linkedin-auth')):
        return 'LinkedIn is not set up; skipped.'
    return text

_NO_CONTACTS_MESSAGE = 'No contacts found. Paste or upload a CSV with a header row and the columns name and email (optional: org, role, facts).'

def parse_csv(text, *, label='your CSV', report=None):
    if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise ContactsError('That file is not a UTF-8 CSV')
    if text.lstrip()[:1] in {'{','['}:
        raise ContactsError(label+' looks like JSON, expected a CSV file with a header row')
    workspace = get_workspace()
    path = workspace.path('data', 'import-' + secrets.token_hex(8) + '.csv')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        records = list(csv.reader(io.StringIO(text)))
        header_index = next((i for i, cells in enumerate(records) if any(c.strip() for c in cells)), None)
        if header_index is None:
            raise ContactsError(_NO_CONTACTS_MESSAGE)
        # Validate each row through the shared loader so duplicate emails can be skipped later.
        # Keep original row numbers when reporting malformed input.
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(records[header_index])
        path.write_text(output.getvalue(), encoding='utf-8')
        load_contacts(path, report=report)
        parsed, problems = [], []
        blank_rows = header_index
        header_text = output.getvalue()
        for index, cells in enumerate(records[header_index+1:], header_index+2):
            if not any(cell.strip() for cell in cells):
                blank_rows += 1
                continue
            output = io.StringIO()
            output.write(header_text)
            csv.writer(output).writerow(cells)
            path.write_text(output.getvalue(), encoding='utf-8')
            try:
                parsed.extend(load_contacts(path))
            except ContactsError as exc:
                problems.extend(str(exc).replace('row 2', f'row {index}').splitlines())
        if problems:
            raise ContactsError('\n'.join(problems[:25]))
        if report is not None:
            report['skipped_blank_rows'] = blank_rows
        if not parsed:
            raise ContactsError(_NO_CONTACTS_MESSAGE)
        return parsed
    except (ContactsError, csv.Error) as exc:
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

def manual_fact_list(raw):
    """Facts the user typed or imported for a contact (partners.facts), as {source, text} dicts."""
    try:
        items = json.loads(raw) if isinstance(raw, (str, bytes)) else list(raw or [])
    except (ValueError, TypeError):
        return []
    return [{'source': 'manual', 'text': str(item).strip()} for item in items if str(item).strip()]

def contacts(db_path=None, include_enrichment=False):
    conn = get_conn(db_path)
    try:
        found = [contact_from_row(row) for row in conn.execute('SELECT * FROM partners ORDER BY id')]
    finally:
        conn.close()
    if include_enrichment:
        # Stored enrichment facts follow the CSV facts; export and import stay CSV-only.
        from vcoutreach.web.enrichment_facts import with_enrichment_many
        found = with_enrichment_many(found, db_path=db_path)
    return found

def import_contacts(items, db_path=None, batch_name='Import', *, report=None):
    items = list(items)
    seen_emails, seen_ids = set(), set()
    accepted = []
    skipped = 0
    with cursor_ctx(db_path) as (conn, cur):
        existing = {row['email'].casefold() for row in conn.execute('SELECT email FROM partners')}
        existing_ids = {row['slug'].casefold() for row in conn.execute('SELECT slug FROM partners WHERE slug IS NOT NULL')}
        for contact in items:
            valid_slug(contact.id)
            email = contact.email.lower()
            if email in existing or email in seen_emails:
                skipped += 1
                continue
            if contact.id.casefold() in existing_ids or contact.id.casefold() in seen_ids:
                raise ContactsError(f'duplicate id {contact.id!r}')
            contact = replace(contact, name=clean_contact_text(contact.name,200),
                org=clean_contact_text(contact.org,200), role=clean_contact_text(contact.role,200))
            if not contact.name:
                raise ContactsError('Contact name is required.')
            accepted.append(contact)
            seen_emails.add(email); seen_ids.add(contact.id.casefold())
        cur.execute('INSERT INTO batches(name) VALUES (?)', (batch_name[:160],))
        batch_id = cur.lastrowid
        for contact in accepted:
            cur.execute('''INSERT INTO partners(partner_name,first_name,firm,email,slug,role,facts,source_urls,lane,thread_subject,in_reply_to,refs,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (contact.name, contact.first_name, contact.org, contact.email.lower(),
                contact.id, contact.role, json.dumps(contact.facts), json.dumps(contact.source_urls), contact.lane.value,
                contact.thread_subject, contact.in_reply_to, contact.references, 'import'))
            cur.execute('INSERT INTO contact_batches(contact_id,batch_id) VALUES (?,?)', (cur.lastrowid, batch_id))
    if report is not None:
        report['skipped_existing'] = skipped
    return len(accepted)

def csv_safe_cell(value):
    """Keep spreadsheet applications from interpreting exported text as formulas."""
    text = str(value).replace('\x00','') if value is not None else ''
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
    from vcoutreach.web.llm.chain import _CLAUDE_HAIKU
    workspace = get_workspace()
    loaded = load_settings(env={**__import__('os').environ, **workspace.env()}, dotenv_path=workspace.env_path)
    # Same default the hook path uses, so a saved Anthropic key works without a separate model setting.
    return replace(loaded, anthropic_model=loaded.anthropic_model or _CLAUDE_HAIKU,
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
    if output.get('website'):
        from vcoutreach.web.server.workspace_settings import validate_http_url
        output['website'] = validate_http_url(output['website'])
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

_NEEDS_FACTS_CODES = {'no_facts', 'insufficient_facts'}

def _plural(count, one, many):
    return one if count == 1 else many

def _contact_names(slugs):
    """(partner_id, display name) for each slug, in the given order."""
    conn = get_conn()
    try:
        rows = {r['slug']: (r['id'], r['partner_name'] or r['slug']) for r in conn.execute('SELECT id, slug, partner_name FROM partners')}
    finally:
        conn.close()
    return [rows[s] for s in slugs if s in rows]

def _plain_error(message):
    """Strip workspace and temp file paths from an engine error before showing it."""
    text = re.sub(r'(?:[A-Za-z]:)?(?:/[^\s:\'"]+)+', 'the file', str(message))
    return text.replace('sender file the file', 'the sender profile')


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
    all_contacts = {c.id: c for c in contacts(include_enrichment=True)}
    conn = get_conn()
    try:
        ids = {row['slug']: row['id'] for row in conn.execute('SELECT id,slug FROM partners')}
        checks = {row['partner_id']: dict(row) for row in conn.execute('SELECT partner_id,hook,hallucination_check,status,auto_approve_skip_reason,boilerplate_version FROM drafts ORDER BY id')}
    finally:
        conn.close()
    result = []
    for path in _draft_paths():
        try:
            draft = read_draft(path)
        except OutreachError:
            logging.getLogger(__name__).warning('Skipping unreadable draft file %s', path.name)
            continue
        contact = all_contacts.get(draft.contact_id)
        try:
            draft = _lint(draft, contact)
        except OutreachError:
            from vcoutreach.types import LintIssue
            draft = replace(draft,status='blocked',issues=(LintIssue('sender_required','Save a sender profile before checking drafts.'),))
        row = asdict(draft)
        review = checks.get(ids.get(draft.contact_id), {})
        reason = '; '.join(i.message for i in draft.issues if i.severity == 'error') if draft.status == 'blocked' else ''
        from vcoutreach.web.llm.hook_grounding import grounding_state, hook_is_ungrounded
        if not reason and review.get('auto_approve_skip_reason') and (review.get('boilerplate_version') or '').startswith('engine-'):
            reason=review['auto_approve_skip_reason']
        if not reason and hook_is_ungrounded(review):
            reason=grounding_state(review)
        row.update(slug=draft.contact_id, partner_id=ids.get(draft.contact_id),
            grounding='blocked: '+reason if reason else grounding_state(review),
            blocked_reason=reason, review_state=review.get('status','pending'),
            approval_state='blocked' if reason else review.get('status','pending'),
            recipient_name=contact.name if contact else draft.to, recipient_email=contact.email if contact else draft.to,
            firm=contact.org if contact else '', word_count=len(draft.body.split()),
            lane=draft.lane.value, editor_url='/crm/drafts/' + draft.contact_id,
            draft_type=draft.lane.value, draft_type_label=draft.lane.value.replace('_',' ').title())
        result.append(row)
    return sorted(result,key=lambda row:(row['status']!='ok',row['slug']))

def draft_block_reason(row, engine_rows=None):
    row = dict(row)
    if engine_rows is None:
        engine_rows = list_drafts()
    engine = next((item for item in engine_rows if item['partner_id'] == row.get('partner_id')), None)
    if engine and engine.get('blocked_reason'):
        return engine['blocked_reason']
    reason = row.get('auto_approve_skip_reason') or ''
    if row.get('status') == 'blocked' or 'insufficient_facts' in reason or (row.get('boilerplate_version') or '').startswith('engine-') and reason:
        return reason or 'Save and check this draft before approval.'
    from vcoutreach.web.llm.hook_grounding import hook_is_ungrounded, grounding_state
    if hook_is_ungrounded(row):
        return grounding_state(row)
    return ''

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
    items = [c for c in contacts(include_enrichment=True) if (not selected or c.id in selected) and (overwrite or c.id not in previous)]
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
    contact = next((c for c in contacts(include_enrichment=True) if c.id == slug),None)
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
    from vcoutreach.email_copy import opener_from_body
    from vcoutreach.web.llm.hook_grounding import check_hook_grounding
    from vcoutreach.web.enrichment.orchestrator import get_enrichment_facts
    from vcoutreach.web.vendor import load_sender_context
    conn=get_conn()
    try:
        row=conn.execute('SELECT * FROM partners WHERE slug=?',(draft.contact_id,)).fetchone()
    finally:
        conn.close()
    if row:
        hook=opener_from_body(draft.body,row['first_name'])
        sender,facts=load_sender_context()
        grounded,reason=check_hook_grounding(hook,dict(row),get_enrichment_facts(row['id'],limit=None),sender,facts)
        check=('grounded' if grounded else 'ungrounded: '+reason) if hook else ''
        draft_id=upsert_draft(row['id'],hook,draft.subject,'engine-' + draft.lane.value,skip_auto_approve=True)
        with cursor_ctx() as (_,cur):
            cur.execute("UPDATE drafts SET status='pending',approved_at=NULL,hallucination_check=?,auto_approve_skip_reason=?,is_reply=?,in_reply_to_message_id=? WHERE id=?",
                (check,None if draft.status=='ok' else ';'.join(i.code for i in draft.issues),int(draft.lane==Lane.FOLLOW_UP),draft.in_reply_to,draft_id))

def send_plan():
    from vcoutreach.send.gate import plan_sends
    from vcoutreach.journal import Journal
    workspace=get_workspace()
    settings=engine_settings();mode=settings.send_mode
    rows=list_drafts()
    drafts=[replace(get_engine_draft(row['slug']),status=row['status'],to=row['recipient_email']) for row in rows]
    actual_journal=Journal(workspace.path('.vcoutreach','journal.jsonl'))
    # Plan through the engine without its execution-only live switches.
    class PlanJournal:
        def has(self,key,ignored):return actual_journal.has(key,mode)
        def count_today(self,ignored):return actual_journal.count_today(mode)
    report=plan_sends(drafts,replace(settings,send_mode='test',test_recipient_email=''),_sender(),PlanJournal())
    conn=get_conn()
    try:
        stages={row['slug']:dict(row) for row in conn.execute('''SELECT p.slug,p.partner_name,p.email,p.dropped_reason,p.email_verification_status,cp.stage,
            d.status AS review_state,d.hook,d.hallucination_check FROM partners p
            LEFT JOIN crm_profiles cp ON cp.partner_id=p.id
            LEFT JOIN drafts d ON d.id=(SELECT latest.id FROM drafts latest WHERE latest.partner_id=p.id ORDER BY latest.id DESC LIMIT 1)''')}
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
        readiness.append('Set a test recipient email in Settings, in the Test recipient card.')
    connection_reason=gmail_connection_reason()
    if connection_reason:
        readiness.append(friendly_message(connection_reason))
    for item in report.planned:
        contact=stages.get(item.draft.contact_id,{})
        reason=''
        from vcoutreach.web.llm.hook_grounding import hook_is_ungrounded, grounding_state
        if contact.get('review_state') == 'rejected':
            reason='Draft was rejected.'
        elif contact.get('review_state') != 'approved':
            reason='Draft is not approved. Approve it before sending.'
        elif str(contact.get('stage') or '').casefold() == 'contacted':
            reason='Contact is already Contacted.'
        elif hook_is_ungrounded(contact):
            reason=grounding_state(contact)+'. Regenerate the hook or edit it before sending.'
        elif readiness:
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
            planned.append({'slug':item.draft.contact_id,'to':item.draft.to if mode=='live' else settings.test_recipient_email or 'Test recipient not set',
                'subject':item.draft.subject if mode=='live' else item.subject,'mode':mode})
    from vcoutreach.web.llm.hook_grounding import hook_is_ungrounded, grounding_state
    draft_rows={row['slug']:row for row in rows}
    for row in skipped:
        contact=stages.get(row['slug'],{})
        state=grounding_state(contact)
        if not draft_rows.get(row['slug'],{}).get('blocked_reason') and hook_is_ungrounded(contact) and state not in row.get('detail',''):
            row['detail']=state+'. '+row.get('detail','')
        draft=draft_rows.get(row['slug'],{})
        row.update(to=settings.test_recipient_email if mode=='test' else 'Held',
                   subject=draft.get('subject',''),mode=mode)
    for row in planned + skipped:
        contact=stages.get(row['slug'],{})
        draft=draft_rows.get(row['slug'],{})
        row.update(recipient_name=contact.get('partner_name',row['slug']), recipient_email=contact.get('email',''),
                   approval_state=draft.get('approval_state',contact.get('review_state') or 'pending'))
        if draft.get('blocked_reason'):
            row['detail']='blocked: '+draft['blocked_reason']+' '+row.get('detail','')
        row['to']=(settings.test_recipient_email or 'Test recipient not set') if mode=='test' else row.get('to','Held')
    return {'planned':planned,'skipped':skipped,'sent':[]}

def send_reviewed_batch(ids):
    from vcoutreach.web.gmail.push_worker import send_with_throttle, gmail_connection_reason
    from vcoutreach.web.crm_store import add_activity
    accepted, errors = [], []
    engine_rows=list_drafts()
    connection_reason=friendly_message(gmail_connection_reason())
    for draft_id in ids:
        conn=get_conn()
        try:
            row=conn.execute('SELECT d.*,p.partner_name,p.email,cp.stage FROM drafts d '
                'JOIN partners p ON p.id=d.partner_id LEFT JOIN crm_profiles cp ON cp.partner_id=p.id WHERE d.id=?',(draft_id,)).fetchone()
        finally:
            conn.close()
        reason=''
        if row is None:
            errors.append(f'Draft {draft_id}: Draft not found.')
            continue
        row=dict(row)
        if row['status']=='rejected':
            reason='Draft was rejected.'
        elif row['status']!='approved':
            reason='Draft is not approved. Approve it before sending.'
        elif str(row.get('stage') or '').casefold()=='contacted':
            reason='Contact is already Contacted.'
        else:
            reason=draft_block_reason(row,engine_rows) or connection_reason
        if reason:
            errors.append(f"{row['partner_name']} <{row['email']}>: {reason}")
            add_activity(row['partner_id'],'note','Send refused',reason)
        else:
            accepted.append(draft_id)
    result={'sent':0,'failed':len(errors),'errors':errors}
    for draft_id in accepted:
        outcome=send_with_throttle([draft_id])
        result['sent']+=outcome['sent']
        result['failed']+=outcome['failed']
        conn=get_conn()
        try:
            row=conn.execute('SELECT d.partner_id,p.partner_name,p.email FROM drafts d '
                'JOIN partners p ON p.id=d.partner_id WHERE d.id=?',(draft_id,)).fetchone()
        finally:
            conn.close()
        for error in outcome.get('errors',[]):
            reason=friendly_message(error)
            result['errors'].append(f"{row['partner_name']} <{row['email']}>: {reason}" if row else reason)
            if row:
                add_activity(row['partner_id'],'note','Send refused',reason)
    return result


def attachment_deck_file():
    """Return the configured pitch deck file only when it is a real file inside <workspace>/attachments, else None."""
    from vcoutreach.web.server.workspace_settings import load_settings
    configured = str(load_settings().get('pitch_deck_path') or '').strip()
    if not configured:
        return None
    try:
        root = get_workspace().path('attachments').resolve()
        candidate = Path(configured).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if candidate.is_file() and candidate.is_relative_to(root):
        return candidate
    return None

def template_deck_panel():
    """Read-only view model for the Template and deck panel (blurb plus deck link)."""
    from vcoutreach.web.server.workspace_settings import load_settings
    settings = load_settings()
    blurb = str(settings.get('outreach_blurb_template') or '').strip()
    url = str(settings.get('pitch_deck_url') or '').strip()
    configured_path = str(settings.get('pitch_deck_path') or '').strip()
    deck_file = attachment_deck_file()
    label = str(settings.get('pitch_deck_label') or '').strip()
    return {'blurb': blurb, 'deck_url': url, 'deck_path_set': bool(configured_path),
            'deck_file_name': deck_file.name if deck_file else '',
            'deck_label': label or (deck_file.name if deck_file else 'Pitch deck'),
            'has_deck': bool(url or deck_file)}

def register_routes(app):
    from flask import request,render_template,redirect,url_for,flash,abort,Response,send_file

    @app.get('/crm/deck')
    def crm_deck_file():
        # Serves only the configured pitch_deck_path, and only when it resolves inside <workspace>/attachments.
        deck = attachment_deck_file()
        if deck is None:
            abort(404)
        return send_file(deck, as_attachment=False, download_name=deck.name, max_age=0)

    from vcoutreach.web.crm_store import STAGES,save_profile,add_activity
    from vcoutreach.web.keys import write_env,KEY_NAMES,key_status

    @app.post('/crm/drafts/write')
    def crm_drafts_write():
        from markupsafe import Markup, escape
        selected=request.form.getlist('slugs') or request.form.getlist('slug')
        selected=[slug.strip() for raw in selected for slug in raw.split(',') if slug.strip()]
        try:
            try:
                _sender()
            except OutreachError:
                flash(Markup('Add your sender profile first. <a href="'+url_for('workspace_settings')+'#sec-sender">Open the sender profile card in Settings</a>.'),'error')
                return redirect(url_for('crm_drafts'))
            overwrite=request.form.get('overwrite')=='1'
            skipped=sum(1 for row in list_drafts() if not selected or row['slug'] in selected) if not overwrite else 0
            from vcoutreach.web.setup_steps import stored_provider_preference
            result=write_drafts(selected,request.form.get('provider') or stored_provider_preference(),overwrite)
            needs=[d.contact_id for d in result if any(i.code in _NEEDS_FACTS_CODES for i in d.issues)]
            message=Markup(escape(f'Wrote {len(result)} '+_plural(len(result),'draft','drafts')))
            if needs:
                names=_contact_names(needs)
                links=Markup(', ').join(Markup('<a href="{}">{}</a>').format(url_for('crm_contact',partner_id=pid),name) for pid,name in names[:8])
                more=len(names)-8
                message+=Markup(escape(f', {len(needs)} '+('needs facts' if len(needs)==1 else 'need facts')+': '))+links
                if more>0:
                    message+=Markup(escape(f' and {more} more'))
                message+=Markup('. Open each contact and use Add facts.')
            else:
                message+=Markup('.')
            if skipped:
                message+=Markup(escape(f" {skipped} "+('contact already has a draft' if skipped==1 else 'contacts already have a draft')+". Tick 'Replace existing drafts' to rewrite them with the model. Replacing discards the saved hook."))
            flash(message,'warning' if skipped or needs else 'success')
        except OutreachError as exc:
            flash(_plain_error(str(exc).splitlines()[0]), 'error')
        except ValueError:
            flash('Draft generation failed. Check sender, facts, provider keys and model settings.','error')
        return redirect(url_for('crm_drafts'))

    @app.post('/crm/<int:partner_id>/facts')
    def crm_contact_add_facts(partner_id):
        conn=get_conn()
        try:
            row=conn.execute('SELECT id,facts FROM partners WHERE id=?',(partner_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            abort(404)
        added=[]
        try:
            existing=json.loads(row['facts'] or '[]')
            if not isinstance(existing,list):
                existing=[]
        except ValueError:
            existing=[]
        known={str(f).strip().lower() for f in existing}
        merged=[str(f) for f in existing]
        for raw in re.split(r'[\r\n]+|\s\|\s',request.form.get('facts','')):
            fact=clean_contact_text(raw,500)
            if fact and fact.lower() not in known:
                known.add(fact.lower()); merged.append(fact); added.append(fact)
        if not added:
            flash('Enter at least one new fact, one per line.','error')
            return redirect(url_for('crm_contact',partner_id=partner_id))
        with cursor_ctx() as (_,cur):
            cur.execute('UPDATE partners SET facts=? WHERE id=?',(json.dumps(merged),partner_id))
        flash(f'Saved {len(added)} '+_plural(len(added),'fact','facts')+'. Write a draft again to use '+('it' if len(added)==1 else 'them')+'.','success')
        return redirect(url_for('crm_contact',partner_id=partner_id))

    @app.route('/crm/drafts/<slug>',methods=['GET','POST'])
    def crm_draft_editor(slug):
        try:
            valid_slug(slug)
            get_engine_draft(slug)
        except ValueError:
            abort(404)
        if request.method=='POST':
            try:
                was_approved=any(row['slug']==slug and row['review_state']=='approved' for row in list_drafts())
                draft=save_draft(slug,request.form)
                if request.form.get('action')=='review' or request.form.get('reviewed')=='1':
                    conn=get_conn()
                    try:
                        row=conn.execute('SELECT id FROM partners WHERE slug=?',(slug,)).fetchone()
                    finally:
                        conn.close()
                    if row:
                        add_activity(row['id'],'note','Draft reviewed',draft.subject)
                from markupsafe import Markup
                approve_link=Markup('<a href="{}#approve">approve it here</a>').format('/crm/drafts/'+slug)
                reviewed=request.form.get('action')=='review' or request.form.get('reviewed')=='1'
                saved_row=next((r for r in list_drafts() if r['slug']==slug),None)
                if draft.status=='blocked' or (saved_row and saved_row.get('blocked_reason')):
                    flash('Draft saved, but it is blocked. See the guidance below.','warning')
                elif was_approved:
                    flash(Markup('Edit saved. The draft went back to Pending and needs approval again: ')+approve_link+Markup('.'),'warning')
                elif reviewed:
                    flash(Markup('Draft saved and marked reviewed. Reviewing does not approve it, so ')+approve_link+Markup(' before sending.'),'success')
                else:
                    flash('Draft saved and checked.','success')
            except (ValueError,OutreachError):
                flash('Could not save draft. Check sender profile, lane and message fields.','error')
            return redirect(url_for('crm_draft_editor',slug=slug))
        rows=list_drafts()
        row=next((row for row in rows if row['slug']==slug),None)
        if row is None:
            abort(404)
        lanes=[(lane.value,lane.value.replace('_',' ').title()) for lane in Lane]
        return render_template('crm_draft.html',draft=row,drafts=rows,lanes=lanes,deck_panel=template_deck_panel(),active_page='drafts')

    @app.get('/crm/send-plan')
    def crm_send_plan():
        error=None
        try:
            plan=send_plan()
        except (OutreachError,ValueError):
            plan={'planned':[],'skipped':[],'sent':[]}
            error='Save a sender profile and check drafts to prepare a send plan.'
        rows=[{**row,'status':'Planned'} for row in plan['planned']]+[{**row,'status':'Held'} for row in plan['skipped']]
        return render_template('crm_send_plan.html',rows=rows,
            summary={'planned':len(plan['planned']),'held':len(plan['skipped'])},error=error,active_page='send_plan')

    @app.route('/crm/new',methods=['GET','POST'])
    def crm_new():
        errors=[]; values=request.form.to_dict() if request.method=='POST' else {}
        if request.method=='POST':
            try:
                if values.get('stage','prospect') not in STAGES:
                    raise ValueError('Choose a valid stage.')
                contact=form_contact(values)
                notes=clean_contact_text(values.get('notes',''),5000,notes=True)
                if not import_contacts([contact]):
                    raise ContactsError('Duplicate email: another contact already has this email address.')
                with cursor_ctx() as (conn,cur):
                    partner_id=conn.execute('SELECT id FROM partners WHERE slug=?',(contact.id,)).fetchone()[0]
                    cur.execute('UPDATE partners SET tags=?,notes=?,next_action=? WHERE id=?',
                        (values.get('tags',''),notes,values.get('next_action',''),partner_id))
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
            report={}
            parsed=parse_csv(text,report=report)
            from datetime import datetime
            name = Path(upload.filename).stem if upload and upload.filename else 'Import ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            count=import_contacts(parsed,batch_name=name,report=report)
            message=f'Imported {count} new '+('contact' if count==1 else 'contacts')+f'; skipped {report.get("skipped_existing",0)} already in this workspace'
            dropped=report.get('dropped_columns',[])
            skipped=report.get('skipped_blank_rows',0)
            if dropped:
                message+=f' Dropped {len(dropped)} unsupported columns: '+', '.join(dropped)+'.'
            if skipped:
                message+=f' Skipped {skipped} blank rows.'
            flash(message,'warning' if dropped or skipped else 'success')
        except UnicodeError:
            flash('That file is not a UTF-8 CSV','error')
        except (OutreachError,ValueError) as exc:
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
        if request.form.get('confirm') != 'yes' and request.form.get('confirm_text','') != contact.id:
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
            cur.execute('UPDATE partners SET dedup_of=NULL WHERE dedup_of=?',(partner_id,))
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
        from vcoutreach.web.server.workspace_settings import CONNECTION_SETTING_NAMES, save_connection_settings
        wants_json=request.accept_mimetypes.best=='application/json'
        try:
            if request.form.get('key') or request.form.get('name'):
                values={request.form.get('key') or request.form.get('name'):request.form.get('value','')}
            else:
                unknown=set(request.form)-CONNECTION_SETTING_NAMES-{'csrf_token','action','clear'}
                if unknown:
                    raise ValueError('Unsupported setting name.')
                values={key:request.form[key] for key in CONNECTION_SETTING_NAMES if key in request.form}
            clear=request.form.get('clear','')
            if clear:
                if clear not in CONNECTION_SETTING_NAMES:
                    raise ValueError('Unsupported setting name.')
                values[clear]=''
            blank_key=any(not value.strip() and key.endswith(('_KEY','_TOKEN')) and key != clear for key,value in values.items())
            values={key:(value if value.strip() else '') for key,value in values.items() if value.strip() or key in {'TEST_RECIPIENT_EMAIL','SEARXNG_URL'} or key==clear}
            save_connection_settings(values)
            message='Settings saved. Key values are never displayed.'
            if values.get('TEST_RECIPIENT_EMAIL') == '':
                message='Test recipient cleared; test sends are held until one is set'
            elif blank_key:
                message='No key entered; saved key unchanged'
            if wants_json:
                return {'ok':True,'status':'unchanged' if blank_key and not values else 'saved','message':message,'saved':sorted(values),'key_status':key_status()}
            flash(message,'success')
        except ValueError as exc:
            if wants_json:
                return {'ok':False,'error':str(exc)},400
            flash(str(exc),'error')
        return redirect('/settings')

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
