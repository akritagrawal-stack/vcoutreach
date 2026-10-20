"""Stored enrichment facts reach the drafting engine, grounding stays strict, and the failure paths speak plainly.

Written from the feature spec only (never from the implementation). Every test drives a public surface:
import contacts through the app, store enrichment rows exactly as the `enrichment` table defines them,
write drafts through POST /crm/drafts/write, then read the draft files, the drafts table and the pages.
Nothing here touches the network. The only things replaced are the model provider the drafts route
resolves (so the test can see what the drafting engine received) and the LLM call used by hook
regeneration.

Assumptions the implementation must satisfy (flexible matching is used wherever the exact wording or
shape is not fixed by the public contract):
* An enrichment row is (partner_id, source, url, facts_json) with facts_json a JSON list of
  {type, text, source_url, confidence}. The route reads those rows for the partner being drafted.
* Facts reach the engine as the strings in `Contact.facts`, with the matching URLs in
  `Contact.source_urls`. A fact string may carry a prefix or suffix added by the builder, so tests look
  for a unique marker word inside the string rather than for exact equality.
* CSV facts come first in `Contact.facts`, all of them, untouched.
* "About 8" is tested as at most 10 enrichment facts, with the best ones kept and the worst dropped.
* Authorship wording is tested as: a third party essay is not presented with "your essay", "you wrote"
  or similar next to its title.
* Flash messages after generate-hook are read from the page the redirect lands on.
* A failure message names its class with any of a small set of plain words (see FAILURE_CLASSES).

No dash characters appear in this file.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, GRACE, MINA, Web, body, csv_row, page_text, read_draft_file, seed_contacts,
)

FAKE_KEY = "sk-live-FAKEKEY-9f3a1c77d0e5b2a4"


# ---------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------

def _db(w: Web) -> sqlite3.Connection:
    return sqlite3.connect(str(Path(w.ws) / "data" / "outreach.db"))


def pid_of(w: Web, slug: str) -> int:
    c = _db(w)
    try:
        row = c.execute("SELECT id FROM partners WHERE slug=?", (slug,)).fetchone()
    finally:
        c.close()
    assert row, f"no partner with slug {slug!r} after import"
    return int(row[0])


def fact(text, url="https://src.example/page", ftype="essay_title", conf=0.85, **extra):
    d = {"type": ftype, "text": text, "source_url": url, "confidence": conf}
    d.update(extra)
    return d


def put_enrichment(w: Web, slug: str, facts, source="web_search", url=None, confidence=0.85):
    """Store one enrichment row for the partner, the way the enrichment pipeline does."""
    if url is None:
        urls = [f.get("source_url") for f in facts if f.get("source_url")]
        url = urls[0] if urls else ""
    c = _db(w)
    try:
        c.execute(
            "INSERT INTO enrichment (partner_id, source, url, raw_text, facts_json, confidence, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (pid_of(w, slug), source, url, "", json.dumps(list(facts)), confidence),
        )
        c.commit()
    finally:
        c.close()


class Recorder:
    """Stands in for the model provider the drafts route resolves. Records what the engine was given.

    By default it behaves exactly like the offline template provider after recording. With hook_for it
    returns the hook text that function builds, so grounding can be exercised with chosen claims.
    """

    name = "template"

    def __init__(self, hook_for=None):
        self.hook_for = hook_for
        self.seen = {}

    def personalize(self, request):
        from vcoutreach.providers.template_provider import TemplateProvider
        from vcoutreach.types import Hook

        c = request.contact
        self.seen[c.id] = {"facts": [str(f) for f in c.facts], "urls": [str(u) for u in c.source_urls]}
        if self.hook_for is not None:
            return Hook(text=self.hook_for(c), provider=self.name, model="none")
        return TemplateProvider().personalize(request)


def install(monkeypatch, rec) -> None:
    monkeypatch.setattr("vcoutreach.providers.resolve_provider", lambda *a, **k: rec)
    import vcoutreach.web.drafts_service as ds

    if hasattr(ds, "resolve_provider"):
        monkeypatch.setattr(ds, "resolve_provider", lambda *a, **k: rec)


def write_all(w: Web, provider="template"):
    r = w.post(w.url("drafts_write"), {"provider": provider}, follow=True)
    assert r.status_code == 200
    return r


def draft_path(w: Web, slug: str):
    for folder in (Path(w.ws) / "drafts", Path(w.ws) / "drafts" / "blocked"):
        p = folder / f"{slug}.md"
        if p.is_file():
            return p
    return None


def issue_codes(w: Web, slug: str):
    p = draft_path(w, slug)
    assert p is not None, f"no draft file written for {slug}"
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("issues:"):
            return [c.strip() for c in line[len("issues:"):].split(";") if c.strip()]
        if not line.strip():
            break
    return []


def is_blocked(w: Web, slug: str) -> bool:
    p = draft_path(w, slug)
    assert p is not None, f"no draft file written for {slug}"
    return p.parent.name == "blocked"


def with_marker(facts, marker):
    """Facts that contain the given marker word inside the string (builder may add a prefix)."""
    return [f for f in facts if marker in f]


def pos(facts, marker):
    for i, f in enumerate(facts):
        if marker in f:
            return i
    return -1


def count_markers(facts, markers):
    return sum(1 for m in markers if pos(facts, m) >= 0)


# Plain fixtures. THIN has no CSV facts at all, so every fact it can use must come from enrichment.
THIN = csv_row(id="thin", name="Thea Rowan", email="thea@example.com", org="Rowan Capital", role="Partner",
               facts="", source_urls="")
OTHER = csv_row(id="other", name="Owen Brandt", email="owen@example.com", org="Brandt Ventures", role="Partner",
                facts="", source_urls="")


@pytest.fixture
def ew(make_web):
    """A workspace with a sender profile and no contacts yet."""
    return make_web(sender=True)


# ---------------------------------------------------------------------------------------------------------------
# 1. Enrichment facts reach the drafting engine: cap, dedupe, order, CSV priority, source URLs
# ---------------------------------------------------------------------------------------------------------------

def test_stored_enrichment_facts_reach_the_engine_with_their_source_urls(ew, monkeypatch):
    seed_contacts(ew, [ADA])
    put_enrichment(ew, "ada", [
        fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/quill",
             ftype="portfolio_company", conf=0.9),
        fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/marlowe",
             ftype="podcast_appearance", conf=0.8),
    ])
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    seen = rec.seen["ada"]
    assert with_marker(seen["facts"], "Quillfeather"), seen["facts"]
    assert with_marker(seen["facts"], "Marlowe"), seen["facts"]
    joined_urls = " ".join(seen["urls"] + seen["facts"])
    assert "https://src.example/quill" in joined_urls
    assert "https://src.example/marlowe" in joined_urls


def test_the_enrichment_facts_are_capped(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    markers = [f"Capmark{chr(97 + i)}x" for i in range(16)]
    facts = [fact(f"Portfolio company {m} Holdings, backed in an early round", f"https://src.example/{m}",
                  ftype="portfolio_company", conf=round(0.99 - i * 0.02, 2)) for i, m in enumerate(markers)]
    put_enrichment(ew, "thin", facts[:8], source="web_search", url="https://src.example/a")
    put_enrichment(ew, "thin", facts[8:], source="web_search_v2", url="https://src.example/b")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    used = count_markers(got, markers)
    assert 3 <= used <= 10, f"expected a bounded set of about 8 enrichment facts, got {used}: {got}"
    assert pos(got, markers[0]) >= 0, "the most relevant fact must survive the cap"
    assert pos(got, markers[-1]) < 0, "the least relevant fact must be cut by the cap"


def test_the_same_fact_from_two_sources_is_given_once(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    text = "Portfolio company Dupemark Analytics, led the seed round"
    put_enrichment(ew, "thin", [fact(text, "https://src.example/one", ftype="portfolio_company")],
                   source="web_search", url="https://src.example/one")
    put_enrichment(ew, "thin", [fact(text.upper(), "https://src.example/two", ftype="portfolio_company")],
                   source="web_search_v2", url="https://src.example/two")
    put_enrichment(ew, "thin", [fact(text + ".", "https://src.example/three", ftype="portfolio_company")],
                   source="firm_website", url="https://src.example/three")
    put_enrichment(ew, "thin", [fact("Recent podcast appearance on Othershow about operations",
                                     "https://src.example/four", ftype="podcast_appearance")],
                   source="linkedin", url="https://src.example/four")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    hits = [f for f in got if "dupemark" in f.lower()]
    assert len(hits) == 1, hits
    assert with_marker(got, "Othershow"), "distinct facts must not be collapsed with the duplicate"


def test_more_relevant_enrichment_facts_come_first(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Lowrank Textiles, a small early bet", "https://src.example/low",
             ftype="portfolio_company", conf=0.65),
    ], source="web_search", url="https://src.example/low")
    put_enrichment(ew, "thin", [
        fact("Portfolio company Midrank Freight, backed at seed", "https://src.example/mid",
             ftype="portfolio_company", conf=0.8),
    ], source="web_search_v2", url="https://src.example/mid")
    put_enrichment(ew, "thin", [
        fact("Portfolio company Toprank Systems, led the seed round", "https://src.example/top",
             ftype="portfolio_company", conf=0.97),
    ], source="firm_website", url="https://src.example/top")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    assert -1 < pos(got, "Toprank") < pos(got, "Midrank") < pos(got, "Lowrank"), got


def test_csv_facts_keep_priority_and_are_never_dropped(ew, monkeypatch):
    csv_facts = ["your essay on how small teams ship weekly", "your role leading Northwind Labs", "Seed deal"]
    ada = csv_row(id="ada", name="Ada Lovelace", email="ada@example.com", org="Northwind Labs", role="Partner",
                  facts=" | ".join(csv_facts), source_urls="https://example.com/essay", lane="first_touch")
    seed_contacts(ew, [ada])
    markers = [f"Prio{chr(97 + i)}x" for i in range(14)]
    put_enrichment(ew, "ada", [
        fact(f"Portfolio company {m} Group, backed in an early round", f"https://src.example/{m}",
             ftype="portfolio_company", conf=0.99) for m in markers[:7]
    ], source="web_search", url="https://src.example/p1")
    put_enrichment(ew, "ada", [
        fact(f"Portfolio company {m} Group, backed in an early round", f"https://src.example/{m}",
             ftype="portfolio_company", conf=0.99) for m in markers[7:]
    ], source="web_search_v2", url="https://src.example/p2")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["ada"]["facts"]
    assert got[:3] == csv_facts, f"CSV facts must stay first and untouched, got {got[:4]}"
    first_enriched = min(i for i in range(len(got)) if any(m in got[i] for m in markers))
    assert first_enriched >= 3
    assert "https://example.com/essay" in rec.seen["ada"]["urls"], "the CSV source URL must be kept"


def test_guessed_email_rows_are_not_offered_as_facts(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [fact("Possible address pattern Guessmark Domain Pattern", "https://src.example/g",
                                     ftype="current_role", conf=0.99)],
                   source="email_guess", url="https://src.example/g")
    put_enrichment(ew, "thin", [fact("Portfolio company Realmark Labs, led the seed round",
                                     "https://src.example/r", ftype="portfolio_company")],
                   source="web_search", url="https://src.example/r")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    assert with_marker(got, "Realmark")
    assert not with_marker(got, "Guessmark"), got


# ---------------------------------------------------------------------------------------------------------------
# 2. Quality filter
# ---------------------------------------------------------------------------------------------------------------

def test_fragments_that_start_mid_word_are_dropped(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("tion of early stage hardware companies across Midwordone Europe", "https://src.example/f1",
             ftype="public_writing_topic"),
        fact("ing weekly shipping habits inside Midwordtwo small teams", "https://src.example/f2",
             ftype="public_writing_topic"),
        fact("Portfolio company Goodmark Labs, led the seed round", "https://src.example/f3",
             ftype="portfolio_company"),
    ], source="web_search", url="https://src.example/f")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    assert with_marker(got, "Goodmark"), got
    assert not with_marker(got, "Midwordone") and not with_marker(got, "Midwordtwo"), got


def test_very_short_fragments_are_dropped(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Qzxshort", "https://src.example/s1", ftype="public_writing_topic"),
        fact("Wvu pod", "https://src.example/s2", ftype="podcast_appearance"),
        fact("Portfolio company Longmark Labs, led the seed round", "https://src.example/s3",
             ftype="portfolio_company"),
    ], source="web_search", url="https://src.example/s")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    assert with_marker(got, "Longmark"), got
    assert not with_marker(got, "Qzxshort") and not with_marker(got, "Wvu pod"), got


def test_facts_without_a_source_url_are_dropped(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Nourlmark Freight, led the seed round", "", ftype="portfolio_company"),
    ], source="web_search", url="")
    put_enrichment(ew, "thin", [
        fact("Portfolio company Withurlmark Systems, led the seed round", "https://src.example/w",
             ftype="portfolio_company"),
    ], source="web_search_v2", url="https://src.example/w")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    assert with_marker(got, "Withurlmark"), got
    assert not with_marker(got, "Nourlmark"), got


AUTHORSHIP = re.compile(r"(?i)\b(your|you wrote|you authored|you published|written by|by thea)\b")


def test_a_third_party_essay_is_not_presented_as_the_partners_own(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    title = "Why Hardware Startups Need Patient Thirdpartymark Capital"
    put_enrichment(ew, "thin", [
        fact(title, "https://blog.thirdparty.example/hardware-patient-capital", ftype="essay_title", conf=0.9),
    ], source="web_search", url="https://blog.thirdparty.example/hardware-patient-capital")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    got = rec.seen["thin"]["facts"]
    mine = with_marker(got, "Thirdpartymark")
    assert mine, "a sourced essay title is still a usable fact"
    for f in mine:
        before = f.split("Why Hardware")[0]
        assert not AUTHORSHIP.search(before), f"fact attributes the essay to the partner: {f!r}"
    p = draft_path(ew, "thin")
    assert p is not None
    text = p.read_text(encoding="utf-8")
    window = re.search(r"(?is)(.{0,60})Thirdpartymark", text)
    if window:
        assert not AUTHORSHIP.search(window.group(1)), f"draft claims authorship: {window.group(1)!r}"


# ---------------------------------------------------------------------------------------------------------------
# 3. Grounding stays strict
# ---------------------------------------------------------------------------------------------------------------

def test_a_partner_with_no_facts_anywhere_is_still_blocked(ew):
    seed_contacts(ew, [MINA])
    write_all(ew)
    assert is_blocked(ew, "mina")
    assert "insufficient_facts" in issue_codes(ew, "mina")


def test_a_partner_with_only_junk_enrichment_is_still_blocked(ew):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("tion of early stage hardware companies across Junkmarkone", "https://src.example/j1",
             ftype="public_writing_topic"),
        fact("Qzx", "https://src.example/j2", ftype="public_writing_topic"),
        fact("Portfolio company Junkmarkthree Freight, led the seed round", "", ftype="portfolio_company"),
    ], source="web_search", url="")
    write_all(ew)
    assert is_blocked(ew, "thin")
    assert "insufficient_facts" in issue_codes(ew, "thin")


def test_a_partner_with_enough_good_enrichment_facts_is_not_blocked_for_insufficient_facts(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    markers = ["Zephyrine", "Oxbridgeton", "Marrowfield"]
    put_enrichment(ew, "thin", [
        fact("Portfolio company Zephyrine Robotics, led the seed round", "https://src.example/z",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Oxbridgeton Weekly about seed investing", "https://src.example/o",
             ftype="podcast_appearance", conf=0.85),
        fact("Public writing on Marrowfield style operating cadence for small teams", "https://src.example/m",
             ftype="public_writing_topic", conf=0.8),
    ], source="web_search", url="https://src.example/set")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    codes = issue_codes(ew, "thin")
    assert "insufficient_facts" not in codes, codes
    assert "no_facts" not in codes, codes
    assert "ungrounded_claim" not in codes, f"the engine's own grounding check must pass: {codes}"
    text = draft_path(ew, "thin").read_text(encoding="utf-8")
    assert any(m in text for m in markers), "the draft hook must refer to a supplied fact"
    supplied = " ".join(rec.seen["thin"]["facts"])
    for token in re.findall(r"\b[A-Z][a-z]{5,}\b", text):
        if token in markers:
            assert token in supplied


def test_the_real_template_provider_path_also_clears_the_block(ew):
    """No stand-in provider here: the route resolves the offline template provider itself."""
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Zephyrine Robotics, led the seed round", "https://src.example/z",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Oxbridgeton Weekly about seed investing", "https://src.example/o",
             ftype="podcast_appearance", conf=0.85),
    ])
    write_all(ew, provider="template")
    assert "insufficient_facts" not in issue_codes(ew, "thin")
    draft = read_draft_file(ew.ws, "thin")
    assert draft is not None
    assert "Zephyrine" in draft.body or "Oxbridgeton" in draft.body


def test_a_hook_citing_a_supplied_enrichment_fact_is_grounded(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/ml",
             ftype="podcast_appearance", conf=0.9),
    ])
    rec = Recorder(hook_for=lambda c: "I saw that you led the seed round for Quillfeather Robotics.")
    install(monkeypatch, rec)
    write_all(ew)
    assert "ungrounded_claim" not in issue_codes(ew, "thin")


def test_a_hook_asserting_an_unsupplied_claim_is_still_flagged(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/ml",
             ftype="podcast_appearance", conf=0.9),
    ])
    rec = Recorder(hook_for=lambda c: "I saw that you led the Brightwater Biologics round for 40 million dollars.")
    install(monkeypatch, rec)
    write_all(ew)
    assert "ungrounded_claim" in issue_codes(ew, "thin")
    assert is_blocked(ew, "thin")


def test_a_fact_the_quality_filter_dropped_is_not_valid_grounding(ew, monkeypatch):
    seed_contacts(ew, [THIN])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/ml",
             ftype="podcast_appearance", conf=0.9),
    ], source="web_search", url="https://src.example/q")
    put_enrichment(ew, "thin", [
        fact("Portfolio company Oldmoor Freight, led the seed round", "", ftype="portfolio_company", conf=0.99),
    ], source="web_search_v2", url="")
    rec = Recorder(hook_for=lambda c: "I saw that you led the seed round for Oldmoor Freight.")
    install(monkeypatch, rec)
    write_all(ew)
    assert not with_marker(rec.seen["thin"]["facts"], "Oldmoor")
    assert "ungrounded_claim" in issue_codes(ew, "thin")


# ---------------------------------------------------------------------------------------------------------------
# 4. Partner isolation
# ---------------------------------------------------------------------------------------------------------------

def test_one_partners_enrichment_never_reaches_another_partners_draft(ew, monkeypatch):
    seed_contacts(ew, [THIN, OTHER])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Alphaonlymark Robotics, led the seed round", "https://src.example/a1",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Alphashow about seed investing", "https://src.example/a2",
             ftype="podcast_appearance", conf=0.9),
    ], source="web_search", url="https://src.example/a")
    put_enrichment(ew, "other", [
        fact("Portfolio company Betaonlymark Freight, led the seed round", "https://src.example/b1",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Betashow about seed investing", "https://src.example/b2",
             ftype="podcast_appearance", conf=0.9),
    ], source="web_search", url="https://src.example/b")
    rec = Recorder()
    install(monkeypatch, rec)
    write_all(ew)
    a, b = rec.seen["thin"], rec.seen["other"]
    assert with_marker(a["facts"], "Alphaonlymark") and with_marker(b["facts"], "Betaonlymark")
    assert not with_marker(a["facts"], "Betaonlymark") and not with_marker(a["facts"], "Betashow")
    assert not with_marker(b["facts"], "Alphaonlymark") and not with_marker(b["facts"], "Alphashow")
    assert not any("src.example/b" in u for u in a["urls"])
    assert not any("src.example/a" in u for u in b["urls"])
    text_a = draft_path(ew, "thin").read_text(encoding="utf-8")
    text_b = draft_path(ew, "other").read_text(encoding="utf-8")
    assert "Betaonlymark" not in text_a and "Betashow" not in text_a
    assert "Alphaonlymark" not in text_b and "Alphashow" not in text_b


def test_a_partner_with_no_enrichment_does_not_borrow_anothers(ew, monkeypatch):
    seed_contacts(ew, [THIN, OTHER])
    put_enrichment(ew, "thin", [
        fact("Portfolio company Lendermark Robotics, led the seed round", "https://src.example/l1",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Lendershow about seed investing", "https://src.example/l2",
             ftype="podcast_appearance", conf=0.9),
    ])
    install(monkeypatch, Recorder())
    write_all(ew)
    assert is_blocked(ew, "other")
    assert "insufficient_facts" in issue_codes(ew, "other")


# ---------------------------------------------------------------------------------------------------------------
# 5. POST /partners/<id>/generate-hook
# ---------------------------------------------------------------------------------------------------------------

def add_draft_row(w: Web, slug: str, hook="Old hook text about a prior note", check=None):
    c = _db(w)
    try:
        c.execute(
            "INSERT INTO drafts (partner_id, hook, subject, boilerplate_version, status, hallucination_check) "
            "VALUES (?, ?, 'Old subject', 'v1', 'pending', ?)", (pid_of(w, slug), hook, check))
        c.commit()
    finally:
        c.close()


def draft_count(w: Web, slug: str) -> int:
    c = _db(w)
    try:
        return c.execute("SELECT COUNT(*) FROM drafts WHERE partner_id=?", (pid_of(w, slug),)).fetchone()[0]
    finally:
        c.close()


def flash_text(markup: str) -> str:
    """Only the notification list, so navigation words on the page cannot satisfy a check."""
    found = re.findall(r'(?is)<ul\b[^>]*class="[^"]*flash-list[^"]*"[^>]*>(.*?)</ul>', markup)
    return page_text(" ".join(found)).replace("\u00d7", " ").strip()


def generate_hook(w: Web, slug: str):
    """POST generate-hook and follow the redirect. Returns (response, whole page text, flash text only)."""
    r = w.post(f"/partners/{pid_of(w, slug)}/generate-hook", {}, follow=True)
    assert r.status_code == 200, r.status_code
    return r, page_text(body(r)), flash_text(body(r))


def test_generate_hook_with_no_draft_gives_an_actionable_message_or_creates_the_draft(ew):
    seed_contacts(ew, [ADA])
    assert draft_count(ew, "ada") == 0
    r, page, text = generate_hook(ew, "ada")
    assert "check your settings" not in page.lower()
    if draft_count(ew, "ada") > 0:
        return  # creating the draft is an accepted way to be actionable
    # Otherwise the message must tell the person what to do next, not only that something is missing.
    assert re.search(r"(?is)\b(write|create|generate|run|make)\b.{0,60}\bdrafts?\b|\bdrafts?\b.{0,60}\b(write|create|generate|first)\b",
                     text), text[:800]
    assert text.strip() != "No draft found for this partner.", "the bare old message gives no next step"


FAILURE_CLASSES = {
    "missing_key": ("ANTHROPIC_API_KEY is not configured", r"(?i)\b(api key|key|credentials?|not configured)\b"),
    "rate_limit": ("Claude request failed: 429 rate limit exceeded, too many requests",
                   r"(?i)(rate.?limit|too many requests|429|quota)"),
    "network": ("Claude request failed: network unreachable, connection timed out",
                r"(?i)(network|connect|timed? ?out|timeout|unreachable|offline)"),
}


@pytest.mark.parametrize("cls", sorted(FAILURE_CLASSES))
def test_generate_hook_failure_names_the_failure_class_and_never_leaks_the_key(ew, monkeypatch, cls):
    from vcoutreach.web.llm.chain import ChainError

    message, expect = FAILURE_CLASSES[cls]
    seed_contacts(ew, [ADA])
    add_draft_row(ew, "ada")
    put_enrichment(ew, "ada", [
        fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/ml",
             ftype="podcast_appearance", conf=0.9),
    ])

    def boom(*a, **k):
        raise ChainError(f"{message} (key={FAKE_KEY}, url=https://api.example/v1?key={FAKE_KEY})")

    monkeypatch.setattr("vcoutreach.web.llm.hook_generator.call_llm", boom)
    r, page, text = generate_hook(ew, "ada")
    assert FAKE_KEY not in body(r), "the key must never appear in the response"
    assert FAKE_KEY not in page
    assert FAKE_KEY.split("FAKEKEY")[-1] not in body(r)
    assert re.search(expect, text), f"the flash should name the {cls} failure: {text[:800]}"


def test_the_three_failure_classes_read_differently(ew, monkeypatch):
    from vcoutreach.web.llm.chain import ChainError

    seed_contacts(ew, [ADA])
    add_draft_row(ew, "ada")
    put_enrichment(ew, "ada", [
        fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
             ftype="portfolio_company", conf=0.95),
        fact("Recent podcast appearance on Marlowe Weekly about seed investing", "https://src.example/ml",
             ftype="podcast_appearance", conf=0.9),
    ])
    flashes = {}
    for cls, (message, _) in FAILURE_CLASSES.items():
        def boom(*a, _m=message, **k):
            raise ChainError(_m)

        monkeypatch.setattr("vcoutreach.web.llm.hook_generator.call_llm", boom)
        r, page, text = generate_hook(ew, "ada")
        assert text, "a failed regeneration must flash something"
        flashes[cls] = text
    assert len(set(flashes.values())) == 3, f"each failure class needs its own message: {flashes}"


# ---------------------------------------------------------------------------------------------------------------
# 6. The /enrichment page
# ---------------------------------------------------------------------------------------------------------------

def enrichment_row_html(w: Web, name: str) -> str:
    markup = body(w.get("/enrichment", follow_redirects=True))
    for m in re.finditer(r"(?is)<tr\b.*?</tr>", markup):
        if name in m.group(0):
            return m.group(0)
    pytest.fail(f"no table row for {name} on /enrichment")


def hook_cell(row_html: str) -> str:
    m = re.search(r'(?is)<td\b[^>]*data-label="Hook"[^>]*>(.*?)</td>', row_html)
    assert m, "the row needs a Hook cell"
    return page_text(m.group(1)).strip()


@pytest.mark.parametrize("with_enrichment", [False, True])
def test_a_hook_with_no_hallucination_check_reads_not_checked(ew, with_enrichment):
    seed_contacts(ew, [ADA])
    add_draft_row(ew, "ada", hook="I came across your essay on how small teams ship weekly.", check=None)
    if with_enrichment:
        put_enrichment(ew, "ada", [
            fact("Portfolio company Quillfeather Robotics, led the seed round", "https://src.example/q",
                 ftype="portfolio_company"),
        ])
    row = enrichment_row_html(ew, "Ada Lovelace")
    cell = hook_cell(row)
    assert "Not checked" in page_text(row), page_text(row)
    assert cell != "?", "a bare question mark says nothing about why the hook is unverified"
