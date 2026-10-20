"""Firm enrichment tests using only documented public function seams."""
from __future__ import annotations

import socket
import urllib.request
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest


@pytest.fixture
def firm_pages(monkeypatch):
    """Realistic HTML over fake standard HTTP clients, with no DNS or sockets."""
    import requests

    pages = {}
    calls = []

    def fetch(method, url, *args, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.encoding = "utf-8"
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response._content = pages[url].encode("utf-8")
        response.read = lambda *args: response.content
        response.getcode = lambda: 200
        return response

    def session_request(session, method, url, *args, **kwargs):
        return fetch(method, url, *args, **kwargs)

    def urlopen(target, *args, **kwargs):
        url = target if isinstance(target, str) else target.full_url
        return fetch("GET", url)

    monkeypatch.setattr(requests.sessions.Session, "request", session_request)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", lambda self, target, *args, **kwargs: urlopen(target))
    # The existing HTTP test fixtures use a public test IP for local URL checks.
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    return pages, calls


@pytest.mark.parametrize("path,label", [("/team", "Our team"), ("/people", "People"), ("/about", "About us")])
def test_team_link_is_resolved_to_an_absolute_homepage_url(firm_pages, path, label):
    """Contract lines 143 to 145: team/people/about anchors become absolute URLs."""
    from vcoutreach.web.enrichment.firm_website import discover_team_url

    pages, calls = firm_pages
    pages["https://cedargroveventures.example/"] = (
        '<!doctype html><html lang="en"><head><title>Cedar Grove Ventures</title></head>'
        '<body><header><a href="/">Cedar Grove</a></header><nav>'
        f'<a href="{path}">{label}</a></nav><main><h1>Patient seed capital</h1>'
        '<p>Zoë Márquez backs independent research teams.</p></main></body></html>'
    )
    assert discover_team_url("https://cedargroveventures.example/") == "https://cedargroveventures.example" + path
    assert calls


@pytest.mark.parametrize("path,label", [("/portfolio", "Portfolio"), ("/companies", "Our companies"), ("/investments", "Investments")])
def test_portfolio_link_is_resolved_to_an_absolute_url(firm_pages, path, label):
    """Contract lines 146 to 148: portfolio/company/investment links become absolute."""
    from vcoutreach.web.enrichment.firm_website import discover_portfolio_url

    pages, calls = firm_pages
    pages["https://cedargroveventures.example/"] = (
        '<!doctype html><html><head><title>Cedar Grove Ventures</title></head><body>'
        f'<nav><a href="{path}">{label}</a></nav>'
        '<main><h1>Research tools worth building</h1><p>Meet the teams we back.</p></main></body></html>'
    )
    assert discover_portfolio_url("https://cedargroveventures.example/") == "https://cedargroveventures.example" + path
    assert calls


@pytest.mark.parametrize("function", ["discover_team_url", "discover_portfolio_url"])
def test_homepage_without_relevant_anchors_returns_none(firm_pages, function):
    """Contract lines 143 to 148: discovery can return None when no link exists."""
    from vcoutreach.web.enrichment import firm_website

    pages, calls = firm_pages
    pages["https://cedargroveventures.example/"] = (
        '<!doctype html><html><body><h1>Cedar Grove Ventures</h1>'
        '<p>Independent seed investors.</p><a href="/privacy">Privacy policy</a></body></html>'
    )
    assert getattr(firm_website, function)("https://cedargroveventures.example/") is None


def company(name):
    return {"name": name, "url": "https://portfolio.cedargrove.example/" + name.lower().replace(" ", "-"),
            "sector": "Research software", "description": "Tools for independent research teams."}


@pytest.mark.parametrize("function,label", [("discover_team_url", "Our team"),
                                            ("discover_portfolio_url", "Our portfolio")])
@pytest.mark.parametrize("external_first", [True, False])
def test_navigation_prefers_same_domain_among_competing_text_links(firm_pages, function, label, external_first):
    from vcoutreach.web.enrichment import firm_website

    pages, calls = firm_pages
    home = "https://cedargroveventures.example/"
    links = ['<a href="https://directory.example/navigation">' + label + '</a>',
             '<a href="/navigation">' + label + '</a>']
    if not external_first:
        links.reverse()
    pages[home] = "<html><body><nav>" + "".join(links) + "</nav></body></html>"
    assert getattr(firm_website, function)(home) == home + "navigation"


def test_website_fallback_uses_official_query_and_skips_social_results(monkeypatch):
    """Contract lines 137 to 141: unknown firms use the top non-social search hit."""
    from vcoutreach.web.enrichment import firm_website, web_search

    engine = Mock(return_value=[
        {"url": "https://www.linkedin.com/company/cedar-grove-ventures", "title": "Cedar Grove Ventures", "snippet": "Team updates"},
        {"url": "https://cedargroveventures.example/", "title": "Cedar Grove Ventures", "snippet": "Patient seed capital"},
        {"url": "https://venture-directory.example/cedar-grove", "title": "Fund directory", "snippet": "Cedar Grove listing"},
    ])
    monkeypatch.setattr(web_search, "search", engine)
    homepage = urlsplit(firm_website.find_firm_website("Cedar Grove Ventures"))
    assert homepage.scheme == "https"
    assert homepage.netloc == "cedargroveventures.example"
    assert homepage.path in ("", "/")
    query = engine.call_args.args[0]
    assert "Cedar Grove Ventures" in query
    assert {"official", "website"} <= set(query.lower().split())


def test_unknown_firm_without_fallback_makes_no_search(monkeypatch):
    """Contract lines 137 to 141: disabling fallback does not call search."""
    from vcoutreach.web.enrichment import firm_website, web_search

    engine = Mock(side_effect=AssertionError("Search was disabled by the operator"))
    monkeypatch.setattr(web_search, "search", engine)
    assert firm_website.find_firm_website("Café Research Seed Collective", fallback_search=False) is None
    engine.assert_not_called()


def test_unknown_firm_with_no_search_hits_returns_none(monkeypatch):
    """Contract lines 138 to 141: no discovered homepage returns None."""
    from vcoutreach.web.enrichment import firm_website, web_search

    monkeypatch.setattr(web_search, "search", lambda *args, **kwargs: [])
    assert firm_website.find_firm_website("Juniper Research Seed Partners") is None


@pytest.mark.parametrize("include_portfolio", [True, False])
def test_firm_enrichment_combines_thesis_and_caps_portfolio(monkeypatch, include_portfolio):
    """Contract lines 165 to 179: composed firm result and ten-company cap."""
    from vcoutreach.web.enrichment import firm_website

    names = ["Lumenlist", "Field Atlas", "Café Notebook", "Juniper Lab", "Open Orchard", "Signal Desk",
             "Northstar Maps", "Wren Studio", "Paper Lantern", "Riverbend Tools", "Cobalt Notes", "Mosaic Lab"]
    monkeypatch.setattr(firm_website, "find_firm_website", lambda *args, **kwargs: "https://cedargroveventures.example/")
    monkeypatch.setattr(firm_website, "discover_team_url", lambda url: None)
    monkeypatch.setattr(firm_website, "discover_portfolio_url", lambda url: "https://cedargroveventures.example/companies" if include_portfolio else None)
    monkeypatch.setattr(firm_website, "scrape_portfolio_page", lambda url: {"companies": [company(name) for name in names], "raw_text_excerpt": "Research tools we back"})
    monkeypatch.setattr(firm_website, "scrape_thesis_or_about", lambda url: {"thesis_text": "We back independent research teams at seed.", "mission_text": None, "raw_text_excerpt": "Patient capital"})
    found = firm_website.enrich_firm("Cedar Grove Ventures")
    assert {"firm_url", "team_page_url", "portfolio_page_url", "partner_bio", "partner_title", "partner_linkedin", "firm_thesis", "recent_portfolio_companies"} <= found.keys()
    assert found["firm_thesis"] == "We back independent research teams at seed."
    assert len(found["recent_portfolio_companies"]) == (10 if include_portfolio else 0)


def test_empty_enrichment_has_no_facts():
    """Contract lines 180 to 187: absent source fields produce no facts."""
    from vcoutreach.web.enrichment.firm_website import to_enrichment_facts

    assert to_enrichment_facts({}, "https://cedargroveventures.example/") == []


def test_unverified_firm_content_cannot_become_personalization_facts():
    from vcoutreach.web.enrichment.firm_website import to_enrichment_facts

    content = {"firm_thesis": "We back independent research teams at seed.",
               "partner_bio": "Zoë Márquez invests in research tools.",
               "partner_title": "Managing Partner",
               "recent_portfolio_companies": [company("Lumenlist")]}
    url = "https://cedargroveventures.example/about"
    assert to_enrichment_facts(content, url), "control: the populated source has usable facts"
    assert to_enrichment_facts({**content, "verification_status": "unverified"}, url) == []


def test_facts_have_documented_types_provenance_and_limits():
    """Contract lines 181 to 187: typed facts, 280-char bio, ten companies."""
    from vcoutreach.web.enrichment.firm_website import to_enrichment_facts

    names = ["Lumenlist", "Field Atlas", "Café Notebook", "Juniper Lab", "Open Orchard", "Signal Desk",
             "Northstar Maps", "Wren Studio", "Paper Lantern", "Riverbend Tools", "Cobalt Notes", "Mosaic Lab"]
    found = to_enrichment_facts({"firm_thesis": "We back independent research teams at seed.",
                                 "partner_bio": "Zoë Márquez invests in research tools. " * 20,
                                 "partner_title": "Managing Partner",
                                 "recent_portfolio_companies": [company(name) for name in names]},
                                "https://cedargroveventures.example/about")
    grouped = {}
    for fact in found:
        assert {"type", "text", "source_url", "confidence"} <= fact.keys()
        grouped.setdefault(fact["type"], []).append(fact)
    assert set(grouped) == {"firm_thesis_quote", "partner_bio", "partner_title", "portfolio_company"}
    assert len(grouped["portfolio_company"]) == 10
    assert len(grouped["partner_bio"]) == 1
    assert len(grouped["partner_bio"][0]["text"]) <= 280
    assert "Zoë Márquez" in grouped["partner_bio"][0]["text"]
    assert "Managing Partner" in grouped["partner_title"][0]["text"]
    assert all(fact["source_url"] == "https://cedargroveventures.example/about" for fact in found)


@pytest.mark.parametrize("function,reason", [
    ("scrape_team_page", "The only docstring is Workspace-backed outreach integration; team output schema is absent (149 to 150)."),
    ("discover_team_url/discover_portfolio_url failure policy", "Timeout, malformed HTML, invalid homepage and conflicting-anchor selection behavior are absent (142 to 148)."),
    ("scrape_portfolio_page", "No HTML selector/extraction mapping or transport contract is supplied (151 to 158)."),
    ("scrape_thesis_or_about", "No paragraph classification rules, fetch client, or failure policy is supplied (159 to 164)."),
])
def test_unspecified_firm_scraper_contract(function, reason):
    pytest.skip(f"UNSPECIFIED {function}: {reason}")
