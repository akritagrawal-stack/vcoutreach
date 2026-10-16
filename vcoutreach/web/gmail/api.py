"""
Gmail API integration - OAuth + draft creation.

Tracking pixel injection REMOVED - this project does not use open/click tracking.

Scope: gmail.modify (read replies, create drafts and explicitly send).
Credentials: data/.gmail_credentials.json + data/.gmail_token.json
"""
from __future__ import annotations

import base64
import logging
import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from vcoutreach.web.config import GMAIL_CREDS_PATH, GMAIL_TOKEN_PATH, PITCH_DECK_PATH, get_workspace

logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
]


class GmailAuthNotReady(Exception):
    """Raised when gmail-web authorization is missing or invalid."""


def _get_credentials():
    """Load cached credentials, refresh if expired, raise GmailAuthNotReady if missing."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise GmailAuthNotReady(
            'google-auth libs not installed. '
            'Run: pip install google-auth google-auth-oauthlib google-api-python-client'
        ) from exc

    token_path = str(GMAIL_TOKEN_PATH)
    if not os.path.exists(token_path):
        raise GmailAuthNotReady(
            'Gmail token is missing. Run `vcoutreach auth gmail-web --workspace DIR` for this workspace.'
        )

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:
        raise GmailAuthNotReady('Gmail token is unreadable. Run `vcoutreach auth gmail-web --workspace DIR`.') from None
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            logger.info('Refreshing expired Gmail token...')
            try:
                creds.refresh(Request())
                from vcoutreach.send.gmail import _write_token
                _write_token(Path(token_path), creds.to_json())
            except Exception:
                raise GmailAuthNotReady('Gmail token refresh failed. Run `vcoutreach auth gmail-web --workspace DIR`.') from None
        else:
            raise GmailAuthNotReady(
                'Cached token is invalid or has no refresh token. '
                'Re-run `vcoutreach auth gmail-web --workspace DIR`.'
            )
    return creds


def _build_service(creds=None):
    from googleapiclient.discovery import build
    if creds is None:
        creds = _get_credentials()
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


def get_service():
    """Return an authenticated Gmail API service client."""
    return _build_service()


def get_profile() -> dict:
    """Return the authenticated user's Gmail profile."""
    service = _build_service()
    return service.users().getProfile(userId='me').execute()


def run_oauth_flow() -> str:
    """
    Interactive one-time OAuth flow. Opens a browser, stores token.
    Returns a completion message; tokens and client secrets stay in the workspace.
    """
    from vcoutreach.send.gmail import _require_google, _write_token

    workspace = get_workspace()
    creds_path = workspace.path('data', '.gmail_credentials.json')
    token_path = workspace.path('data', '.gmail_token.json')
    if not creds_path.is_file():
        raise GmailAuthNotReady(
            'Gmail OAuth client JSON is missing. Enable the Gmail API in Google Cloud Console, '
            'create a Desktop app OAuth client, save its JSON at data/.gmail_credentials.json '
            'under the workspace, then run `vcoutreach auth gmail-web --workspace DIR`.'
        )
    google = _require_google()
    try:
        flow = google.InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0, prompt='consent')
        _write_token(token_path, creds.to_json())
    except Exception:
        raise GmailAuthNotReady(
            'Gmail web authorization failed. Check the Desktop app client JSON at '
            'data/.gmail_credentials.json and retry `vcoutreach auth gmail-web --workspace DIR`.'
        ) from None
    return 'Gmail web authorization complete.'


def _wrap_html(body_html: str) -> str:
    """Wrap body HTML in a proper email document."""
    if '<html' in body_html.lower():
        return body_html
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="utf-8"></head>'
        '<body style="font-family: Arial, sans-serif; font-size: 14px; '
        'line-height: 1.6; color: #333;">'
        f'{body_html}'
        '</body></html>'
    )


