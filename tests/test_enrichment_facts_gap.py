"""Boundary and wiring gaps for enrichment facts feeding drafts, found by mutation checking.

Each test pins one public behavior of vcoutreach.web.enrichment_facts, vcoutreach.web.drafts_service or the
generate-hook route that the main suite left loose: exact thresholds and caps, ranking inputs, dedupe,
per-URL limit, provenance wording, drafts_service wiring and the plain-language failure labels.

No dash characters appear in this file; the two long dashes are built with chr().
"""
from __future__ import annotations

import pytest
import re

from test_enrichment_facts_reach_drafts import (
    Recorder, add_draft_row, fact, generate_hook, install, put_enrichment, write_all,
)
from web_helpers import ADA, csv_row, make_web, seed_contacts  # noqa: F401

from vcoutreach.web.enrichment_facts import select_enrichment_facts

EM = chr(0x2014)
EN = chr(0x2013)

THIN = csv_row(id="thin", name="Thea Rowan", email="thea@example.com", org="Rowan Capital", role="Partner",
               facts="", source_urls="")


@pytest.fixture
def ew(make_web):
    return make_web(sender=True)


def entry(text, url="https://src.example/a", ftype="portfolio_company", conf=0.9, source="linkedin", **extra):
    d = {"type": ftype, "text": text, "source_url": url, "confidence": conf, "source": source}
    d.update(extra)
    return d


def texts(result):
    return [r["text"] for r in result]


# ---- thresholds and caps ------------------------------------------------------------------------------------------

def test_confidence_floor_is_inclusive_at_055_and_rejects_anything_lower():
    got = select_enrichment_facts([
        entry("Kept exactly at the floor value alpha", "https://src.example/keep", conf=0.55),
        entry("Dropped just below the floor value bravo", "https://src.example/low1", conf=0.54),
        entry("Dropped well below the floor value charlie", "https://src.example/low2", conf=0.5),
    ])
    joined = " ".join(texts(got))
    assert "alpha" in joined and "bravo" not in joined and "charlie" not in joined, got


def test_a_fact_needs_at_least_four_words():
    got = select_enrichment_facts([
        entry("Three words only", "https://src.example/three"),
        entry("Exactly four words here", "https://src.example/four"),
    ])
    joined = " ".join(texts(got))
    assert "four words here" in joined and "Three words only" not in joined, got


def test_exactly_eight_facts_are_returned_by_default_and_the_limit_is_honoured():
    many = [entry(f"Distinct portfolio company number {chr(97 + i)}{chr(97 + i)}{chr(97 + i)} raised a seed round",
                  f"https://src.example/p{i}") for i in range(12)]
    assert len(select_enrichment_facts(many)) == 8
    assert len(select_enrichment_facts(many, limit=3)) == 3


def test_long_text_is_cut_to_280_characters_but_200_characters_survive_whole():
    mid = "Seed investor profile " + "x" * 178
    long = "Seed investor profile " + "y" * 578
    got = select_enrichment_facts([entry(mid, "https://src.example/mid"), entry(long, "https://src.example/long")])
    by_url = {r["source_url"]: r["text"] for r in got}
    assert mid in by_url["https://src.example/mid"]
    assert "y" * 280 not in by_url["https://src.example/long"]
    assert "y" * 200 in by_url["https://src.example/long"]


def test_pitchbook_facts_without_a_confidence_are_trusted_but_other_sources_are_not():
    pb = {"type": "pitchbook_recent_deal", "text": "Led the seed round in Quillfeather Robotics", "source_url": "https://pb.example/deal",
          "source": "pitchbook"}
    other = {"type": "portfolio_company", "text": "Unrated claim about Nimbus Aerospace funding", "source_url": "https://src.example/nimbus",
             "source": "linkedin"}
    joined = " ".join(texts(select_enrichment_facts([pb, other])))
    assert "Quillfeather" in joined and "Nimbus" not in joined


def test_a_non_numeric_confidence_is_treated_as_untrusted():
    got = select_enrichment_facts([entry("Garbled confidence value claim delta", "https://src.example/g", conf="n/a")])
    assert got == []


# ---- filtering ----------------------------------------------------------------------------------------------------

