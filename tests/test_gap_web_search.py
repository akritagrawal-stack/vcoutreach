"""Offline search orchestration tests from the public docstrings."""
from __future__ import annotations

import http.client
import time
import urllib.request
import json
from unittest.mock import Mock

import pytest

from web_helpers import make_web, web_env_isolation  # noqa: F401


def result(url, title="Patient capital for research teams", snippet="Priya Raman discusses seed investing."):
    return {"url": url, "title": title, "snippet": snippet}


@pytest.mark.parametrize("name,firm", [("Priya Raman", "Cedar Grove Ventures"), ("Zoë Márquez", "Café Ventures")])
def test_partner_runs_the_four_documented_queries_and_deduplicates(monkeypatch, name, firm):
    """Contract lines 98 to 106: exact query set, deduplicated result list."""
    from vcoutreach.web.enrichment import web_search

    essay = result("https://notes.cedargrove.example/patient-capital")
    podcast = result("https://founder-radio.example/episodes/priya", "Founder conversations", "Building enduring research tools.")
    engine = Mock(side_effect=[[essay], [dict(essay)], [podcast], [dict(podcast)]])
    monkeypatch.setattr(web_search, "search", engine)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    found = web_search.search_partner(name, firm, count_per_query=3)
    queries = [call.args[0] if call.args else call.kwargs["query"] for call in engine.call_args_list]
    assert queries == [f'"{name}" "{firm}" thesis',
                       f'"{name}" essay site:medium.com OR site:substack.com',
                       f'"{name}" portfolio investment', f'"{name}" podcast']
    assert {item["url"] for item in found} == {"https://notes.cedargrove.example/patient-capital",
                                               "https://founder-radio.example/episodes/priya"}
    assert len(found) == 2
    assert all({"url", "title", "snippet"} <= item.keys() for item in found)
    for call in engine.call_args_list:
        assert (call.args[1] if len(call.args) > 1 else call.kwargs.get("count")) == 3


def test_partner_search_with_no_results_is_empty(monkeypatch):
    """Contract lines 98 to 106: merging four empty result sets is empty."""
    from vcoutreach.web.enrichment import web_search

    engine = Mock(return_value=[])
    monkeypatch.setattr(web_search, "search", engine)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert web_search.search_partner("Daniel Okafor", "Juniper Seed Partners") == []
    assert engine.call_count == 4


@pytest.mark.parametrize("name_key", ["partner_name", "name"])
def test_archetype_override_filters_junk_and_adds_provenance(monkeypatch, name_key):
    """Contract lines 113 to 128: aliases, explicit archetype, filtering, provenance."""
    from vcoutreach.web.enrichment import web_search

    payload = [result("https://www.linkedin.com/in/priya-raman"),
               result("https://www.crunchbase.com/person/priya-raman"),
               result("https://pitchbook.com/profiles/investor/12345-67"),
               result("https://priyaraman.substack.com/p/seed-research", "Patient seed research", "Independent teams build useful tools.")]
    engine = Mock(side_effect=lambda *args, **kwargs: [dict(item) for item in payload])
    monkeypatch.setattr(web_search, "search", engine)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    found = web_search.enrich_partner_web({name_key: "Priya Raman", "firm": "Cedar Grove Ventures",
                                          "archetype": "active_writer", "notes": "Quiet investor with no public essays"})
    assert len(found) == 1
    assert found[0]["url"] == "https://priyaraman.substack.com/p/seed-research"
    assert found[0]["_archetype"] == "active_writer"
    queries = [call.args[0] if call.args else call.kwargs["query"] for call in engine.call_args_list]
    assert found[0]["_query"] in queries
    assert {"title", "snippet"} <= found[0].keys()
    for call in engine.call_args_list:
        assert (call.args[1] if len(call.args) > 1 else call.kwargs.get("count", 10)) == 7


def test_archetype_search_forwards_an_explicit_result_count(monkeypatch):
    """Contract lines 116 to 117: requested results per query are configurable."""
    from vcoutreach.web.enrichment import web_search

    engine = Mock(return_value=[])
    monkeypatch.setattr(web_search, "search", engine)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert web_search.enrich_partner_web({"partner_name": "Daniel Okafor", "firm": "Juniper Seed Partners",
                                           "archetype": "active_writer"}, count_per_query=2) == []
    assert engine.called
    for call in engine.call_args_list:
        assert (call.args[1] if len(call.args) > 1 else call.kwargs.get("count")) == 2


def test_unavailable_search_transports_return_empty_results(make_web, monkeypatch, network_guard):
    """Contract lines 93 to 96: search failure returns [] instead of fabricated facts."""
    from vcoutreach.web.enrichment import web_search
    import requests

    def timeout(*args, **kwargs):
        raise TimeoutError("Search gateway timed out while looking up Priya Raman")

    monkeypatch.setattr(urllib.request, "urlopen", timeout)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", timeout)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", timeout)
    monkeypatch.setattr(requests.sessions.Session, "request", timeout)
    workspace = make_web()
    with workspace.app.app_context():
        assert web_search.search('"Priya Raman" "Cedar Grove Ventures" thesis', force_refresh=True) == []
    assert network_guard.attempts == []


def test_successful_search_is_cached_for_repeated_query(make_web, monkeypatch):
    from vcoutreach.web.enrichment import web_search
    import requests
    # Move past any transport-failure cooldown left by another search test.
    real_time, real_monotonic = time.time, time.monotonic
    monkeypatch.setattr(time, 'time', lambda: real_time() + 86400)
    monkeypatch.setattr(time, 'monotonic', lambda: real_monotonic() + 86400)
    from datetime import timedelta
    original_datetime = web_search.datetime
    base = original_datetime.now(web_search.timezone.utc)
    elapsed = [timedelta(0)]
    class Clock(original_datetime):
        @classmethod
        def now(cls, tz=None):
            value = base + elapsed[0]
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr(web_search, 'datetime', Clock)

    calls = []
    hit = result("https://example.com/patient-capital")
    payload = {"results": [hit], "web": {"results": [hit]}}
    def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        response.read = lambda *a: response.content
        response.getcode = lambda: 200
        response.headers['Content-Type'] = 'application/json'
        return response
    monkeypatch.setattr(requests.sessions.Session, 'request', fetch)
    monkeypatch.setattr(urllib.request, 'urlopen', fetch)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fetch)
    w = make_web({'SEARXNG_URL': 'http://127.0.0.1:8080', 'ENABLE_SEARXNG': '1', 'SEARXNG_ENABLED': '1'})
    with w.app.app_context():
        first = web_search.search('patient capital research', count=3)
        assert [r['url'] for r in first] == [hit['url']], (first, calls)
        count = len(calls)
        assert count == 1
        elapsed[0] = timedelta(seconds=1)
        second = web_search.search('patient capital research', count=3)
        assert second == first
        assert len(calls) == count, 'a fresh cached query must not call a provider again'
        elapsed[0] = timedelta(days=6)
        assert web_search.search('patient capital research', count=3) == first
        assert len(calls) == count
        elapsed[0] = timedelta(days=8)
        assert web_search.search('patient capital research', count=3) == first
        assert len(calls) == count + 1, 'an eight-day-old entry must be refreshed'
