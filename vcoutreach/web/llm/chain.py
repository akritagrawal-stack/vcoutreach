"""Lazy Gemini API, optional Vertex, and Anthropic fallback chain."""
from __future__ import annotations
import json
import os
import re
from vcoutreach.web.vendor import setting_value, data_path

_VERTEX_MODEL_PRIMARY = 'gemini-2.5-flash-lite'
_VERTEX_MODEL_FALLBACK = 'gemini-2.5-flash'
_VERTEX_MODEL_LABEL = 'gemini-2.5-flash-lite-vertex'
_CLAUDE_HAIKU = 'claude-haiku-4-5-20251001'
_CLAUDE_SONNET = 'claude-sonnet-4-6'


class ChainError(Exception):
    """A content-free provider failure."""


def _bootstrap_gcp_auth() -> None:
    if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        return
    raw = setting_value('GCP_SA_PATH', 'data/.gcp_service_account.json')
    path = data_path(raw)
    if path.is_file():
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(path)


def _strip_json_fence(text: str) -> str:
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r'(\{.*\})', text, re.DOTALL)
    return match.group(1) if match else text


def _call_gemini(prompt: str, *, vertex: bool) -> tuple[str, str]:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ChainError('Gemini needs google-genai. Run: pip install -e ".[integrations]"') from exc
    try:
        if vertex:
            project = setting_value('GOOGLE_CLOUD_PROJECT') or setting_value('GCP_PROJECT')
            location = setting_value('GCP_LOCATION', 'us-central1')
            if not re.fullmatch(r'[a-z][a-z0-9-]{4,62}', project) or not re.fullmatch(r'[a-z0-9-]+', location):
                raise ChainError('Configure a valid Vertex project and location')
            _bootstrap_gcp_auth()
            client = genai.Client(vertexai=True, project=project, location=location, http_options=types.HttpOptions(api_version='v1', base_url=('https://aiplatform.googleapis.com' if location == 'global' else 'https://' + location + '-aiplatform.googleapis.com'), timeout=30000))
        else:
            from vcoutreach.web.server.runtime_secrets import load_gemini_key
            key = load_gemini_key()
            if not key:
                raise ChainError('GEMINI_API_KEY is not configured')
            client = genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(base_url='https://generativelanguage.googleapis.com', timeout=30000))
        model = setting_value('GEMINI_MODEL', _VERTEX_MODEL_PRIMARY)
        if not re.fullmatch(r'[A-Za-z0-9._-]+', model):
            raise ChainError('Gemini model is invalid')
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=types.GenerateContentConfig(response_mime_type='application/json', max_output_tokens=1024))
            text = response.text or ''
        finally:
            close = getattr(client, 'close', None)
            if close:
                close()
        return text.strip(), model + ('-vertex' if vertex else '-api')
    except ChainError:
        raise
    except Exception as exc:
        raise ChainError('Gemini request failed. Check credentials, model access and quota.') from exc


def _call_vertex_gemini(prompt: str) -> tuple[str, str]:
    return _call_gemini(prompt, vertex=True)


def _call_claude(prompt: str, model: str) -> tuple[str, str]:
    try:
        import anthropic
    except ImportError as exc:
        raise ChainError('Claude needs anthropic. Run: pip install -e ".[integrations]"') from exc
    from vcoutreach.web.server.runtime_secrets import load_provider_key
    key = load_provider_key('anthropic')
    if not key:
        raise ChainError('ANTHROPIC_API_KEY is not configured')
    try:
        with anthropic.Anthropic(api_key=key, base_url='https://api.anthropic.com', timeout=30, max_retries=0) as client:
            response = client.messages.create(model=model, max_tokens=1024, system='Output only the requested valid JSON object.', messages=[{'role': 'user', 'content': prompt}])
            raw = ''.join(getattr(block, 'text', '') for block in response.content)
        return raw.strip(), model
    except Exception as exc:
        raise ChainError('Claude request failed. Check credentials, model access and quota.') from exc


def call_llm(prompt: str, json_mode: bool = True, model_pref: str = 'auto') -> dict | str:
    if model_pref not in {'auto', 'vertex', 'gemini', 'anthropic', 'haiku', 'sonnet'}:
        raise ChainError('Unknown model provider')
    strict = setting_value('VERTEX_STRICT') == '1'
    if model_pref == 'auto':
        primary = 'vertex' if strict or setting_value('GOOGLE_GENAI_USE_VERTEXAI') == '1' else 'gemini'
        choices = [primary] if strict else [primary, 'haiku', 'sonnet']
    else:
        choices = [model_pref]
    errors = []
    for choice in choices:
        try:
            if choice in {'gemini', 'vertex'}:
                raw, used = _call_gemini(prompt, vertex=choice == 'vertex')
            else:
                model = setting_value('ANTHROPIC_MODEL') or (_CLAUDE_SONNET if choice == 'sonnet' else _CLAUDE_HAIKU)
                raw, used = _call_claude(prompt, model)
            if not json_mode:
                return raw
            try:
                result = json.loads(_strip_json_fence(raw))
            except (ValueError, TypeError) as exc:
                raise ChainError('Model returned invalid JSON') from exc
            if not isinstance(result, dict):
                raise ChainError('Model returned an invalid JSON object')
            result['_model_used'] = used
            return result
        except ChainError as exc:
            errors.append(str(exc))
    raise ChainError(' '.join(dict.fromkeys(errors)))


def _reload_env() -> None:
    """Compatibility helper. Settings are read at call time without changing env."""
