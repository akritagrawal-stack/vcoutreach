"""Routes for the "Set up with AI" page and the Publik API recommendation card.

Routes (blueprint ``bp``, registered by the integrator):

* ``GET  /setup/ai``                    the setup page (snippets, Test MCP, Publik card)
* ``POST /api/mcp/self-test``           spawn the fixed MCP launch command and check it
* ``POST /api/publik/test``             one minimal probe of the saved Publik key
* ``POST /settings/publik-recommendation``  store ``pending`` or ``skipped``

Secrets never pass through here: the Publik key is saved by the existing
``/api/model-provider/key`` route. Probe and self-test answers are fixed sentences,
never provider or child-process output.

Lane A (``vcoutreach.mcp.launch``) and Lane B (``vcoutreach.web.setup_steps``) are
imported lazily with local fallbacks so this module imports cleanly without them.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets as _secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, flash, jsonify, redirect, render_template, request

bp = Blueprint('mcp_setup', __name__)

PUBLIK_KEY_URL = 'https://publikhq.com/dashboard/api'
SETTINGS_FILENAME = 'workspace-settings.json'
RECOMMENDATION_VALUES = ('pending', 'skipped')
SERVER_KEY = 'publikreach'
PROBE_WINDOW_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 10.0

MSG_SELF_START = 'Could not start the MCP command.'
MSG_SELF_TOOLS = 'The MCP server answered but the tool list was not the expected one.'
MSG_SELF_TIMEOUT = 'Timed out after 10 seconds.'
SELF_TEST_MESSAGES = (MSG_SELF_START, MSG_SELF_TOOLS, MSG_SELF_TIMEOUT)

PYTHON_MODULE_HINT = (
    'The vcoutreach command was not found next to this Python, so the snippets use '
    'python -m vcoutreach.cli mcp instead. If you installed with pip, make sure the '
    'environment that runs this app is the one your assistant should launch.'
)

_self_test_lock = threading.Lock()
_probe_lock = threading.Lock()
_probe_cache: dict = {'fingerprint': None, 'at': 0.0, 'result': None}


# ---------------------------------------------------------------------------
# Lazy access to the other lanes
# ---------------------------------------------------------------------------

def _launch_module():
    try:
        from vcoutreach.mcp import launch
        return launch
    except Exception:
        return None


def _steps_module():
    try:
        from vcoutreach.web import setup_steps
        return setup_steps
    except Exception:
        return None


def _workspace():
    from vcoutreach.web.config import get_workspace
    return get_workspace()


# ---------------------------------------------------------------------------
# Raw workspace-settings access (keys normalize_settings may not know yet)
# ---------------------------------------------------------------------------

def _settings_path() -> Path:
    return _workspace().path('data', SETTINGS_FILENAME)


def _read_setting(name: str, default: str = '') -> str:
    steps = _steps_module()
    if steps is not None and hasattr(steps, 'read_raw_setting'):
        try:
            return steps.read_raw_setting(name, default)
        except Exception:
            pass
    try:
        payload = json.loads(_settings_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default
    value = payload.get(name) if isinstance(payload, dict) else None
    return value if isinstance(value, str) else default


def _write_setting(name: str, value: str) -> None:
    steps = _steps_module()
    if steps is not None and hasattr(steps, 'write_raw_setting'):
        steps.write_raw_setting(name, value)
        return
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload[name] = value
    temporary = path.with_name(path.name + '.' + _secrets.token_hex(6) + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Publik card state
# ---------------------------------------------------------------------------

def _own_publik_key() -> str:
    """The key the user pasted (workspace .env or environment). Excludes the no-key credential file."""
    try:
        return str(_workspace().value('PUBLIK_API_KEY', '') or '').strip()
    except Exception:
        return ''


def _publik_credential() -> dict:
    """The saved no-key credential (never leaves this module), or an empty dict."""
    try:
        from vcoutreach import publik_api
        return publik_api.read_credential() or {}
    except Exception:
        return {}


def _publik_key() -> str:
    return _own_publik_key() or str(_publik_credential().get('key') or '').strip()


def publik_state() -> str:
    """``connected`` (key set), else the stored ``pending`` or ``skipped``."""
    if _publik_key():
        return 'connected'
    stored = _read_setting('publik_recommendation', 'pending').strip().lower()
    return stored if stored in RECOMMENDATION_VALUES else 'pending'


def publik_card() -> dict:
    """Everything the ``_publik_card.html`` partial needs. Never contains the key."""
    state = publik_state()
    return {
        'state': state,
        'has_key': bool(_own_publik_key()),
        'publik_api': publik_api_status(),
        'disclosure': _publik_disclosure(),
        'has_credential': bool(_publik_credential()),
        'chip_state': 'ok' if state == 'connected' else 'unset',
        'chip_word': {'connected': 'Connected', 'pending': 'Not connected', 'skipped': 'Skipped'}[state],
        'key_url': PUBLIK_KEY_URL,
    }


@bp.app_context_processor
def _inject_publik_card():
    return {'publik_card': publik_card}


def _publik_disclosure() -> str:
    from vcoutreach import publik_api
    return publik_api.DISCLOSURE


def _safe_top_up(url) -> str:
    """One https link from the provider, or an empty string."""
    text = str(url or '').strip()
    return text if text.startswith('https://') and len(text) <= 500 and not any(c.isspace() for c in text) else ''


def publik_api_status() -> dict:
    """{configured, balance_line, claim_url, claim_state, add_credit_url, cost_sentence}. Never the key."""
    from vcoutreach import publik_api
    cred = _publik_credential()
    claim_state = str(cred.get('claim_state') or 'anonymous')
    micros = cred.get('balance_micros')
    return {
        'configured': bool(cred),
        'balance_line': publik_api.balance_line(micros, claim_state) if cred and isinstance(micros, int) else '',
        'claim_url': _safe_top_up(cred.get('claim_url')),
        'claim_state': claim_state if cred else '',
        'add_credit_url': _safe_top_up(cred.get('add_credit_url')),
        'cost_sentence': str(cred.get('cost_sentence') or '') if cred else '',
    }


# ---------------------------------------------------------------------------
# Launch spec and snippets
# ---------------------------------------------------------------------------

_TOML_ESCAPES = {'\\': '\\\\', '"': '\\"', '\b': '\\b', '\t': '\\t', '\n': '\\n', '\f': '\\f', '\r': '\\r'}


def _toml_str(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def _sh_dq(value: str) -> str:
    import shlex
    if any(ord(ch) < 32 or ord(ch) == 127 or ch in '$`' for ch in value):
        return shlex.quote(value)
    return '"' + ''.join('\\' + ch if ch in '\\"' else ch for ch in value) + '"'


def _fallback_spec() -> dict:
    """The same resolution rules as Lane A, used only when launch.py cannot be imported."""
    workspace = str(Path(_workspace().root).expanduser().resolve())
    exe_text = str(sys.executable)
    hint = None
    if getattr(sys, 'frozen', False):
        command, lead, mode_arg, form = str(Path(exe_text).resolve()), [], '--mcp', 'bundle'
    else:
        sibling = Path(exe_text).parent / 'vcoutreach'
        mode_arg = 'mcp'
        if sibling.is_file():
            command, lead, form = str(sibling), [], 'console_script'
        else:
            command, lead, form, hint = exe_text, ['-m', 'vcoutreach.cli'], 'python_module', PYTHON_MODULE_HINT
    args = lead + [mode_arg, '--workspace', workspace]
    return {'executable': command, 'command': command, 'args': args, 'argv': [command] + args,
            'mode_arg': mode_arg, 'workspace': workspace, 'form': form,
            'frozen': bool(getattr(sys, 'frozen', False)), 'hint': hint}


def _resolve_spec() -> dict:
    launch = _launch_module()
    if launch is not None:
        try:
            spec = launch.mcp_launch_spec()
            if spec["argv"] and spec["executable"]:
                return dict(spec)
        except Exception:
            pass
    return _fallback_spec()


def _fallback_snippets(spec: dict) -> dict:
    command = str(spec['executable'])
    args = [str(a) for a in spec['args']]
    fixed = {'-m', 'vcoutreach.cli', 'mcp', '--mcp', '--workspace'}
    shell_bits = [_sh_dq(command)] + [a if a in fixed else _sh_dq(a) for a in args]
    return {
        'claude_desktop': json.dumps({'mcpServers': {SERVER_KEY: {'command': command, 'args': args}}},
                                     indent=2, ensure_ascii=False),
        'claude_code': 'claude mcp add %s -- %s' % (SERVER_KEY, ' '.join(shell_bits)),
        'codex': '[mcp_servers.%s]\ncommand = %s\nargs = [%s]\n' % (
            SERVER_KEY, _toml_str(command), ', '.join(_toml_str(a) for a in args)),
    }


def build_snippets() -> dict:
    """``{claude_desktop, claude_code, codex, spec}``, rendered server side and escaped."""
    spec = _resolve_spec()
    launch = _launch_module()
    snippets = None
    if launch is not None:
        try:
            rendered = launch.render_snippets(spec)
            snippets = {key: str(rendered[key]) for key in ('claude_desktop', 'claude_code', 'codex')}
        except Exception:
            snippets = None
    if snippets is None:
        snippets = _fallback_snippets(spec)
    snippets['spec'] = spec
    return snippets


# ---------------------------------------------------------------------------
# GET /setup/ai
# ---------------------------------------------------------------------------

_HOME_SHAPED = re.compile(r'(?:[A-Za-z]:)?[\\/]+(?:Users|home)[\\/]+[^\\/"\'\s,\]]+', re.IGNORECASE)
SNIPPET_KEYS = ('claude_desktop', 'claude_code', 'codex')
HOME_PLACEHOLDER = '~'


def _home_variants() -> list:
    """Spellings of the user's home folder, longest first, so the redaction cannot leave a tail behind."""
    found = set()
    for getter in (lambda: Path.home(), lambda: Path(os.path.expanduser('~'))):
        try:
            home = getter()
        except Exception:
            continue
        for candidate in (str(home), str(home.resolve())):
            if len(candidate) > 1 and candidate not in ('/', '\\'):
                found.add(candidate)
                found.add(candidate.replace('\\', '\\\\'))
                found.add(candidate.replace('\\', '/'))
    return sorted(found, key=len, reverse=True)


