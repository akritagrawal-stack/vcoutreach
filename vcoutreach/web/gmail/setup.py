"""Shared, private Desktop OAuth client import for browser and native Settings."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import urlsplit

MAX_CLIENT_BYTES = 64 * 1024
CLIENT_ERROR = 'Choose a Google Cloud Desktop app OAuth client JSON file.'


def validate_oauth_client(raw: bytes) -> dict:
    """Accept Google's installed-app format only, with fixed Google endpoints.

    Reject web clients, service accounts, tokens and endpoint substitutions before
    any library can open a browser or send a client secret to a remote endpoint.
    """
    if not isinstance(raw, (bytes, bytearray, str)) or not raw or len(raw) > MAX_CLIENT_BYTES:
        raise ValueError(CLIENT_ERROR)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError(CLIENT_ERROR) from None
    if not isinstance(payload, dict) or set(payload) != {'installed'}:
        raise ValueError(CLIENT_ERROR)
    client = payload['installed']
    if not isinstance(client, dict):
        raise ValueError(CLIENT_ERROR)
    for field in ('client_id', 'client_secret'):
        if not isinstance(client.get(field), str) or not client[field].strip():
            raise ValueError(CLIENT_ERROR)
    if (client.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth'
            or client.get('token_uri') != 'https://oauth2.googleapis.com/token'):
        raise ValueError(CLIENT_ERROR)
    if client.get('auth_provider_x509_cert_url', 'https://www.googleapis.com/oauth2/v1/certs') != 'https://www.googleapis.com/oauth2/v1/certs':
        raise ValueError(CLIENT_ERROR)
    redirects = client.get('redirect_uris')
    if not isinstance(redirects, list) or not redirects:
        raise ValueError(CLIENT_ERROR)
    for redirect in redirects:
        if not isinstance(redirect, str):
            raise ValueError(CLIENT_ERROR)
        try:
            parsed = urlsplit(redirect)
            parsed.port
        except ValueError:
            raise ValueError(CLIENT_ERROR) from None
        if (parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError(CLIENT_ERROR)
    allowed = {'client_id', 'client_secret', 'project_id', 'auth_uri', 'token_uri',
               'auth_provider_x509_cert_url', 'redirect_uris'}
    if set(client) - allowed:
        raise ValueError(CLIENT_ERROR)
    return payload


def store_oauth_client(raw: bytes, workspace) -> Path:
    """Validate and atomically store owner-only JSON; never echo its contents."""
    payload = validate_oauth_client(raw)
    from vcoutreach.send.gmail import _write_token
    target = workspace.path('data', '.gmail_credentials.json')
    _write_token(target, json.dumps(payload))
    # An existing token belongs to the previous client until consent is repeated.
    workspace.path('data', '.gmail_token.json').unlink(missing_ok=True)
    return target


def copy_oauth_client(source: Path, workspace) -> Path:
    with Path(source).open('rb') as handle:
        return store_oauth_client(handle.read(MAX_CLIENT_BYTES + 1), workspace)


class GmailSetup:
    """Serialize client replacement and a bounded background consent flow."""

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.running = False
        self.error = False

    def import_client(self, raw):
        with self.lock:
            if self.running:
                raise ValueError('Finish the Gmail consent flow before replacing the client.')
            store_oauth_client(raw, self.app.extensions['workspace'])
            self.error = False

    def import_file(self, path):
        with Path(path).open('rb') as handle:
            self.import_client(handle.read(MAX_CLIENT_BYTES + 1))

    def connect_app_password(self, address, password):
        from vcoutreach.web.gmail.app_password import connect
        with self.lock:
            if self.running:
                raise ValueError('Finish the Gmail consent flow before connecting another account.')
            connect(address, password, self.app.extensions['workspace'])
            self.error = False

    def remove_client(self):
        with self.lock:
            if self.running:
                raise ValueError('Finish the Gmail consent flow before removing the client and token.')
            workspace = self.app.extensions['workspace']
            from vcoutreach.web.gmail.app_password import secret_path
            secret_path(workspace).unlink(missing_ok=True)
            workspace.path('data', '.gmail_token.json').unlink(missing_ok=True)
            workspace.path('data', '.gmail_credentials.json').unlink(missing_ok=True)
            self.error = False

    def connect(self):
        with self.lock:
            if self.running:
                return
            workspace = self.app.extensions['workspace']
            with workspace.path('data', '.gmail_credentials.json').open('rb') as handle:
                validate_oauth_client(handle.read(MAX_CLIENT_BYTES + 1))
            self.running, self.error = True, False
        def authorize():
            try:
                with self.app.app_context():
                    from vcoutreach.web.gmail.api import run_oauth_flow
                    run_oauth_flow()
            except Exception:
                with self.lock:
                    self.error = True
            finally:
                with self.lock:
                    self.running = False
        try:
            threading.Thread(target=authorize, name='gmail-consent', daemon=True).start()
        except Exception:
            with self.lock:
                self.running, self.error = False, True
            raise


def register_setup_routes(app, connection_state):
    from flask import abort, jsonify, request
    setup = GmailSetup(app)
    app.extensions['gmail_setup'] = setup

    def same_origin():
        source = request.headers.get('Origin') or request.headers.get('Referer', '')
        parsed = urlsplit(source)
        if parsed.scheme != request.scheme or parsed.netloc != request.host:
            abort(400)

    @app.post('/api/gmail/client')
    def gmail_client():
        same_origin()
        upload = request.files.get('oauth_client')
        if upload is None:
            return jsonify(ok=False, error=CLIENT_ERROR), 400
        try:
            setup.import_client(upload.stream.read(MAX_CLIENT_BYTES + 1))
        except (ValueError, OSError):
            return jsonify(ok=False, error=CLIENT_ERROR), 400
        return jsonify(ok=True, state=connection_state())

    @app.post('/api/gmail/connect')
    def gmail_connect():
        same_origin()
        if request.form.get('mode') == 'app_password' or 'address' in request.form or 'password' in request.form:
            from vcoutreach.web.gmail.api import GmailAuthNotReady
            try:
                setup.connect_app_password(request.form.get('address', ''), request.form.get('password', ''))
            except (ValueError, GmailAuthNotReady) as exc:
                return jsonify(ok=False, error=str(exc)), 400
            except Exception:
                return jsonify(ok=False, error='Could not save the Gmail connection. Try again.'), 500
            return jsonify(ok=True, state=connection_state())
        try:
            setup.connect()
        except Exception:
            return jsonify(ok=False, error='Choose a valid Desktop OAuth client file before connecting.'), 400
        return jsonify(ok=True, running=True), 202

    @app.post('/api/gmail/disconnect')
    def gmail_disconnect():
        same_origin()
        try:
            setup.remove_client()
        except ValueError:
            return jsonify(ok=False, error='Finish the Gmail consent flow before removing the client and token.'), 409
        except OSError:
            return jsonify(ok=False, error='Could not remove the Gmail connection. Try again.'), 500
        return jsonify(ok=True, state=connection_state())

    @app.get('/api/gmail/status')
    def gmail_status():
        from vcoutreach.web.gmail.app_password import connection_info
        with setup.lock:
            return jsonify(state=connection_state(), running=setup.running, failed=setup.error,
                           **connection_info(app.extensions['workspace']))