def test_bare_url_duplicates_text_with_a_url_start_and_dotless_hosts_are_not_facts():
    got = select_enrichment_facts([
        entry("https://elsewhere.example/post about seed investing today", "https://src.example/one"),
        entry("Local only link claim echo", "http://localhost/page"),
        entry("Intranet only link claim foxtrot", "https://intranet/page"),
        entry("Essay page with a quotable title golf", "https://src.example/two", ftype="essay_url"),
        entry("Genuine fact kept for hotel", "https://src.example/ok"),
    ])
    assert [r["source_url"] for r in got] == ["https://src.example/ok"], got


def test_clean_text_has_no_long_dashes():
    got = select_enrichment_facts([entry(f"Seed {EM} stage investor {EN} deeply technical", "https://src.example/d")])
    out = texts(got)[0]
    assert EM not in out and EN not in out and "Seed - stage investor - deeply technical" in out


# ---- dedupe, per-URL limit, ranking -------------------------------------------------------------------------------

def test_one_fact_per_url_even_when_the_texts_differ():
    got = select_enrichment_facts([
        entry("First distinct claim from the page india", "https://src.example/same"),
        entry("Second distinct claim from the page juliet", "https://src.example/same"),
    ])
    assert len(got) == 1 and got[0]["source_url"] == "https://src.example/same"


def test_ranking_follows_type_not_confidence_and_not_input_order():
    got = select_enrichment_facts([
        entry("Earlier firm was a boutique kilo shop", "https://src.example/prior", ftype="prior_firm", conf=0.99),
        entry("Portfolio company lima rocket maker", "https://src.example/port", ftype="portfolio_company", conf=0.9),
        entry("Thesis quote mike about patient capital", "https://src.example/thesis", ftype="pitchbook_thesis_quote", conf=0.6),
    ])
    assert [r["type"] for r in got] == ["pitchbook_thesis_quote", "portfolio_company", "prior_firm"], got


def test_a_fact_about_the_partner_outranks_a_better_typed_unrelated_fact():
    got = select_enrichment_facts([
        entry("Unrelated thesis quote november about markets", "https://src.example/x", ftype="pitchbook_thesis_quote", conf=0.99),
        entry("Brandt joined the board of Oscar Robotics", "https://src.example/y", ftype="prior_firm", conf=0.6),
    ], name="Owen Brandt", firm="Brandt Ventures")
    assert got[0]["source_url"] == "https://src.example/y", got


def test_equal_rank_facts_prefer_the_higher_confidence():
    got = select_enrichment_facts([
        entry("Portfolio company papa low confidence entry", "https://src.example/lo", conf=0.6),
        entry("Portfolio company quebec high confidence entry", "https://src.example/hi", conf=0.95),
    ])
    assert got[0]["source_url"] == "https://src.example/hi", got


# ---- provenance wording -------------------------------------------------------------------------------------------

def test_third_party_web_quotes_and_podcasts_are_attributed_to_a_web_page():
    got = select_enrichment_facts([
        entry("Patient capital beats fast capital in deep tech", "https://src.example/q", ftype="thesis_quote", source="web_search"),
        entry("Marlowe Weekly episode on seed investing", "https://src.example/pod", ftype="podcast_appearance", source="web_search_v2"),
        entry("Patient capital beats fast capital in hardware", "https://src.example/own", ftype="thesis_quote", source="linkedin"),
    ])
    by_url = {r["source_url"]: r["text"] for r in got}
    assert "web page" in by_url["https://src.example/q"].lower() and "https://src.example/q" in by_url["https://src.example/q"]
    assert "web page" in by_url["https://src.example/pod"].lower()
    assert "web page" not in by_url["https://src.example/own"].lower()
    assert "https://src.example/own" in by_url["https://src.example/own"]


# ---- stored rows (needs a workspace) ------------------------------------------------------------------------------

def test_a_stored_fact_without_its_own_url_falls_back_to_the_row_url(ew):
    from vcoutreach.web.enrichment_facts import enrichment_facts_for

    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [{"type": "portfolio_company", "text": "Portfolio company Zephyr Robotics seed round", "confidence": 0.9}],
                   source="linkedin", url="https://row.example/profile")
    got = enrichment_facts_for("thin")
    assert [r["source_url"] for r in got] == ["https://row.example/profile"], got


