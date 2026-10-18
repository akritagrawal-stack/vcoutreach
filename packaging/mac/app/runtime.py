"""Desktop runtime helpers. Importing this module never starts a GUI or server."""
from __future__ import annotations

import fcntl
import os
import socket
import stat
import threading
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode


def bootstrap_workspace(root=None):
    from vcoutreach.web.config import Workspace
    from vcoutreach.web.keys import write_env
    workspace = Workspace(root or Path.home() / 'Documents' / 'Publikreach')
    workspace.initialize()
    write_env({'SEND_MODE': 'test'}, workspace, internal=True)
    return workspace


def pick_free_port(socket_factory=socket.socket):
    """Probe an OS-selected loopback port. Callers must still handle bind races.

    The production server instead binds port zero directly, keeping ownership of
    the selected socket. A factory argument permits testing without a real bind.
    """
    with socket_factory(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class SingleInstanceLock:
    """Process lock with crash recovery. Keep the inode in place after release."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def acquire(self):
        if self.fd is not None:
            return True
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('Instance lock must be a regular file.')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                return False
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        self.fd = descriptor
        return True

    def release(self):
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError('Publikreach is already running.')
        return self

    def __exit__(self, *args):
        self.release()


@dataclass(repr=False)
class SessionCookie:
    name: str
    value: str
    csrf: str


def exchange_token(app, base_url):
    """Use the existing one-use guard in-process, without a URL on the wire.

    Return the HttpOnly session for the native cookie store. No cookie, bootstrap
    token or session secret is exposed to JavaScript, printed, or persisted here.
    """
    with app.test_client() as client:
        response = client.get('/?' + urlencode({'token': app.config['ACCESS_TOKEN']}),
                              base_url=base_url, follow_redirects=False)
        if response.status_code != 302 or response.headers.get('Location') != '/':
            raise RuntimeError('Internal session setup failed.')
        cookies = SimpleCookie()
        for header in response.headers.getlist('Set-Cookie'):
            cookies.load(header)
        name = app.config['SESSION_COOKIE_NAME']
        cookie = cookies.get(name)
        if cookie is None or not cookie['httponly'] or cookie['samesite'] != 'Strict':
            raise RuntimeError('Internal session cookie is missing its protections.')
        with client.session_transaction(base_url=base_url) as session:
            csrf = session['csrf']
        return SessionCookie(name, cookie.value, csrf)


@dataclass
class LocalServer:
    app: object
    server: object
    thread: threading.Thread
    base_url: str
    _stop_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stopped: bool = False

    def stop(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            self.server.shutdown()
            self.thread.join(timeout=5)
            self.server.server_close()
            from vcoutreach.web.gmail.scheduler import stop_scheduler
            stop_scheduler()


def start_server(workspace, *, server_factory=None):
    from vcoutreach.web import create_app
    from werkzeug.serving import WSGIRequestHandler, make_server

    class QuietHandler(WSGIRequestHandler):
        def log(self, *args, **kwargs):
            # Request URLs can contain credentials. Desktop operation is silent.
            pass

    app = create_app(workspace)
    factory = server_factory or make_server
    server = factory('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    port = server.server_port
    app.config['PORT'] = port
    thread = threading.Thread(target=server.serve_forever, name='publik-loopback', daemon=True)
    try:
        thread.start()
    except BaseException:
        server.server_close()
        raise
    return LocalServer(app, server, thread, f'http://127.0.0.1:{port}')


def self_test(root):
    """Exercise a real loopback server and bundled pages without importing a GUI."""
    import http.client
    import json
    workspace = bootstrap_workspace(root)
    with SingleInstanceLock(workspace.path('data', '.app.lock')):
        runtime = start_server(workspace)
        try:
            cookie = exchange_token(runtime.app, runtime.base_url)
            connection = http.client.HTTPConnection('127.0.0.1', runtime.server.server_port, timeout=10)
            try:
                connection.request('GET', '/healthz')
                response = connection.getresponse()
                if response.status != 200 or json.loads(response.read()) != {'ok': True}:
                    raise RuntimeError('Health check failed.')
                for path in ('/', '/crm', '/settings', '/replies', '/static/app.js', '/static/style.css'):
                    connection.request('GET', path, headers={'Cookie': f'{cookie.name}={cookie.value}'})
                    response = connection.getresponse()
                    content = response.read()
                    expected = 302 if path == '/' else 200
                    if response.status != expected or not content:
                        raise RuntimeError('Bundled page check failed.')
                connection.request('GET', '/settings')
                response = connection.getresponse()
                response.read()
                if response.status != 403:
                    raise RuntimeError('Session protection check failed.')
            finally:
                connection.close()
        finally:
            runtime.stop()