def display_text(text: str) -> str:
    """The snippet as shown on the page: the home folder becomes ``~`` so the page never prints it.

    The real text is served only by ``POST /api/mcp/snippet`` for the Copy buttons.
    """
    text = str(text)
    for variant in _home_variants():
        text = text.replace(variant, HOME_PLACEHOLDER)
    return _HOME_SHAPED.sub(HOME_PLACEHOLDER, text)


@bp.get('/setup/ai')
def setup_ai():
    real = build_snippets()
    spec = real['spec']
    snippets = {key: display_text(real[key]) for key in SNIPPET_KEYS}
    snippets['spec'] = {'hint': spec.get('hint')}
    last_ok = _read_setting('mcp_last_self_test_ok', '')
    return render_template(
        'setup_ai.html',
        snippets=snippets,
        launch_hint=spec.get('hint'),
        last_self_test=last_ok,
        active_page='settings',
    )


@bp.post('/api/mcp/snippet')
def mcp_snippet():
    """The real (unredacted) text of one snippet, for the Copy button. POST only, so a plain GET never prints paths."""
    key = (request.form.get('key') or '').strip()
    if key not in SNIPPET_KEYS:
        return jsonify({'ok': False, 'error': 'Unknown snippet.'}), 400
    try:
        return jsonify({'ok': True, 'text': build_snippets()[key]})
    except Exception:
        return jsonify({'ok': False, 'error': 'Could not build the snippet.'}), 500


