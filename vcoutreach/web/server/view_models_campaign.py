"""View models for the Campaign page (/crm/drafts), the draft editor and the send plan.

Pure helpers: they shape the rows from drafts_service.list_drafts() and the dict from drafts_service.campaign_state()
into what the templates draw (stepper state, status pills, checks summary, tab counts, editor approve state). Nothing
here sends mail, writes files or calls a live API.
"""
from __future__ import annotations

from urllib.parse import quote as _quote, urlencode

# One color map for every draft status pill (crm_drafts table, crm_draft header, Other drafts list).
# key -> (label, pk-pill modifier)
PILLS = {
    'needs-review': ('Needs review', 'needs-review'),
    'approved': ('Approved', 'approved'),
    'blocked': ('Blocked', 'blocked'),
    'rejected': ('Rejected', 'rejected'),
    'sent': ('Sent', 'sent'),
    'test-sent': ('Test sent', 'test-sent'),
}

# Lint issue code -> plain-English hover text. Unknown codes fall back to the engine message.
ISSUE_HOVER = {
    'empty_body': 'The email body is empty.',
    'empty_subject': 'The subject line is empty.',
    'insufficient_facts': 'Not enough saved facts about this person. Add a fact to the contact, then write the draft again.',
    'sender_required': 'Save your sender profile first so drafts can be signed.',
    'placeholder': 'The draft still has placeholder text in it, such as a name in brackets.',
    'em_dash': 'The draft uses a long dash. Replace it with a comma, period or colon.',
    'filler': 'The draft uses filler words that make it sound generic.',
    'exclamation': 'The draft uses an exclamation mark. Remove it to keep the tone calm.',
    'forbidden_phrase': 'The draft uses a phrase that is not allowed in outreach.',
    'empty_hook': 'The opening line is empty.',
}

TAB_LABELS = (
    ('pending', 'Needs review'),
    ('approved', 'Approved'),
    ('blocked', 'Blocked'),
    ('all', 'All'),
)


def pill_key(row) -> str:
    """Which status the row shows. Blocked wins, then the stored review state."""
    if row.get('status') == 'blocked' or row.get('approval_state') == 'blocked':
        return 'blocked'
    for key in ('sent', 'test_sent'):
        if row.get(key):
            return key.replace('_', '-')
    state = row.get('approval_state') or row.get('review_state') or 'pending'
    if state == 'approved':
        return 'approved'
    if state == 'rejected':
        return 'rejected'
    return 'needs-review'


def pill(row) -> dict:
    key = pill_key(row)
    label, mod = PILLS[key]
    return {'key': key, 'label': label, 'cls': 'pk-pill pk-pill--dot pk-pill--' + mod}


def issue_hover(issue) -> str:
    code = (issue.get('code') if isinstance(issue, dict) else getattr(issue, 'code', '')) or ''
    message = (issue.get('message') if isinstance(issue, dict) else getattr(issue, 'message', '')) or ''
    return ISSUE_HOVER.get(code) or message or 'This draft needs a fix.'


def checks(row) -> dict:
    """Checks column: OK, or N issues with a plain-English hover. The raw messages stay for screen readers."""
    issues = list(row.get('issues') or [])
    blocked = bool(row.get('blocked_reason'))
    n = len(issues)
    if n == 0 and blocked:
        n = 1
    hover = [issue_hover(i) for i in issues]
    if not hover and blocked:
        hover = [row.get('blocked_reason')]
    raw = [(i.get('message') or i.get('code')) if isinstance(i, dict) else (getattr(i, 'message', '') or getattr(i, 'code', '')) for i in issues]
    return {
        'ok': n == 0,
        'count': n,
        'label': 'OK' if n == 0 else ('1 issue' if n == 1 else '%d issues' % n),
        'hover': ' '.join(hover),
        'raw': raw,
    }


def decorate(rows) -> list:
    """Return rows with `pill` and `checks` added (rows are copied, the input is not changed)."""
    out = []
    for row in rows:
        item = dict(row)
        item['pill'] = pill(row)
        item['checks'] = checks(row)
        out.append(item)
    return out


def _tab_href(status: str, query: str) -> str:
    args = {'status': status}
    if query:
        args['q'] = query
    return '/crm/drafts?' + urlencode(args)


def tabs(all_rows, status_filter: str, query: str) -> list:
    """Status tabs with counts from the full list. A Rejected tab appears only when something is rejected."""
    counts = {'pending': 0, 'approved': 0, 'blocked': 0, 'rejected': 0, 'all': len(all_rows)}
    for row in all_rows:
        state = row.get('approval_state') or 'pending'
        if state in counts:
            counts[state] += 1
    spec = list(TAB_LABELS)
    if counts['rejected']:
        spec.insert(3, ('rejected', 'Rejected'))
    active = status_filter if status_filter in dict(spec) else 'all'
    return [{'status': s, 'label': label, 'count': counts[s], 'active': s == active, 'href': _tab_href(s, query)} for s, label in spec]


