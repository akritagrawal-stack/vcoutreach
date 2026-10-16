"""Pure, escaped-by-template preview using editable workspace settings."""
from vcoutreach.web.server.workspace_settings import normalize_settings

def build_sample_email(payload: dict | None) -> dict[str, object]:
    settings = normalize_settings(payload)
    body = settings['outreach_blurb_template']
    values = {'First name': 'Contact', 'One verified sentence': '[Add a verified contact fact.]',
              'Company description': settings['one_liner'], 'Ask': settings['ask_line'] or settings['call_to_action'],
              'Sender name': settings['operator_name']}
    for name, value in values.items():
        body = body.replace('{' + name + '}', value)
    return {'subject': settings['email_subject_rule'], 'attachment': settings['pitch_deck_label'],
            'from_label': settings['operator_name'] or 'Sender', 'to_label': 'Contact / example.com',
            'website': settings['website'], 'paragraphs': body.split('\n\n'),
            'source': 'unsaved_form_preview', 'delivery_authority': False}