def test_enrichment_urls_are_added_to_the_contacts_source_urls_after_the_csv_ones(ew):
    from vcoutreach.web import drafts_service

    row = csv_row(id="csvy", name="Cora Yates", email="cora@example.com", org="Yates Fund", role="Partner",
                  facts="your memo on seed pricing", source_urls="https://csv.example/memo")
    seed_contacts(ew, [row])
    put_enrichment(ew, "csvy", [fact("Portfolio company Quillfeather Robotics, led the seed", "https://src.example/quill",
                                     ftype="portfolio_company", conf=0.9)], source="linkedin")
    (plain,) = [c for c in drafts_service.contacts() if c.id == "csvy"]
    (rich,) = [c for c in drafts_service.contacts(include_enrichment=True) if c.id == "csvy"]
    assert tuple(plain.facts) == ("your memo on seed pricing",) and tuple(plain.source_urls) == ("https://csv.example/memo",)
    assert rich.facts[0] == "your memo on seed pricing"
    assert tuple(rich.source_urls) == ("https://csv.example/memo", "https://src.example/quill")


def _enriched_thin(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q", ftype="portfolio_company", conf=0.9),
        fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/m", ftype="podcast_appearance", conf=0.9),
    ], source="linkedin")
    install(monkeypatch, Recorder())
    write_all(ew)


def _codes(issues):
    return [getattr(i, "code", None) or i["code"] for i in issues]


def test_checking_the_drafts_list_counts_enrichment_facts_as_facts(ew, monkeypatch):
    from vcoutreach.web import drafts_service

    _enriched_thin(ew, monkeypatch)
    (row,) = [r for r in drafts_service.list_drafts() if r["slug"] == "thin"]
    assert "insufficient_facts" not in _codes(row["issues"]), row["issues"]


def test_saving_a_draft_counts_enrichment_facts_as_facts(ew, monkeypatch):
    from vcoutreach.web import drafts_service

    _enriched_thin(ew, monkeypatch)
    saved = drafts_service.save_draft("thin", {"body": "Hello Thea, a short body with enough words to pass the lint check."})
    assert "insufficient_facts" not in _codes(saved.issues), saved.issues


def test_a_contact_with_no_facts_anywhere_still_gets_insufficient_facts_on_save(ew, monkeypatch):
    from vcoutreach.web import drafts_service

    seed_contacts(ew, [THIN, ADA])
    put_enrichment(ew, "ada", [fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
                                    ftype="portfolio_company", conf=0.9)], source="linkedin")
    install(monkeypatch, Recorder())
    write_all(ew)
    saved = drafts_service.save_draft("thin", {"body": "Hello Thea, a short body with enough words to pass the lint check."})
    assert "insufficient_facts" in _codes(saved.issues)


# ---- generate-hook failure labels --------------------------------------------------------------------------------

def _fail_with(ew, monkeypatch, message):
    from vcoutreach.web.llm.chain import ChainError

    seed_contacts(ew, [ADA])
    add_draft_row(ew, "ada")
    put_enrichment(ew, "ada", [fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
                                    ftype="portfolio_company", conf=0.95),
                               fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/ml",
                                    ftype="podcast_appearance", conf=0.9)])

    def boom(*a, **k):
        raise ChainError(message)

    monkeypatch.setattr("vcoutreach.web.llm.hook_generator.call_llm", boom)
    return generate_hook(ew, "ada")[2]


@pytest.mark.parametrize("message,label", [
    ("No provider is available for this request", "api key"),
    ("HTTP 429 Too Many Requests", "rate limit"),
    ("request exceeded the deadline after 30s: connection reset", "network problem"),
])
def test_failure_flash_names_the_class_in_plain_words_even_when_the_raw_error_does_not(ew, monkeypatch, message, label):
    text = _fail_with(ew, monkeypatch, message)
    assert label in text.lower(), text


def test_regenerating_a_hook_with_no_enrichment_says_no_enrichment_facts_yet(ew):
    seed_contacts(ew, [ADA])
    add_draft_row(ew, "ada")
    text = generate_hook(ew, "ada")[2]
    assert "no enrichment facts yet" in text.lower(), text


def test_a_very_long_provider_error_is_cut_before_it_reaches_the_flash(ew, monkeypatch):
    text = _fail_with(ew, monkeypatch, "Claude request failed: " + "boom " * 60 + "TAILMARKER" + " more" * 80)
    assert len(text) < 400
    assert "TAILMARKER" not in text and "boom " * 3 not in text
    assert "Traceback" not in text and "File \"" not in text
    assert re.search(r"provider|try again|unable|could not|unavailable|failed", text, re.I), text
