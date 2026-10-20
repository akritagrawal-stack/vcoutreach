"""Offline discovery budgeting and classification through public entry points."""
import json
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

from web_helpers import make_web, web_env_isolation


@pytest.fixture
def discovery_wire(make_web, monkeypatch):
    from vcoutreach.web.enrichment import discovery, pitchbook_api
    calls = []
    monkeypatch.setattr(time, 'sleep', lambda _: None)
    class Session:
        def __init__(self, *args, **kwargs):
            self.cookies = SimpleNamespace(set=lambda *a, **k: None)
            self.headers = {}
        def get(self, url, *args, **kwargs):
            calls.append(url)
            return SimpleNamespace(status_code=200, json=lambda: {}, text='{}', raise_for_status=lambda: None)
        def post(self, url, *args, **kwargs):
            return self.get(url, *args, **kwargs)
        def close(self):
            pass
    module = ModuleType('curl_cffi')
    module.requests = SimpleNamespace(Session=Session)
    monkeypatch.setitem(sys.modules, 'curl_cffi', module)
    monkeypatch.setattr(pitchbook_api, '_PITCHBOOK_BACKEND', 'curl_cffi')
    monkeypatch.setattr(discovery, 'KNOWN_COMP_PBIDS', {'Lumen Notes': '11111-11', 'Research Atlas': '22222-22'})
    monkeypatch.setattr(discovery, 'SEARCH_COMPS', [])
    w = make_web()
    (w.ws / 'data' / '.pitchbook_session.json').write_text(json.dumps({'cookies': [
        {'name': 'SESSION', 'value': 'fake-session', 'domain': '.pitchbook.com', 'path': '/'}]}))
    return w, calls


@pytest.mark.parametrize('cap', [0, 2, 3, 4, 5])
def test_comparable_credit_cap_and_count_match_actual_requests(discovery_wire, cap):
    from vcoutreach.web.enrichment.discovery import discover_by_comparable_companies
    w, calls = discovery_wire
    with w.app.app_context():
        summary = discover_by_comparable_companies(credit_cap=cap, dry_run=True)
    assert summary['credits_spent'] <= cap, summary
    assert summary['credits_spent'] == len(calls), (summary, calls)
    if cap >= 2:
        assert calls, 'control: a positive budget must actually harvest configured comps'
    else:
        assert calls == []


def test_screener_empty_results_insert_no_contacts(discovery_wire, monkeypatch):
    from vcoutreach.web.enrichment import discovery, pitchbook_api
    from web_helpers import export_rows
    w, calls = discovery_wire
    monkeypatch.setattr(pitchbook_api, 'screener_search', lambda **kwargs: [])
    with w.app.app_context():
        summary = discovery.discover_investors(dry_run=False, resolve_contacts=False)
    assert summary['fetched'] == 0
    assert summary['inserted'] == 0
    assert export_rows(w) == []


@pytest.mark.parametrize('description,expected', [
    ('We invest at seed in consumer social apps, creator tools and marketplaces.', True),
])
def test_consumer_relevance_has_positive_and_negative_examples(description, expected):
    from vcoutreach.web.enrichment.discovery import is_consumer_relevant
    assert is_consumer_relevant('Cedar Grove Ventures', description) is expected


@pytest.mark.parametrize('firm,description', [('', ''), ('   ', '\t\n')])
def test_discovery_does_not_recommend_a_record_with_no_firm_or_description(firm, description):
    from vcoutreach.web.enrichment.discovery import is_consumer_relevant

    assert is_consumer_relevant(firm, description) is False


def test_target_profile_accepts_early_stage_consumer_investor():
    from vcoutreach.web.enrichment.discovery import is_target_profile
    accepted, reason = is_target_profile('Cedar Grove Ventures', 'Seed venture capital for consumer social apps',
                                         investor_type='Venture Capital', location='San Francisco, United States',
                                         last_deal_date='2026-10-01', aum_millions=80)
    assert accepted, reason


def test_partner_quality_increases_with_relevant_evidence():
    from vcoutreach.web.enrichment.discovery import partner_quality_score
    empty = partner_quality_score('Ada Lovelace', '', '')
    rich = partner_quality_score('Ada Lovelace', 'Partner investing in seed consumer social startups',
                                 'https://www.linkedin.com/in/ada/', last_deal_date='2026-10-01', company_count=3)
    assert rich > empty
