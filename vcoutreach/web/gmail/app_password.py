"""Private Gmail app-password storage and a small IMAP/SMTP service adapter."""
from __future__ import annotations

import base64
import imaplib
import json
import re
import smtplib
import ssl
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

SECRET_FILE = '.gmail_app_password.json'
CONNECT_HINT = 'Open Settings and choose Connect Gmail.'
AUTH_ERROR = (
    'Gmail rejected the app password. Check the Gmail address and use a new 16-character '
    'app password, not your Google password. 2-Step Verification must be on. '
    'If app passwords are unavailable, your Workspace admin may have disabled them. '
    + CONNECT_HINT
)


def secret_path(workspace=None):
    from vcoutreach.web.config import get_workspace
    return (workspace or get_workspace()).path('data', SECRET_FILE)


def configured(workspace=None):
    return secret_path(workspace).is_file()


def _validate(address, password):
    from vcoutreach.contacts import _EMAIL_RE
    if not isinstance(address, str) or not _EMAIL_RE.fullmatch(address.strip()):
        raise ValueError('Enter a single valid Gmail address.')
    if not isinstance(password, str):
        raise ValueError('Enter a 16-character app password. Spaces are allowed.')
    password = ''.join(password.split())
    if len(password) != 16 or not password.isascii() or not password.isalnum():
        raise ValueError('Enter a 16-character app password. Spaces are allowed.')
    return address.strip(), password


def load_credentials(workspace=None):
    from vcoutreach.web.gmail.api import GmailAuthNotReady
    try:
        data = json.loads(secret_path(workspace).read_text(encoding='utf-8'))
        return _validate(data['address'], data['password'])
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise GmailAuthNotReady('Gmail app-password connection needs attention. ' + CONNECT_HINT) from None


def connection_info(workspace=None):
    """Return public connection metadata only, without contacting Gmail."""
    if not configured(workspace):
        target = secret_path(workspace).parent
        oauth = any((target / name).is_file() for name in ('.gmail_token.json', '.gmail_credentials.json'))
        return {'mode': 'oauth' if oauth else 'app_password', 'address': ''}
    try:
        address, _ = load_credentials(workspace)
    except Exception:
        return {'mode': 'app_password', 'address': ''}
    return {'mode': 'app_password', 'address': address}


def connection_factory(protocol):
    """The only socket seam. Tests replace this function for both protocols."""
    context = ssl.create_default_context()
    if protocol == 'imap':
        return imaplib.IMAP4_SSL('imap.gmail.com', 993, ssl_context=context, timeout=15)
    if protocol == 'smtp':
        return smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context, timeout=15)
    raise ValueError('Unknown mail protocol.')


@contextmanager
def _connection(protocol, address, password):
    """Discard provider exception text, which can contain login credentials."""
    from vcoutreach.web.gmail.api import GmailAuthNotReady
    connection = None
    try:
        connection = connection_factory(protocol)
        if protocol == 'imap':
            connection.debug = 0
        else:
            connection.set_debuglevel(0)
        try:
            result = connection.login(address, password)
            if protocol == 'imap' and result[0] != 'OK':
                raise GmailAuthNotReady(AUTH_ERROR)
        except imaplib.IMAP4.abort:
            raise
        except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
            raise GmailAuthNotReady(AUTH_ERROR) from None
        yield connection
    except GmailAuthNotReady:
        raise
    except (OSError, EOFError, imaplib.IMAP4.abort, smtplib.SMTPServerDisconnected):
        raise GmailAuthNotReady(
            'Could not reach Gmail over a secure connection. Check your network and try again. '
            + CONNECT_HINT
        ) from None
    except Exception:
        raise GmailAuthNotReady('Gmail could not complete the request. ' + CONNECT_HINT) from None
    finally:
        if connection is not None:
            try:
                if protocol == 'imap':
                    connection.logout()
                else:
                    connection.quit()
            except Exception:
                try:
                    if protocol == 'imap':
                        connection.shutdown()
                    else:
                        connection.close()
                except Exception:
                    pass


def connect(address, password, workspace=None):
    """Verify both logins before atomically replacing the private secret."""
    address, password = _validate(address, password)
    for protocol in ('imap', 'smtp'):
        with _connection(protocol, address, password):
            pass
    from vcoutreach.send.gmail import _write_token
    try:
        _write_token(secret_path(workspace), json.dumps({'address': address, 'password': password}))
    except OSError:
        raise ValueError('Could not save the Gmail connection. Check workspace permissions.') from None