def approved_count(all_rows) -> int:
    return sum(1 for row in all_rows if row.get('approval_state') == 'approved')


def write_button(campaign, all_rows) -> dict:
    """Label for the primary Write drafts button: contacts without a draft, or all contacts when every one has one."""
    contacts = int(campaign.get('contact_count') or 0)
    drafted = int(campaign.get('draft_count') or 0)
    n = contacts - drafted if contacts > drafted else contacts
    n = max(n, 0)
    if n == 0:
        label = 'Write drafts with AI'
    else:
        label = 'Write %d %s with AI' % (n, 'draft' if n == 1 else 'drafts')
    return {'n': n, 'label': label}


def stepper(campaign, approved: int) -> dict:
    """State of the five steps. Exactly one step is current. Steps 1 to 3 done puts the drafts table first."""
    c = campaign
    pitch_done = bool(c.get('kind_chosen') and c.get('has_blurb') and c.get('has_deck'))
    done1 = int(c.get('contact_count') or 0) > 0
    done3 = int(c.get('draft_count') or 0) > 0
    done4 = bool(c.get('test_sent'))
    first_three_done = done1 and pitch_done and done3
    done = {1: done1, 2: pitch_done, 3: done3, 4: done4, 5: False}
    if not done1:
        current = 1
    elif not pitch_done:
        current = 2
    elif not done3:
        current = 3
    elif approved > 0:
        current = 5
    else:
        current = 4
    # A test send is recommended, not required, once something is approved: step 4 reads as collapsed.
    if current == 5 and not done4:
        step4_note = 'Test send recommended'
    else:
        step4_note = ''
    return {
        'done': done,
        'current': current,
        'pitch_done': pitch_done,
        'table_first': first_three_done,
        'step4_note': step4_note,
        'approved': approved,
        'send_label': 'Send to contacts',
        'send_count': approved,
        'send_href': '/partners?status=approved',
        'strip': [{'n': n, 'done': done[n], 'current': n == current} for n in (1, 2, 3, 4, 5)],
    }


# Readiness rows keep the order the tests pin: Gmail, test recipient, sender profile, one approved draft.
# key -> (short label used in the pill, row title)
READINESS_ROWS = (
    ('gmail', 'gmail', 'Gmail connected'),
    ('recipient', 'test recipient', 'Test recipient set'),
    ('sender', 'sender profile', 'Sender profile saved'),
    ('approved', 'approved draft', 'One draft approved'),
)


def readiness(campaign, flags=None) -> dict:
    """The four readiness rows for the test send, the first row that blocks, and the pill wording.

    `flags` is optional and only adds an override: {'same_as_contact': bool} marks the recipient row as not ready
    when the test recipient is the same address as the contact on the previewed draft.
    """
    c = campaign or {}
    flags = flags or {}
    gmail = c.get('gmail')
    preview = c.get('preview') or {}
    same = bool(flags.get('same_as_contact', preview.get('same_as_contact')))
    recipient = (c.get('test_recipient') or '').strip()
    first_slug = c.get('first_slug') or ''
    approved_ok = c.get('first_approved_partner_id') is not None and bool(c.get('first_approved_slug'))
    approved_n = int(c.get('approved_count') or 0) if approved_ok else 0
    # One count drives the row title, its status and the Campaign copy, so they can never disagree.
    approved_title = 'One draft approved' if approved_n <= 1 else '%d drafts approved' % approved_n
    ok = {
        'gmail': gmail == 'connected',
        'recipient': bool(recipient),
        'sender': bool(c.get('has_sender')),
        'approved': approved_ok,
    }
    # Everything else passes but the preview shows the recipient is the contact: the test send would be refused.
    recipient_clash = ok['gmail'] and ok['recipient'] and ok['sender'] and ok['approved'] and same
    if recipient_clash:
        ok['recipient'] = False
    status = {
        'gmail': 'Connected' if ok['gmail'] else ('OAuth required: choose Connect Gmail' if gmail == 'OAuth required' else 'Not connected'),
        'recipient': (recipient if ok['recipient'] else
                      ('Same address as the contact on this draft, so a test send is refused. Use your own inbox.' if recipient_clash
                       else 'Not set. Test emails go only to this address.')),
        'sender': (c.get('sender_name') or 'Saved') if ok['sender'] else 'Not saved. Fill it in under step 3.',
        'approved': ('Approved: %d ready to send' % max(approved_n, 1)) if ok['approved'] else (
            'Not yet. Open the first draft to review it.' if first_slug else
            ('No drafts yet. Write drafts in step 3.' if int(c.get('contact_count') or 0) > 0 else 'No contacts yet. Add one in step 1.')),
    }
    if first_slug:
        approve_href = '/crm/drafts/%s#approve' % _quote(first_slug)
    elif int(c.get('contact_count') or 0) > 0:
        approve_href = '#writeDrafts'
    else:
        approve_href = '/crm/new?next=campaign'
    fix = {
        'gmail': ('/settings#svc-gmail', 'Fix'),
        'recipient': ('/settings#svc-test-recipient', 'Fix'),
        'sender': ('#writeDrafts', 'Fix'),
        'approved': (approve_href, 'Fix'),
    }
    rows = []
    for key, label, title in READINESS_ROWS:
        href, fix_label = fix[key]
        if key == 'approved':
            title = approved_title
        rows.append({'key': key, 'label': label, 'title': title, 'ok': ok[key], 'status': status[key],
                     'fix_href': href, 'fix_label': fix_label})
    blocking = next((r for r in rows if not r['ok']), None)
    todo = sum(1 for r in rows if not r['ok'])
    pill_text = 'Ready to test send' if blocking is None else 'Not ready: ' + blocking['label']
    return {
        'rows': rows,
        'blocking': blocking,
        'todo': todo,
        'ready': blocking is None,
        'recipient_clash': recipient_clash,
        'pill_text': pill_text,
        'pill_cls': 'pk-pill pk-pill--dot ' + ('pk-pill--success' if blocking is None else 'pk-pill--warning'),
        'count_line': ('To do: %d %s' % (todo, 'item' if todo == 1 else 'items')) if todo else '',
    }


