"""Write-only allowlisted workspace credentials, with atomic private storage."""
from __future__ import annotations
import os
import re
import secrets
from vcoutreach.web.config import get_workspace

KEY_NAMES = frozenset({
    'PUBLIK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_BASE_URL',
    'OPENAI_MODEL', 'ANTHROPIC_MODEL', 'GEMINI_API_KEY', 'GOOGLE_API_KEY',
    'OPENROUTER_API_KEY', 'APOLLO_API_KEY', 'ENROW_API_KEY', 'BRAVE_API_KEY',
    'EMAILABLE_API_KEY', 'CALENDLY_API_TOKEN', 'TRACKING_API_TOKEN',
    'TRACKING_WORKER_URL', 'TRACKER_PUBLIC_URL', 'TRACKING_WORKER_TOKEN',
    'CLOUDFLARE_TRACKING_URL', 'CLOUDFLARE_TRACKING_TOKEN', 'TRACKING_ORIGIN', 'SENDER_EMAIL',
    'TEST_RECIPIENT_EMAIL', 'DAILY_SEND_CAP', 'PITCHBOOK_ENABLED',
})
_BAD = re.compile(r"[\s\"'#\$`();\\]|[\x00-\x1f\x7f]")

def validate_worker_url(value):
    from urllib.parse import urlsplit
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}):
        raise ValueError('Tracking Worker URL must be an HTTPS URL without credentials or query.')
    try:
        parsed.port
    except ValueError:
        raise ValueError('Tracking Worker URL is invalid.') from None
    return value.rstrip('/')

def write_env(values, workspace=None, *, internal=False):
    workspace = workspace or get_workspace()
    allowed = KEY_NAMES | ({'FLASK_SECRET_KEY', 'SEND_MODE'} if internal else set())
    for key, value in values.items():
        if key not in allowed:
            raise ValueError('Unsupported setting name.')
        if key == 'TEST_RECIPIENT_EMAIL' and value:
            from vcoutreach.contacts import _EMAIL_RE
            if not isinstance(value, str) or not _EMAIL_RE.fullmatch(value):
                raise ValueError('TEST_RECIPIENT_EMAIL must be a single valid email address.')
        if not isinstance(value, str) or _BAD.search(value):
            raise ValueError(f'{key} contains unsupported characters.')
        if key in {'TRACKING_WORKER_URL', 'TRACKER_PUBLIC_URL', 'CLOUDFLARE_TRACKING_URL', 'TRACKING_ORIGIN'} and value:
            validate_worker_url(value)
        if key == 'DAILY_SEND_CAP' and (not value.isdigit() or not 0 < int(value) <= 10000):
            raise ValueError('DAILY_SEND_CAP must be between 1 and 10000.')
    path = workspace.env_path
    try:
        lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    remaining = dict(values)
    output = []
    written = set()
    for line in lines:
        name = line.strip().removeprefix('export ').partition('=')[0].strip()
        if name in values:
            if name not in written:
                output.append(f'{name}={values[name]}\n')
                written.add(name)
                remaining.pop(name, None)
        else:
            output.append(line if line.endswith('\n') else line + '\n')
    output.extend(f'{key}={value}\n' for key, value in remaining.items())
    temporary = workspace.path('.env.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(''.join(output))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)

def key_status(workspace=None):
    workspace = workspace or get_workspace()
    return {key: bool(workspace.value(key)) for key in sorted(KEY_NAMES)}