def _make_raw(
    to: str,
    subject: str,
    body_text: str,
    body_html: str,
    attachment_path: Path | None = None,
    attachment_filename: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """
    Build a base64url-encoded MIME message.

    If attachment_path is provided and the file exists, it is attached as a
    PDF with the name attachment_filename (defaults to the file's basename).
    If the file does not exist, a warning is logged and the email is sent
    without an attachment (graceful degrade).
    """
    if attachment_path is not None:
        attachment_path = get_workspace().path(attachment_path)
    if attachment_path is not None and attachment_path.exists():
        # multipart/mixed wraps body + attachment
        msg = MIMEMultipart('mixed')
        msg['To'] = to
        msg['Subject'] = subject
        if in_reply_to:
            msg['In-Reply-To'] = in_reply_to
            msg['References'] = references if references else in_reply_to
        # Build the text/html body as a nested alternative part
        body_part = MIMEMultipart('alternative')
        body_part.attach(MIMEText(body_text, 'plain', 'utf-8'))
        body_part.attach(MIMEText(_wrap_html(body_html), 'html', 'utf-8'))
        msg.attach(body_part)
        # Attach the PDF
        filename = attachment_filename or attachment_path.name
        with attachment_path.open('rb') as fh:
            pdf_data = fh.read()
        pdf_part = MIMEApplication(pdf_data, _subtype='pdf')
        pdf_part.add_header('Content-Disposition', 'attachment', filename=filename)
        msg.attach(pdf_part)
        logger.info(
            'Attaching %s (%d bytes) as "%s"',
            attachment_path, len(pdf_data), filename,
        )
    else:
        if attachment_path is not None:
            logger.warning(
                'PITCH_DECK_PATH set to %s but file not found - sending without attachment.',
                attachment_path,
            )
        msg = MIMEMultipart('alternative')
        msg['To'] = to
        msg['Subject'] = subject
        if in_reply_to:
            msg['In-Reply-To'] = in_reply_to
            msg['References'] = references if references else in_reply_to
        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        msg.attach(MIMEText(_wrap_html(body_html), 'html', 'utf-8'))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')


def send_message(
    service,
    to: str,
    subject: str,
    body_text: str,
    body_html: str,
    no_deck: bool = False,
    in_reply_to: str | None = None,
    thread_id: str | None = None,
    references: str | None = None,
) -> str:
    """
    Send an email immediately via Gmail. Returns the Gmail message id.

    Args:
        service:      Authenticated Gmail API client (from get_service()).
        to:           Recipient email address.
        subject:      Email subject line.
        body_text:    Plain-text body.
        body_html:    HTML body (will be wrapped in <html>...</html>).
        no_deck:      If True, skip attaching the pitch deck even if PITCH_DECK_PATH is set.
                      Useful for test sends that don't need the attachment.
        in_reply_to:  RFC 2822 Message-ID of the message being replied to (e.g.
                      '<CABEbgnC...@mail.example.com>'). Sets In-Reply-To and References
                      headers in the MIME message to thread correctly.
        thread_id:    Gmail thread ID (e.g. '19e3d5822064565e'). When provided, the
                      Gmail API places the sent message inside the existing thread.
        references:   Full References header value (space-separated chain of Message-IDs).
                      If omitted, defaults to in_reply_to alone.

    The pitch deck (PITCH_DECK_PATH from .env) is attached automatically when:
      - no_deck is False, AND
      - PITCH_DECK_PATH is set in .env, AND
      - the file exists at that path.
    If the file is missing, a warning is logged and the email is sent without attachment.

    For correct Gmail threading, supply both thread_id AND in_reply_to.  The subject
    must also match (or begin with "Re: ") the existing thread subject.

    Returns:
        Gmail message id string.
    """
    # Auto-prefix subject with Re: if replying and not already prefixed
    if in_reply_to and not subject.lower().startswith('re:'):
        subject = 'Re: ' + subject

    raw_deck = get_workspace().value('PITCH_DECK_PATH')
    deck_path = get_workspace().path(raw_deck) if raw_deck and not no_deck else None
    raw = _make_raw(
        to, subject, body_text, body_html,
        attachment_path=deck_path,
        attachment_filename=None,
        in_reply_to=in_reply_to,
        references=references,
    )
    body: dict = {'raw': raw}
    if thread_id:
        body['threadId'] = thread_id
    response = service.users().messages().send(
        userId='me',
        body=body,
    ).execute()
    message_id = response.get('id', '')
    returned_thread = response.get('threadId', '')
    logger.info(
        'Sent Gmail message id=%s to=%s thread=%s',
        message_id, to, returned_thread,
    )
    return message_id


def create_draft(
    service,
    to: str,
    subject: str,
    body_text: str,
    body_html: str,
    no_deck: bool = False,
) -> str:
    """
    Create one Gmail draft. Returns the Gmail draft id.

    Args:
        service:    Authenticated Gmail API client (from get_service()).
        to:         Recipient email address.
        subject:    Email subject line.
        body_text:  Plain-text body.
        body_html:  HTML body (will be wrapped in <html>...</html>).
        no_deck:    If True, skip attaching the pitch deck.

    Returns:
        Gmail draft id string.
    """
    raw_deck = get_workspace().value('PITCH_DECK_PATH')
    deck_path = get_workspace().path(raw_deck) if raw_deck and not no_deck else None
    raw = _make_raw(
        to, subject, body_text, body_html,
        attachment_path=deck_path,
        attachment_filename=None,
    )
    response = service.users().drafts().create(
        userId='me',
        body={'message': {'raw': raw}},
    ).execute()
    draft_id = response.get('id', '')
    logger.info('Created Gmail draft id=%s for to=%s', draft_id, to)
    return draft_id


# ── Read / inbox helpers (for reply polling) ─────────────────────────────────

def list_threads_in_inbox(query: str, max_results: int = 50) -> list[dict]:
    """
    Search Gmail and return thread stubs matching query.

    Args:
        query:       Gmail search string, e.g. 'in:inbox newer_than:7d from:...'
        max_results: Maximum number of threads to return.

    Returns:
        List of {'id': thread_id, 'snippet': ...} dicts.
    """
    service = _build_service()
    response = service.users().threads().list(
        userId='me',
        q=query,
        maxResults=max_results,
    ).execute()
    return response.get('threads', [])


def get_thread(thread_id: str) -> dict:
    """
    Fetch a full Gmail thread (all messages, full format).

    Args:
        thread_id: Gmail thread ID string.

    Returns:
        Gmail thread resource dict with 'messages' list.
    """
    service = _build_service()
    return service.users().threads().get(
        userId='me',
        id=thread_id,
        format='full',
    ).execute()


def get_message(message_id: str, format: str = 'metadata') -> dict:
    """
    Fetch a single Gmail message.

    Args:
        message_id: Gmail message ID string.
        format:     'metadata', 'full', or 'minimal'.

    Returns:
        Gmail message resource dict.
    """
    service = _build_service()
    return service.users().messages().get(
        userId='me',
        id=message_id,
        format=format,
    ).execute()