# ---------------------------------------------------------------------------
# POST /api/mcp/self-test
# ---------------------------------------------------------------------------

def _self_test_result() -> dict:
    launch = _launch_module()
    if launch is None:
        return {'ok': False, 'message': MSG_SELF_START, 'tool_count': 0}
    try:
        spec = _resolve_spec()
        raw = launch.run_self_test(list(spec['argv']))
    except Exception:
        return {'ok': False, 'message': MSG_SELF_START, 'tool_count': 0}
    ok = bool(raw.get('ok')) if isinstance(raw, dict) else False
    try:
        count = int(raw.get('tool_count', 0)) if isinstance(raw, dict) else 0
    except (TypeError, ValueError):
        count = 0
    if ok:
        message = str(raw.get('message') or 'The MCP server started and listed %d tools.' % count)
        message = ' '.join(message.split())[:160]
    else:
        message = raw.get('message') if isinstance(raw, dict) else None
        message = message if message in SELF_TEST_MESSAGES else MSG_SELF_START
    return {'ok': ok, 'message': message, 'tool_count': count}


@bp.post('/api/mcp/self-test')
def mcp_self_test():
    with _self_test_lock:
        result = _self_test_result()
    if result['ok']:
        try:
            _write_setting('mcp_last_self_test_ok', datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        except Exception:
            pass
    return jsonify(result)


# ---------------------------------------------------------------------------
# POST /api/publik/test
# ---------------------------------------------------------------------------

PROBE_NO_KEY = 'No Publik API key is saved. Paste one above and choose Save key.'
PROBE_OK = 'Connected. Publik answered one small test request (a real, billed call).'
PROBE_AUTH = 'Publik rejected the key. Check it at ' + PUBLIK_KEY_URL + '.'
PROBE_CREDIT = 'Publik reports no remaining credit on this account. Add credit at ' + PUBLIK_KEY_URL + '.'
PROBE_RATE = 'Publik is rate limiting requests right now. Wait a minute, then test again.'
PROBE_TRANSPORT = 'Could not reach the Publik host. Check your connection and the configured base URL.'
PROBE_RESPONSE = 'Publik answered, but the reply could not be read.'
PROBE_CONFIG = 'The Publik settings are not valid. Check the model and base URL in your .env file.'
PROBE_OTHER = 'The Publik test failed.'


def _run_probe(key: str) -> dict:
    """One minimal paid request. Fixed messages only; no provider text is echoed."""
    try:
        from vcoutreach.config import load_settings
        from vcoutreach.providers.publik import PublikProvider
        from vcoutreach.types import (
            Contact, InsufficientFacts, PersonalizationRequest, ProviderAuthError,
            ProviderConfigError, ProviderCreditError, ProviderRateLimitError,
            ProviderResponseError, ProviderTransportError, SenderProfile,
        )
    except Exception:
        return {'ok': False, 'message': PROBE_OTHER}
    try:
        ws = _workspace()
        settings = load_settings(env=ws.env(), dotenv_path=ws.env_path)
        settings = dataclasses.replace(
            settings, publik_max_retries=0,
            publik_timeout_seconds=min(float(settings.publik_timeout_seconds or PROBE_TIMEOUT_SECONDS),
                                       PROBE_TIMEOUT_SECONDS))
        provider = PublikProvider(settings)
        contact = Contact(id='probe', name='Test Contact', email='probe@example.com', org='Example Org',
                          role='Editor', facts=('Publishes a weekly newsletter about early-stage startups.',))
        sender = SenderProfile(name='Publikreach', org='Publikreach')
        provider.personalize(PersonalizationRequest(contact=contact, sender=sender))
        return {'ok': True, 'message': PROBE_OK}
    except InsufficientFacts:
        return {'ok': True, 'message': PROBE_OK}
    except ProviderAuthError:
        return {'ok': False, 'message': PROBE_AUTH}
    except ProviderCreditError as exc:
        # A 402 shows the fixed message plus exactly one top-up link (when the provider sent one).
        return {'ok': False, 'message': PROBE_CREDIT, 'top_up_url': _safe_top_up(getattr(exc, 'top_up_url', ''))}
    except ProviderRateLimitError:
        return {'ok': False, 'message': PROBE_RATE}
    except ProviderTransportError:
        return {'ok': False, 'message': PROBE_TRANSPORT}
    except ProviderResponseError:
        return {'ok': False, 'message': PROBE_RESPONSE}
    except ProviderConfigError:
        return {'ok': False, 'message': PROBE_CONFIG}
    except Exception:
        return {'ok': False, 'message': PROBE_OTHER}


@bp.post('/api/publik/test')
def publik_test():
    key = _publik_key()
    if not key:
        return jsonify({'ok': False, 'message': PROBE_NO_KEY})
    fingerprint = hashlib.sha256(key.encode('utf-8')).hexdigest()
    with _probe_lock:
        now = time.monotonic()
        cached = _probe_cache['result']
        if (cached is not None and _probe_cache['fingerprint'] == fingerprint
                and now - _probe_cache['at'] < PROBE_WINDOW_SECONDS):
            return jsonify({'ok': cached['ok'], 'message': cached['message'],
                            'top_up_url': cached.get('top_up_url', ''), 'cached': True})
        result = _run_probe(key)
        _probe_cache.update(fingerprint=fingerprint, at=time.monotonic(), result=result)
    return jsonify({'ok': result['ok'], 'message': result['message'], 'top_up_url': result.get('top_up_url', '')})


# ---------------------------------------------------------------------------
# GET /api/publik/status and POST /api/publik/provision (no-key publik API)
# ---------------------------------------------------------------------------

PROVISION_FALLBACK = 'Or paste your own publik API key below.'
PROVISION_NETWORK = 'Could not reach publik to set up this computer.'


@bp.get('/api/publik/status')
def publik_status():
    status = publik_api_status()
    return jsonify({k: status[k] for k in ('configured', 'balance_line', 'claim_url', 'claim_state',
                                           'add_credit_url', 'cost_sentence')})


@bp.post('/api/publik/provision')
def publik_provision():
    """Set up publik API for this install. The button is the user's consent to DISCLOSURE."""
    from vcoutreach import publik_api
    try:
        publik_api.provision()
    except RuntimeError as exc:
        return jsonify({'ok': False, 'message': str(exc), 'hint': PROVISION_FALLBACK}), 200
    except Exception:
        return jsonify({'ok': False, 'message': PROVISION_NETWORK, 'hint': PROVISION_FALLBACK}), 200
    _probe_cache.update(fingerprint=None, at=0.0, result=None)
    return jsonify(dict(publik_api_status(), ok=True))


# ---------------------------------------------------------------------------
# POST /settings/publik-recommendation
# ---------------------------------------------------------------------------

def _wants_json() -> bool:
    return 'application/json' in request.headers.get('Accept', '')


def _safe_back(default: str) -> str:
    """A same-origin path (no scheme, no host) from ``next`` or the Referer, else ``default``."""
    for candidate in (request.form.get('next', ''), request.headers.get('Referer', '')):
        if not candidate:
            continue
        parts = urlsplit(candidate)
        if parts.scheme or parts.netloc:
            if parts.netloc != request.host:
                continue
        path = parts.path or ''
        if not path.startswith('/') or path.startswith('//') or '\\' in path or '\n' in path or '\r' in path:
            continue
        query = ('?' + parts.query) if parts.query else ''
        return path + query
    return default


@bp.post('/settings/publik-recommendation')
def publik_recommendation():
    state = (request.form.get('state') or '').strip().lower()
    if state not in RECOMMENDATION_VALUES:
        if _wants_json():
            return jsonify({'ok': False, 'error': 'state must be pending or skipped'}), 400
        flash('Choose pending or skipped.', 'error')
        return redirect(_safe_back('/settings'))
    try:
        _write_setting('publik_recommendation', state)
    except Exception:
        if _wants_json():
            return jsonify({'ok': False, 'error': 'Could not save the choice.'}), 500
        flash('Could not save the choice.', 'error')
        return redirect(_safe_back('/settings'))
    if _wants_json():
        return jsonify({'ok': True, 'state': publik_state()})
    flash('Publik API skipped. You can undo this from Settings.' if state == 'skipped'
          else 'Publik API recommendation restored.', 'success')
    return redirect(_safe_back('/settings'))


# ---------------------------------------------------------------------------
# POST /crm/setup/publik  ("Let Publik do it for you")
# ---------------------------------------------------------------------------

SETUP_NO_KEY = 'Add your Publik API key to let Publik set this up.'
SETUP_OK = 'Publik drafted your email template and saved it. Read it below and edit anything you like.'
SETUP_AUTH = 'Publik rejected the key. Check it on the Set up with AI page.'
SETUP_CREDIT = 'Publik reports no remaining credit on this account. Add credit, then try again.'
SETUP_RATE = 'Publik is busy right now. Wait a minute, then try again.'
SETUP_TRANSPORT = "We couldn't reach Publik. Check your connection and try again."
SETUP_RESPONSE = 'Publik answered, but the reply was not usable. Try again.'
SETUP_CONFIG = 'The Publik settings are not valid. Check the model and base URL in your .env file.'
SETUP_OTHER = "Publik couldn't set this up. Try again in a moment."
SETUP_MAX_BLURB = 3000
SETUP_NO_KEY_PATH = '/setup/ai'

_SETUP_FACT_KEYS = (
    ('company_name', 'Company'), ('product_name', 'Product'), ('one_liner', 'One line'),
    ('problem_statement', 'Problem'), ('solution_statement', 'Solution'),
    ('traction_line', 'Traction'), ('founder_line', 'Founder'), ('ask_line', 'Ask'),
    ('call_to_action', 'Call to action'),
)


def _setup_facts(settings: dict) -> str:
    lines = []
    for key, label in _SETUP_FACT_KEYS:
        value = str(settings.get(key) or '').strip()
        if value:
            lines.append(f'{label}: {value[:400]}')
    return '\n'.join(lines)


def _setup_messages(settings: dict) -> list:
    job = str(settings.get('campaign_kind') or '').strip() == 'job_search'
    audience = 'a hiring manager' if job else 'an investor'
    attachment = 'resume' if job else 'pitch deck'
    system = (
        f'You write the short email template for a cold outreach campaign sent to {audience}. '
        f'Write 3 to 5 plain sentences the sender can reuse, in the first person, mentioning that the {attachment} is attached. '
        'Use only the facts you are given. Invent nothing: no numbers, names, customers or claims. '
        'Never use an em dash. No greeting, no sign-off, no subject line, no markdown. Reply with the template text only.')
    facts = _setup_facts(settings) or 'No details have been added yet. Write a short neutral template with no specific claims.'
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': 'Facts:\n' + facts}]


def _clean_blurb(text: str) -> str:
    text = re.sub(r'\s*[\u2014\u2013]\s*', ', ', str(text or ''))
    text = re.sub(r'[ \t]+', ' ', text).strip()
    return text[:SETUP_MAX_BLURB].rstrip()


def _run_setup(settings_dict: dict, transport=None) -> dict:
    """Draft the blurb with one Publik call. Returns {'ok', 'message', 'blurb'}; no provider text is echoed."""
    try:
        from vcoutreach.config import load_settings
        from vcoutreach.providers.http import USER_AGENT
        from vcoutreach.providers.openai_compat import extract_chat_completion_text
        from vcoutreach.providers.publik import PublikProvider
        from vcoutreach.types import (
            ProviderAuthError, ProviderConfigError, ProviderCreditError, ProviderRateLimitError,
            ProviderResponseError, ProviderTransportError,
        )
    except Exception:
        return {'ok': False, 'message': SETUP_OTHER, 'blurb': ''}
    try:
        ws = _workspace()
        settings = load_settings(env=ws.env(), dotenv_path=ws.env_path)
        settings = dataclasses.replace(
            settings, publik_max_retries=0,
            publik_timeout_seconds=min(float(settings.publik_timeout_seconds or PROBE_TIMEOUT_SECONDS) * 3,
                                       PROBE_TIMEOUT_SECONDS * 3))
        provider = PublikProvider(settings, transport=transport)
        headers = {
            'Authorization': 'Bearer ' + provider._api_key, 'Content-Type': 'application/json',
            'Accept': 'application/json', 'User-Agent': USER_AGENT,
        }
        payload = {'model': provider._model, 'messages': _setup_messages(settings_dict),
                   'temperature': 0.4, 'max_tokens': 500}
        response = provider._post(provider._url, headers, payload)
        provider._raise_for_status(response)
        blurb = _clean_blurb(extract_chat_completion_text(response.body, provider.label))
        if not blurb:
            return {'ok': False, 'message': SETUP_RESPONSE, 'blurb': ''}
        return {'ok': True, 'message': SETUP_OK, 'blurb': blurb}
    except ProviderAuthError:
        return {'ok': False, 'message': SETUP_AUTH, 'blurb': ''}
    except ProviderCreditError:
        return {'ok': False, 'message': SETUP_CREDIT, 'blurb': ''}
    except ProviderRateLimitError:
        return {'ok': False, 'message': SETUP_RATE, 'blurb': ''}
    except ProviderTransportError:
        return {'ok': False, 'message': SETUP_TRANSPORT, 'blurb': ''}
    except ProviderResponseError:
        return {'ok': False, 'message': SETUP_RESPONSE, 'blurb': ''}
    except ProviderConfigError:
        return {'ok': False, 'message': SETUP_CONFIG, 'blurb': ''}
    except Exception:
        return {'ok': False, 'message': SETUP_OTHER, 'blurb': ''}


def _save_blurb(blurb: str) -> None:
    from vcoutreach.web.server.workspace_settings import _persist_settings, load_settings, normalize_settings
    current = load_settings()
    current['outreach_blurb_template'] = blurb
    _persist_settings(normalize_settings(current))


@bp.post('/crm/setup/publik')
def publik_setup():
    """Let Publik draft the email template. The transport can be injected via app.extensions['publik_transport']."""
    from flask import current_app
    if not _publik_key():
        if _wants_json():
            return jsonify({'ok': False, 'message': SETUP_NO_KEY, 'redirect': SETUP_NO_KEY_PATH}), 200
        flash(SETUP_NO_KEY, 'error')
        return redirect(SETUP_NO_KEY_PATH)
    back = _safe_back('/crm/drafts')
    try:
        from vcoutreach.web.server.workspace_settings import load_settings as load_workspace_settings
        facts = load_workspace_settings()
    except Exception:
        facts = {}
    result = _run_setup(facts, transport=current_app.extensions.get('publik_transport'))
    if result['ok']:
        try:
            _save_blurb(result['blurb'])
        except Exception:
            result = {'ok': False, 'message': 'Publik drafted it, but we could not save it. Try again.', 'blurb': ''}
    if _wants_json():
        return jsonify({'ok': result['ok'], 'message': result['message']})
    flash(result['message'], 'success' if result['ok'] else 'error')
    return redirect(back)
