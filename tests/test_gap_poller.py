"""Gmail reply polling return contract on an isolated empty workspace."""
from __future__ import annotations

import pytest
import base64
from unittest.mock import Mock

from web_helpers import make_web, web_env_isolation  # noqa: F401


def test_empty_outreach_workspace_has_no_threads_or_new_replies(make_web):
    """Contract lines 63 to 71: poll sent outreach and return counts and lists."""
    from vcoutreach.web.replies.poller import poll_gmail_for_replies

    workspace = make_web()
    with workspace.app.app_context():
        result = poll_gmail_for_replies()
    assert result["threads_scanned"] == 0
    assert result["new_replies"] == 0
    assert result["replies"] == []
    assert result["errors"] == []


@pytest.mark.parametrize('limit,expected', [(None, 2), (1, 1)])
def test_populated_threads_ignore_own_messages_and_deduplicate_replies(make_web, monkeypatch, limit, expected):
    from vcoutreach.web.gmail import api
    from vcoutreach.web.replies.poller import poll_gmail_for_replies
    from vcoutreach.web.replies import classifier
    from web_helpers import ADA, GRACE, SENDER_EMAIL, page_text
    from web_gap_helpers import ready_web, sql_exec, sql
    w = ready_web(make_web, [ADA, GRACE])
    monkeypatch.setattr(classifier, 'classify_reply', lambda *a, **k: {
        'label': 'interested', 'confidence': 0.99, 'summary': 'Interested in meeting next week',
        'suggested_action': 'Schedule a meeting', 'suggested_response': 'Thanks, happy to find a time.'})
    for row in (ADA, GRACE):
        slug = row['id']
        sql_exec(w, "UPDATE drafts SET status='sent', gmail_message_id=?, thread_id=?, sent_to_address=? "
                    "WHERE partner_id=(SELECT id FROM partners WHERE slug=?)",
                 ('sent-' + slug, 'thread-' + slug, row['email'], slug))
    def message(mid, sender, text, sent=False):
        return {'id': mid, 'threadId': mid.split('-')[-1], 'labelIds': ['SENT'] if sent else ['INBOX'],
                'internalDate': '1792512000000',
                'payload': {'mimeType': 'text/plain', 'headers': [
                    {'name': 'From', 'value': sender}, {'name': 'To', 'value': SENDER_EMAIL},
                    {'name': 'Subject', 'value': 'Re: A short conversation'},
                    {'name': 'Date', 'value': 'Tue, 20 Oct 2026 12:00:00 +0000'}],
                    'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}}}
    threads = {'thread-' + r['id']: {'id': 'thread-' + r['id'], 'messages': [
        message('sent-' + r['id'], SENDER_EMAIL, 'Our original outreach', True),
        message('own-' + r['id'], SENDER_EMAIL, 'Our own follow up', True),
        message('reply-' + r['id'], r['email'], 'Interested, happy to meet next week.')]} for r in (ADA, GRACE)}
    monkeypatch.setattr(api, 'get_service', lambda: Mock())
    monkeypatch.setattr(api, 'get_profile', lambda: {'emailAddress': SENDER_EMAIL})
    monkeypatch.setattr(api, 'get_message', lambda mid, **k: {'id': mid, 'threadId': 'thread-' + mid.split('-')[-1]})
    monkeypatch.setattr(api, 'get_thread', lambda tid: threads[tid])
    monkeypatch.setattr(api, 'list_threads_in_inbox', lambda *a, **k: [])
    with w.app.app_context():
        result = poll_gmail_for_replies(limit=limit)
        repeated = poll_gmail_for_replies(limit=limit)
    assert result['errors'] == [], result
    assert result['threads_scanned'] == expected, result
    assert result['new_replies'] == expected, result
    assert repeated['new_replies'] == 0, repeated
    rows = sql(w, 'SELECT from_email, gmail_message_id, body_text FROM replies')
    assert len(rows) == expected
    assert all(sender in {ADA['email'], GRACE['email']} and mid.startswith('reply-')
               for sender, mid, text in rows)
    assert all(text == 'Interested, happy to meet next week.' for sender, mid, text in rows)
    assert 'Our own follow up' not in w.text('/replies')
