"""Loopback authentication, constant-time CSRF and confined request handling."""
from __future__ import annotations
import hashlib
import secrets
import threading
from urllib.parse import urlsplit
from flask import request, session, redirect, jsonify, render_template, flash
from werkzeug.exceptions import HTTPException

CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'"
DECK_EMBED_PATH = '/crm/deck'
AUTH_MESSAGE = 'Open the newest link printed in the Terminal window.'
USED_LINK_MESSAGE = 'This one-time link was already used. Open the Publikreach window again for a fresh link.'

def _cookie_name(workspace) -> str:
    """One stable session cookie name per workspace, so two local instances on 127.0.0.1 do not overwrite each other."""
    if not workspace:
        return 'outreach_session'
    digest = hashlib.sha256(str(workspace).encode('utf-8')).hexdigest()[:10]
    return 'outreach_session_' + digest


DECK_UPLOAD_PATH = '/crm/deck/upload'
DECK_UPLOAD_BODY_LIMIT = 26 * 1024 * 1024
DECK_TOO_LARGE_MESSAGE = 'That file is larger than 25 MB.'
DECK_TOO_LARGE_BACK = '/crm/drafts#templateDeckPanel'
_DRAIN_CHUNK = 1024 * 1024
_DRAIN_CAP = 256 * 1024 * 1024


def install_security(app):
    token = secrets.token_urlsafe(32)
    app.config['ACCESS_TOKEN'] = token
    app.config['BOOTSTRAP_TOKEN'] = token
    app.config.update(MAX_CONTENT_LENGTH=2 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_PATH='/', SESSION_COOKIE_NAME=_cookie_name(app.config.get('WORKSPACE')))
    lock = threading.Lock()
    app.extensions['access_token_used'] = False

    def error(message, status):
        if request.path.startswith('/api/') or request.is_json:
            return jsonify(error=message), status
        return app.response_class(message, status=status, mimetype='text/plain')

    def deck_too_large():
        """An oversize deck upload from a signed-in person goes back to the Campaign page with a flash, so the
        desktop app never shows a bare text page. Anyone else keeps the plain refusal. The body is read and
        dropped first (bounded) so the browser receives the redirect instead of a reset connection."""
        if session.get('authenticated') != app.config['SESSION_NONCE']:
            return error(DECK_TOO_LARGE_MESSAGE, 413)
        remaining = request.content_length if request.content_length is not None else 0
        if remaining <= _DRAIN_CAP:
            stream = request.environ.get('wsgi.input')
            while stream is not None and remaining > 0:
                chunk = stream.read(min(_DRAIN_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        flash(DECK_TOO_LARGE_MESSAGE, 'deck_error')
        return redirect(DECK_TOO_LARGE_BACK)

    def csrf_expired():
        message = 'Your session expired or this page is out of date. Reload the page and try again.'
        if request.path.startswith('/api/') or request.is_json:
            return jsonify(error=message), 400
        back = '/crm'
        try:
            path = urlsplit(request.referrer or '').path
        except ValueError:
            path = ''
        if path.startswith('/') and not path.startswith('//') and '\\' not in path:
            back = path
        html = render_template('error.html', heading='Session expired', message=message,
                               back_href=back, back_label='Go back and reload', active_page='')
        return app.response_class(html, status=400, mimetype='text/html')

    @app.before_request
    def guard():
        if not request.headers.get('Host', '').strip():
            return error('Host is not allowed.', 403)
        host = request.host.lower()
        parsed = urlsplit('http://' + host)
        try:
            port = parsed.port
        except ValueError:
            return error('Host is not allowed.', 403)
        expected = app.config.get('PORT') or int(request.environ.get('SERVER_PORT',80))
        if (parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.username
                or parsed.password or (expected is not None and (port or (443 if request.is_secure else 80)) != expected)):
            return error('Host is not allowed.', 403)
        limit = app.config['MAX_CONTENT_LENGTH']
        if request.method == 'POST' and request.path == DECK_UPLOAD_PATH:
            # The deck upload alone may carry a 25 MB file (plus form overhead); every other route keeps the 2 MB cap.
            limit = DECK_UPLOAD_BODY_LIMIT
            request.max_content_length = limit
        if request.content_length is not None and request.content_length > limit:
            if limit == DECK_UPLOAD_BODY_LIMIT:
                return deck_too_large()
            return error('Request body exceeds 2 MB.',413)
        if request.path == '/healthz' and request.method == 'GET':
            return None
        if request.path == '/favicon.ico' and request.method in ('GET', 'HEAD'):
            # Browsers fetch the tab icon without a session on the first load; it is a fixed public asset, host check above still applies.
            return None
        supplied = request.args.get('token', '')
        if request.path == '/' and supplied:
            with lock:
                matches = secrets.compare_digest(supplied.encode('utf-8'), token.encode('utf-8'))
                if matches and app.extensions['access_token_used']:
                    return error(USED_LINK_MESSAGE, 403)
                if not matches:
                    return error(AUTH_MESSAGE, 403)
                app.extensions['access_token_used'] = True
            session.clear()
            session['authenticated'] = app.config['SESSION_NONCE']
            session['csrf'] = session['csrf_token'] = secrets.token_urlsafe(32)
            return redirect('/')
        if session.get('authenticated') != app.config['SESSION_NONCE']:
            return error(AUTH_MESSAGE, 403)
        if request.method == 'POST':
            expected_token = session.get('csrf', '')
            given = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
            if not expected_token or not secrets.compare_digest(str(given).encode('utf-8'), expected_token.encode('utf-8')):
                return csrf_expired()
            for source in ('Origin', 'Referer'):
                value = request.headers.get(source)
                if value:
                    if source == 'Origin' and value == 'null':
                        if request.headers.get('Sec-Fetch-Site') != 'same-origin':
                            return error('Cross-origin request refused.', 400)
                        continue
                    try:
                        origin = urlsplit(value)
                    except ValueError:
                        return error('Cross-origin request refused.', 400)
                    if origin.scheme != request.scheme or origin.netloc.lower() != host:
                        return error('Cross-origin request refused.', 400)
        return None

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': CSP})
        if request.path == DECK_EMBED_PATH and request.method == 'GET':
            # Only the pitch deck PDF may be framed, and only by this same origin (the inline deck preview).
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
            response.headers['Content-Security-Policy'] = CSP + "; frame-ancestors 'self'"
        response.headers.pop('Access-Control-Allow-Origin', None)
        return response

    @app.errorhandler(Exception)
    def sanitized_error(exc):
        status = exc.code if isinstance(exc, HTTPException) else 500
        messages = {400: 'Invalid request.', 403: 'Request refused.',
                    404: 'Record not found.', 405: 'Method not allowed.',
                    413: DECK_TOO_LARGE_MESSAGE if request.path == DECK_UPLOAD_PATH else 'Request body exceeds 2 MB.'}
        if status == 413 and request.path == DECK_UPLOAD_PATH and request.method == 'POST':
            return deck_too_large()
        message = messages.get(status, 'Operation failed. Check your workspace settings.')
        if status == 404 and not (request.path.startswith('/api/') or request.is_json):
            return render_template('error.html', message=message, active_page=''), status
        return error(message, status)
