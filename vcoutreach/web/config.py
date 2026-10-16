"""Workspace-scoped paths and runtime settings for Publik Signal."""
from __future__ import annotations
import json
import os
from pathlib import Path
from dataclasses import dataclass
from contextvars import ContextVar

@dataclass
class Workspace:
    root: Path

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts):
        candidate = self.root.joinpath(*map(str, parts))
        if not candidate.resolve().is_relative_to(self.root):
            raise ValueError('File must stay inside the workspace.')
        return candidate

    @property
    def db_path(self):
        return self.path('data', 'outreach.db')

    @property
    def drafts_dir(self):
        return self.path('drafts')

    @property
    def env_path(self):
        return self.path('.env')

    def env(self):
        from vcoutreach.config import parse_dotenv
        try:
            return parse_dotenv(self.env_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}

    def value(self, name, default=''):
        return self.env().get(name, os.environ.get(name, default))

    def read_json(self, name, default=None):
        try:
            return json.loads(self.path(name).read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            return default

    def initialize(self):
        for name in ('data', 'outputs', 'drafts'):
            self.path(name).mkdir(parents=True, exist_ok=True)

_current = ContextVar('outreach_workspace', default=None)

def get_workspace():
    try:
        from flask import current_app, has_app_context
        if has_app_context() and 'workspace' in current_app.extensions:
            return current_app.extensions['workspace']
    except ImportError:
        pass
    current = _current.get()
    if current is None:
        current = Workspace(Path(os.environ.get('OUTREACH_HOME') or Path.cwd()))
        _current.set(current)
    return current

def configure_workspace(workspace):
    value = workspace if isinstance(workspace, Workspace) else Workspace(Path(workspace))
    _current.set(value)
    return value

class WorkspacePath(os.PathLike):
    """Compatibility path resolved against the current app, never an owner machine."""
    def __init__(self, *parts):
        self.parts = parts
    def resolve(self):
        return get_workspace().path(*self.parts).resolve()
    def __fspath__(self):
        return str(get_workspace().path(*self.parts))
    def __str__(self):
        return self.__fspath__()
    def __truediv__(self, value):
        return WorkspacePath(*self.parts, str(value))
    def with_suffix(self, suffix):
        return get_workspace().path(get_workspace().path(*self.parts).with_suffix(suffix))
    def glob(self, pattern):
        return (get_workspace().path(path) for path in get_workspace().path(*self.parts).glob(pattern))
    def rglob(self, pattern):
        return (get_workspace().path(path) for path in get_workspace().path(*self.parts).rglob(pattern))
    def __getattr__(self, name):
        return getattr(get_workspace().path(*self.parts), name)

PROJECT_ROOT = WorkspacePath()
DB_PATH = WorkspacePath('data', 'outreach.db')
GMAIL_CREDS_PATH = WorkspacePath('data', '.gmail_credentials.json')
GMAIL_TOKEN_PATH = WorkspacePath('data', '.gmail_token.json')
GCP_SA_PATH = WorkspacePath('data', '.gcp_service_account.json')
BOILERPLATE_PATH = WorkspacePath('data', 'boilerplate.json')
SCHEMA_PATH = Path(__file__).parent / 'schema.sql'
DEFAULT_SUBJECT = 'A quick introduction'
from vcoutreach.config import Settings
DAILY_SEND_CAP = Settings().daily_send_cap

def send_mode():
    return 'live' if get_workspace().value('SEND_MODE', 'test').strip().lower() == 'live' else 'test'

def daily_send_cap():
    raw = get_workspace().value('DAILY_SEND_CAP', str(DAILY_SEND_CAP))
    cap = int(raw)
    if cap < 0:
        raise ValueError('DAILY_SEND_CAP must be zero or greater.')
    return cap
SENDER_LABEL = 'sender'


def __getattr__(name):
    defaults = {'GCP_PROJECT':'','GCP_LOCATION':'us-central1','BRAVE_API_KEY':'',
                'SEARXNG_URL':'http://localhost:8080','TRACKER_PUBLIC_URL':'',
                'SEND_MODE':'test','TEST_RECIPIENT_EMAIL':'','FLASK_SECRET_KEY':''}
    if name == 'SEND_MODE':
        return send_mode()
    if name in defaults:
        return get_workspace().value(name,defaults[name])
    if name in {'VERTEX_STRICT','ENABLE_BRAVE_FALLBACK'}:
        return get_workspace().value(name,'0')=='1'
    if name=='PITCH_DECK_PATH':
        raw=get_workspace().value(name)
        return get_workspace().path(raw) if raw else None
    raise AttributeError(name)