def _quote(value):
    if any(char in value for char in '\r\n\x00'):
        raise ValueError('Invalid Gmail search value.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _ok(response):
    status, data = response
    if status != 'OK':
        raise ValueError('Gmail IMAP request failed.')
    return data


def _mailbox(connection, kind):
    """Discover localized folders by Special-Use flags, keeping IMAP wire names."""
    defaults = {'All': '[Gmail]/All Mail', 'Sent': '[Gmail]/Sent Mail', 'Drafts': '[Gmail]/Drafts'}
    for line in _ok(connection.list()) or []:
        if not isinstance(line, bytes):
            continue
        match = re.match(rb'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$', line)
        if match and ('\\' + kind).encode() in match[1].split():
            name = match[2].decode('ascii')
            return name if name.startswith('"') else _quote(name)
    return _quote(defaults[kind])


def _select(connection, kind='All'):
    mailbox = 'INBOX' if kind == 'INBOX' else _mailbox(connection, kind)
    _ok(connection.select(mailbox, readonly=True))


def _search(connection, *criteria):
    data = _ok(connection.uid('SEARCH', None, *criteria))
    return data[0].split() if data and data[0] else []


def _raw_search(connection, query):
    quoted = _quote(query)
    if not query.isascii():
        connection.literal = query.encode('utf-8')
        return _search(connection, 'X-GM-RAW')
    return _search(connection, 'X-GM-RAW', quoted)


def _payload(message):
    result = {'mimeType': message.get_content_type(),
              'headers': [{'name': name, 'value': str(value)} for name, value in message.items()]}
    if message.is_multipart():
        result['parts'] = [_payload(part) for part in message.iter_parts()]
        result['body'] = {'size': 0}
    else:
        raw = message.get_payload(decode=True) or b''
        if message.get_content_maintype() == 'text':
            raw = raw.decode(message.get_content_charset() or 'utf-8', errors='replace').encode('utf-8')
        result['body'] = {'size': len(raw), 'data': base64.urlsafe_b64encode(raw).decode('ascii')}
        result['filename'] = message.get_filename() or ''
    return result


def _fetch(connection, uid, format='full'):
    body = 'BODY.PEEK[]' if format in {'full', 'raw'} else 'BODY.PEEK[HEADER]'
    data = _ok(connection.uid('FETCH', uid, f'(X-GM-MSGID X-GM-THRID X-GM-LABELS INTERNALDATE {body})'))
    for item in data or []:
        if not isinstance(item, tuple):
            continue
        metadata, raw = item
        msg_id = re.search(rb'X-GM-MSGID (\d+)', metadata)
        thread_id = re.search(rb'X-GM-THRID (\d+)', metadata)
        if not msg_id or not thread_id:
            continue
        message = BytesParser(policy=policy.default).parsebytes(raw)
        result = {'id': f'{int(msg_id[1]):x}', 'threadId': f'{int(thread_id[1]):x}',
                  'payload': _payload(message), 'labelIds': []}
        labels = re.search(rb'X-GM-LABELS \(([^)]*)\)', metadata)
        for flag, label in ((b'\\Sent', 'SENT'), (b'\\Inbox', 'INBOX'), (b'\\Drafts', 'DRAFT')):
            if labels and flag in labels[1].split():
                result['labelIds'].append(label)
        try:
            internal = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
            date = internal[1].decode('ascii') if internal else str(message.get('Date', ''))
            result['internalDate'] = str(int(parsedate_to_datetime(date).timestamp() * 1000))
        except (ValueError, TypeError, OverflowError):
            result['internalDate'] = '0'
        plain = message.get_body(preferencelist=('plain',))
        result['snippet'] = plain.get_content()[:200] if plain and format == 'full' else ''
        if format == 'raw':
            result['raw'] = base64.urlsafe_b64encode(raw).decode('ascii')
        return result
    raise ValueError('Gmail message not found.')


class _Request:
    def __init__(self, operation):
        self.operation = operation

    def execute(self):
        return self.operation()


class _Resource:
    def __init__(self, service, kind):
        self.service, self.kind = service, kind

    def get(self, **args):
        return _Request(lambda: self.service.read(self.kind, **args))

    def list(self, **args):
        return _Request(lambda: self.service.search(self.kind, **args))

    def send(self, **args):
        return _Request(lambda: self.service.send_raw(args['body']['raw']))

    def create(self, **args):
        return _Request(lambda: self.service.draft_raw(args['body']['message']['raw']))


class AppPasswordService:
    """The Gmail resource subset used by sends, CRM refresh and reply polling."""
    auth_mode = 'app_password'

    def __init__(self, workspace):
        self.workspace = workspace
        load_credentials(workspace)

    def users(self):
        return self

    def messages(self):
        return _Resource(self, 'messages')

    def threads(self):
        return _Resource(self, 'threads')

    def drafts(self):
        return _Resource(self, 'drafts')

    def getProfile(self, **args):
        return _Request(lambda: {'emailAddress': load_credentials(self.workspace)[0]})

    def read(self, kind, id, format='full', **args):
        address, password = load_credentials(self.workspace)
        with _connection('imap', address, password) as connection:
            _select(connection)
            if id.startswith('<') and id.endswith('>') and kind == 'messages':
                uids = _raw_search(connection, 'rfc822msgid:' + id[1:-1])
            else:
                key = 'X-GM-THRID' if kind == 'threads' else 'X-GM-MSGID'
                uids = _search(connection, key, str(int(id, 16)))
            if not uids:
                raise ValueError('Gmail message not found.')
            messages = [_fetch(connection, uid, format) for uid in uids]
            messages.sort(key=lambda message: int(message['internalDate']))
            return {'id': id, 'messages': messages} if kind == 'threads' else messages[0]

    def search(self, kind, q='', maxResults=50, pageToken=None, **args):
        address, password = load_credentials(self.workspace)
        with _connection('imap', address, password) as connection:
            _select(connection)
            uids = list(reversed(_raw_search(connection, q)))
            start = int(pageToken or 0)
            size = max(1, min(int(maxResults), 1000))
            # Thread pages must deduplicate before applying the requested cap.
            items, seen = [], set()
            index = start
            while index < len(uids) and len(items) < size:
                message = _fetch(connection, uids[index], 'metadata')
                index += 1
                item_id = message['threadId'] if kind == 'threads' else message['id']
                if item_id not in seen:
                    seen.add(item_id)
                    items.append({'id': item_id, 'threadId': message['threadId']})
            result = {kind: items}
            if index < len(uids):
                result['nextPageToken'] = str(index)
            return result

    def _wire_message(self, raw, address):
        message = BytesParser(policy=policy.SMTP).parsebytes(base64.urlsafe_b64decode(raw))
        if not message.get('From'):
            message['From'] = address
        if not message.get('Message-ID'):
            message['Message-ID'] = make_msgid()
        if not message.get('Date'):
            message['Date'] = formatdate(localtime=False)
        return message

    def send_raw(self, raw):
        address, password = load_credentials(self.workspace)
        # Parsing and transport errors are always replaced with fixed safe text.
        with _connection('smtp', address, password) as connection:
            message = self._wire_message(raw, address)
            recipients = [email for _, email in getaddresses(
                message.get_all('To', []) + message.get_all('Cc', []) + message.get_all('Bcc', []))]
            if not recipients:
                raise ValueError('Gmail message has no recipients.')
            del message['Bcc']
            refused = connection.sendmail(address, recipients, message.as_bytes())
            if refused:
                raise ValueError('Gmail refused one or more recipients.')
        message_id = str(message['Message-ID'])
        # Gmail saves SMTP sends itself. Never append or resend after acceptance.
        try:
            with _connection('imap', address, password) as connection:
                _select(connection, 'Sent')
                uids = _raw_search(connection, 'rfc822msgid:' + message_id.strip('<>'))
                if uids:
                    result = _fetch(connection, uids[-1], 'metadata')
                    return {'id': result['id'], 'threadId': result['threadId']}
        except Exception:
            pass
        # Sent indexing can lag. This durable RFC id resolves on a later poll.
        return {'id': message_id, 'threadId': ''}

    def draft_raw(self, raw):
        address, password = load_credentials(self.workspace)
        with _connection('imap', address, password) as connection:
            message = self._wire_message(raw, address)
            _ok(connection.append(_mailbox(connection, 'Drafts'), '(\\Draft)', None, message.as_bytes()))
            _select(connection, 'Drafts')
            uids = _raw_search(connection, 'rfc822msgid:' + str(message['Message-ID']).strip('<>'))
            result = _fetch(connection, uids[-1], 'metadata') if uids else {'id': str(message['Message-ID'])}
            return {'id': result['id'], 'message': result}
