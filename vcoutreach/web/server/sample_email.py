"""Pure, escaped-by-template preview using editable workspace settings."""
import re

from vcoutreach.web.server.example_pitch import EXAMPLE_DECK_LABEL
from vcoutreach.web.server.workspace_settings import blurb_for_kind, effective_campaign_kind, normalize_settings

_PLACEHOLDER = re.compile(r'\{([A-Za-z][A-Za-z _]*)\}')
_HINTS = {
    'hook': '[Add a verified contact fact.]',
    'founder_line': '[Add your founder line.]',
    'traction_line': '[Add your traction line.]',
    'problem_statement': '[Add the core problem.]',
    'solution_statement': '[Add your solution.]',
    'ask_line': '[Add your ask.]',
    'operator_name': '[Your name]',
    'operator_role': '[Your role]',
    'company_name': '[Your company]',
}
_ALIASES = {'one_verified_sentence': 'hook', 'company_description': 'one_liner',
            'ask': 'ask_line', 'sender_name': 'operator_name'}


def _values(settings: dict) -> dict[str, str]:
    values = {key: str(value) for key, value in settings.items() if isinstance(value, (str, int))}
    values['first_name'] = 'Contact'
    values['hook'] = _HINTS['hook']
    return values


def _fill(body: str, values: dict[str, str]) -> str:
    def replace(match):
        key = match.group(1).strip().lower().replace(' ', '_')
        key = _ALIASES.get(key, key)
        if key not in values:
            return match.group(0)
        return values[key] or _HINTS.get(key, '')
    return _PLACEHOLDER.sub(replace, body)


def build_sample_email(payload: dict | None) -> dict[str, object]:
    settings = normalize_settings(payload)
    body = _fill(blurb_for_kind(settings['outreach_blurb_template'], effective_campaign_kind(settings)), _values(settings))
    body = re.sub(r'[ \t]{2,}', ' ', body)
    attachment = settings['pitch_deck_label']
    if effective_campaign_kind(settings) == 'job_search' and attachment in ('', EXAMPLE_DECK_LABEL):
        attachment = 'Resume'
    return {'subject': settings['email_subject_rule'], 'attachment': attachment,
            'from_label': settings['operator_name'] or 'Sender', 'to_label': 'Contact / example.com',
            'website': settings['website'], 'paragraphs': [p.strip() for p in body.split('\n\n') if p.strip()],
            'source': 'unsaved_form_preview', 'delivery_authority': False}
