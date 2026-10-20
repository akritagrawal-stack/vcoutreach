"""Rescue model candidates checked against current identity and verification."""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from web_helpers import ADA, make_web, seed_contacts, web_env_isolation
from web_gap_helpers import partner_row_id


@pytest.mark.parametrize('candidate,deliverable,found', [
    ('better@example.com', True, True),
    (ADA['email'], True, False),
    ('bounced@example.com', False, False),
])
def test_rescue_candidate_must_be_new_and_deliverable(make_web, monkeypatch, candidate, deliverable, found):
    from vcoutreach.web.enrichment import haiku_rescue
    from vcoutreach.web.enrichment import web_search, firm_website
    monkeypatch.setattr(web_search, 'search', lambda *a, **k: [])
    monkeypatch.setattr(firm_website, 'enrich_firm', lambda *a, **k: {})
    w = make_web({'ANTHROPIC_API_KEY': 'fake-model-key', 'EMAILABLE_API_KEY': 'fake-verifier-key'})
    seed_contacts(w, [ADA])
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        output = {'email': candidate, 'found': True, 'source': 'https://example.com/team',
                  'confidence': 0.95, 'alternates': []}
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(output))],
                               stop_reason='end_turn', usage=SimpleNamespace(input_tokens=100, output_tokens=30))
    module = ModuleType('anthropic')
    module.Anthropic = lambda **kwargs: SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, 'anthropic', module)
    from vcoutreach.web.enrichment import emailable_validator
    verification_calls = []
    def verify(self, email):
        verification_calls.append(email)
        return {'email': email, 'state': 'deliverable' if deliverable else 'undeliverable',
                'score': 99 if deliverable else 0, 'reason': 'accepted_email' if deliverable else 'rejected_email',
                'internal_status': 'valid' if deliverable else 'invalid', 'confidence': 0.99,
                'error': None, 'free_email': False, 'role_email': False, 'accept_all': False,
                'did_you_mean': None, 'credits_remaining': 100}
    monkeypatch.setattr(emailable_validator.EmailableValidator, 'verify', verify)
    with w.app.app_context():
        result = haiku_rescue.rescue_partner_email(partner_row_id(w, 'ada'))
    assert calls, result
    if not deliverable:
        assert verification_calls == [candidate]
        assert result['verified_state'] == 'invalid', result
    assert result['found'] is found, result
    if found:
        assert result['email'] == candidate
        assert result['confidence'] == pytest.approx(0.99), result
        assert verification_calls == [candidate]
    elif not deliverable:
        assert not result['confidence'], result
        assert verification_calls == [candidate]
