"""Assemble user-provided email copy with escaped HTML."""
from html import escape
from vcoutreach.email_copy import clean_opener, safe_greeting_name, unique_paragraphs
from vcoutreach.web.config import get_workspace

def _match_conflict_firm(recipient_firm: str | None) -> str | None:
    return (recipient_firm or '').strip() or None

def _build_social_proof_line(boilerplate: dict, recipient_firm: str | None) -> tuple[str, bool, list[str]]:
    firms = list(boilerplate.get('social_proof_firms', []))
    kept = [firm for firm in firms if firm.casefold() != (recipient_firm or '').strip().casefold()]
    template = boilerplate.get('social_proof_template', '')
    return (template.replace('{firms}', ', '.join(kept)) if kept and template else '', len(kept) != len(firms), kept)

def assemble_email(hook: str, first_name: str, boilerplate: dict, subject: str | None = None,
                   recipient_firm: str | None = None) -> dict:
    sender = get_workspace().read_json('sender.json', {}) or {}
    signatures = boilerplate.get('signatures', {})
    signature = signatures.get('sender') or next(iter(signatures.values()), '')
    social, review, firms = _build_social_proof_line(boilerplate, recipient_firm)
    first_name = safe_greeting_name(first_name)
    paragraphs = [f'Hi {first_name or "there"},', clean_opener(hook, first_name), boilerplate.get('intro', ''),
        boilerplate.get('product_para') or boilerplate.get('value_para', ''),
        boilerplate.get('traction_para') or boilerplate.get('proof_para', ''), social,
        boilerplate.get('cta', ''), signature]
    body = '\n\n'.join(unique_paragraphs(paragraphs))
    return {'subject': subject or sender.get('first_touch_subject', '').replace('{first_name}', first_name or 'there') or 'A quick introduction',
            'body_text': body, 'body_html': ''.join('<p>' + escape(p).replace('\n', '<br>') + '</p>' for p in body.split('\n\n')),
            'social_proof_manual_review': review, 'social_proof_firms': firms}