# Draft status funnel for the Campaign right rail: key -> label. Rejected drafts are not shown (they are out of the flow).
FUNNEL = (
    ('needs-review', 'Needs review'),
    ('approved', 'Approved'),
    ('blocked', 'Blocked'),
    ('sent', 'Sent'),
)


def funnel(all_rows) -> dict:
    """Counts of drafts per status for the right-rail bars. width is a percent of the largest bucket (0 when empty)."""
    counts = {key: 0 for key, _ in FUNNEL}
    for row in all_rows or []:
        key = pill_key(row)
        if key == 'test-sent':
            key = 'needs-review' if (row.get('approval_state') or 'pending') != 'approved' else 'approved'
        if key in counts:
            counts[key] += 1
    peak = max(counts.values()) if counts else 0
    rows = [{'key': key, 'label': label, 'n': counts[key],
             'width': (round(100.0 * counts[key] / peak, 1) if peak else 0),
             'pill_cls': 'pk-pill pk-pill--dot pk-pill--' + key}
            for key, label in FUNNEL]
    return {'rows': rows, 'total': sum(counts.values()), 'empty': peak == 0}


def editor(draft, all_rows=None) -> dict:
    """Approve state for the draft editor: why Approve is disabled, and how many issues are left to fix."""
    issues = list(draft.get('issues') or [])
    errors = [i for i in issues if (i.get('severity') if isinstance(i, dict) else getattr(i, 'severity', 'error')) in (None, 'error')]
    n = len(errors) or len(issues)
    blocked = bool(draft.get('blocked_reason'))
    if blocked and n == 0:
        n = 1
    approved = draft.get('review_state') == 'approved'
    reason = ''
    if blocked:
        reason = 'Fix %d %s to approve' % (n, 'issue' if n == 1 else 'issues')
    key = pill_key(draft)
    # Back goes to the step the person was last on: fixing issues is step 3, reviewing is step 4.
    back_step = 3 if (key in ('needs-review', 'blocked') and n > 0) else 4
    back_label = 'Back to step %d: %s' % (back_step, 'Write drafts' if back_step == 3 else 'Review and approve')
    # Checks meter: one check per lint issue plus the blocked reason when it has no lint issue of its own.
    total_checks = max(len(issues), n, 1) if (issues or blocked) else 1
    passing = max(total_checks - n, 0)
    nxt = None
    ordered = list(all_rows or [])
    slugs = [r.get('slug') for r in ordered]
    if draft.get('slug') in slugs:
        i = slugs.index(draft.get('slug'))
        for r in ordered[i + 1:] + ordered[:i]:
            if pill_key(r) in ('needs-review', 'blocked'):
                nxt = r
                break
    return {
        'pill': pill(draft),
        'back_step': back_step,
        'back_label': back_label,
        'back_href': '/crm/drafts#step-%d' % back_step,
        'next_draft': nxt,
        'checks_total': total_checks,
        'checks_passing': passing,
        'checks_pct': round(100.0 * passing / total_checks, 1),
        'issue_count': n,
        'blocked': blocked,
        'approved': approved,
        'can_approve': (not blocked) and (not approved) and bool(draft.get('partner_id')),
        'reason': reason,
        'checks': checks(draft),
        'issue_rows': [{'code': (i.get('code') if isinstance(i, dict) else getattr(i, 'code', '')) or '',
                        'text': issue_hover(i)} for i in issues],
        'paragraphs': [p for p in (draft.get('body') or '').split('\n\n') if p.strip()],
    }


def sibling_rows(rows) -> list:
    return decorate(rows)
