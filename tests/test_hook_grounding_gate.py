"""Hook grounding gate, retries, approve refusal, fact typing, model default and redirect policy.

Written from the behavior spec. No network. No dash characters in this file.
Spec item 1 names (hook_text, snippets, partner_fields); the real signature is
check_hook_grounding(hook, partner, enrichment_facts, sender_profile, facts) -> (bool, reason).
"""
from __future__ import annotations

import socket

import pytest

from web_helpers import *  # noqa: F401,F403
from test_enrichment_facts_reach_drafts import add_draft_row, fact, pid_of, seed_contacts, ADA, _db, flash_text
from web_helpers import body

from vcoutreach.web.llm.hook_grounding import check_hook_grounding
from vcoutreach.web.llm import hook_generator
from vcoutreach.web.enrichment.fact_extractor import extract_facts

PARTNER = {"partner_name": "Priya Raman", "firm": "Lattice Ventures"}
FACTS = [
    {"type": "essay_title", "text": 'Priya Raman wrote the essay "Boring Businesses Win" about durable software margins',
     "source_url": "https://lattice.example/blog/boring"},
    {"type": "podcast_appearance", "text": "Priya Raman on the Seed Stage Weekly podcast discussing pricing for vertical SaaS",
     "source_url": "https://podcasts.example/seed-stage"},
]
SENDER = {"traction_lines": [], "approved_claims": {"views": "about 10M organic views"}}
SENDER_FACTS = {"approved_claims": {"views": "about 10M organic views"}}


def check(hook, **kw):
    return check_hook_grounding(hook, PARTNER, FACTS, SENDER, SENDER_FACTS, **kw) if not kw else \
        check_hook_grounding(hook, PARTNER, FACTS, **kw)


# ---- 1. grounding ----------------------------------------------------------------------------------------------

def test_quoted_title_present_in_snippets_is_grounded():
    ok, reason = check('I enjoyed "Boring Businesses Win" and your take on durable software margins.')
    assert ok, reason


def test_invented_quoted_title_is_ungrounded():
    ok, reason = check('I loved "Why Every Founder Should Sell Shovels" and your take on margins.')
    assert not ok and reason


def test_invented_capitalized_title_is_ungrounded():
    ok, reason = check("Your piece The Shovel Seller Manifesto stayed with me.")
    assert not ok and reason


def test_you_wrote_about_a_real_topic_is_grounded():
    ok, reason = check("You wrote about durable software margins and boring businesses.")
    assert ok, reason


def test_you_wrote_about_an_unsupported_topic_is_ungrounded():
    ok, reason = check("You wrote about quantum networking hardware and satellite logistics.")
    assert not ok and reason


def test_you_back_an_unsupported_company_is_ungrounded():
    ok, reason = check("I saw you back Quillfeather Robotics and Marlowe Biotech lately.")
    assert not ok and reason


def test_invented_analogy_is_ungrounded():
    ok, reason = check("We are applying that same efficiency principle to outreach.")
    assert not ok and reason


def test_sender_number_matching_approved_wording_passes():
    ok, reason = check("We reached about 10M organic views last year.", sender_profile=SENDER, facts=SENDER_FACTS)
    assert ok, reason


def test_sender_number_with_plus_fails():
    ok, reason = check("We reached 10M+ organic views last year.", sender_profile=SENDER, facts=SENDER_FACTS)
    assert not ok and reason


def test_failure_reason_is_empty_when_grounded():
    ok, reason = check('I enjoyed "Boring Businesses Win".')
    assert ok and not reason


# ---- 2. retries ------------------------------------------------------------------------------------------------

GOOD = ('I enjoyed "Boring Businesses Win" and your view on durable software margins. '
        'It made me rethink how small teams can build calm and profitable companies without chasing every trend in the market.')
BAD = ('I loved "Why Every Founder Should Sell Shovels" and your view on durable software margins. '
       'It made me rethink how small teams can build calm and profitable companies without chasing every trend in the market.')


def fake_llm(monkeypatch, hooks):
    prompts = []
    seq = list(hooks)

    def call(prompt, model_pref="auto"):
        prompts.append(prompt)
        h = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"hook": h, "subject": "A quick note on durable software margins", "_model_used": "fake"}

    monkeypatch.setattr(hook_generator, "call_llm", call)
    return prompts


def gen():
    return hook_generator.generate_hook({**PARTNER, "sender_profile": {}, "sender_facts": {}}, FACTS,
                                        sender_profile=SENDER, facts=SENDER_FACTS)


def test_retry_feeds_reason_back_and_recovers(monkeypatch):
    prompts = fake_llm(monkeypatch, [BAD, GOOD])
    r = gen()
    assert r["hallucination_check"] == "grounded"
    assert r["attempts"] == 2 and len(prompts) == 2
    assert "Sell Shovels" in prompts[1] or "Previous" in prompts[1]
    assert prompts[1] != prompts[0]


def test_three_failures_keep_last_flagged_ungrounded(monkeypatch):
    prompts = fake_llm(monkeypatch, [BAD])
    r = gen()
    assert len(prompts) == 3
    assert r["hallucination_check"].startswith("ungrounded: ")
    assert len(r["hallucination_check"]) > len("ungrounded: ")
    assert r["hook"]


def test_clean_first_try_calls_model_once(monkeypatch):
    prompts = fake_llm(monkeypatch, [GOOD])
    r = gen()
    assert len(prompts) == 1 and r["hallucination_check"] == "grounded"


# ---- 3. approve route ------------------------------------------------------------------------------------------

@pytest.fixture
def aw(make_web):
    return make_web(sender=True)


def status_of(w, slug):
    c = _db(w)
    try:
        return c.execute("SELECT status FROM drafts WHERE partner_id=?", (pid_of(w, slug),)).fetchone()[0]
    finally:
        c.close()


def test_approve_refuses_ungrounded_hook_and_keeps_it_visible(aw):
    seed_contacts(aw, [ADA])
    hook = "You wrote Shovel Economics and we apply that same efficiency principle."
    add_draft_row(aw, "ada", hook=hook, check="ungrounded: unsupported title")
    r = aw.post(f"/partners/{pid_of(aw, 'ada')}/approve", {}, follow=True)
    assert r.status_code == 200
    assert status_of(aw, "ada") != "approved"
    text = flash_text(body(r)).lower()
    assert "cannot be approved" in text or "not in the saved research" in text
    assert "Shovel Economics" in body(r)


def test_approve_accepts_grounded_hook(aw):
    seed_contacts(aw, [ADA])
    add_draft_row(aw, "ada", hook=GOOD, check="grounded")
    aw.post(f"/partners/{pid_of(aw, 'ada')}/approve", {}, follow=True)
    assert status_of(aw, "ada") == "approved"


# ---- 4. fact typing --------------------------------------------------------------------------------------------

def types_for(url, title, snippet, name="Priya Raman"):
    res = extract_facts("web_search", {"partner_name": name, "results": [
        {"url": url, "title": title, "snippet": snippet}]})
    return {f["type"] for f in res}


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/priya-raman",
    "https://www.ycombinator.com/people/priya-raman",
    "https://a16z.com/author/priya-raman/",
    "https://www.crunchbase.com/person/priya-raman",
    "https://techcrunch.com/2025/03/02/priya-raman-leads-lattice-round/",
    "https://www.amazon.com/Seed-Stage-Priya-Raman/dp/123",
])
def test_non_podcast_sources_are_never_podcast_appearance(url):
    t = types_for(url, "Priya Raman podcast interview", "Priya Raman on the podcast episode about seed investing")
    assert "podcast_appearance" not in t


def test_essay_by_partner_is_essay_title():
    res = extract_facts("web_search", {"partner_name": "Priya Raman", "results": [{
        "url": "https://priyaraman.substack.com/p/boring-businesses-win",
        "title": "Why Boring Businesses Win Slowly", "snippet": "An essay by Priya Raman on durable software margins and how teams build calm companies."}]})
    assert any(f["type"] == "essay_title" for f in res), res


def test_essay_without_by_partner_is_public_writing_topic():
    res = extract_facts("web_search", {"partner_name": "Priya Raman", "results": [{
        "url": "https://priyaraman.substack.com/p/boring-businesses-win",
        "title": "Boring Businesses Win", "snippet": "An essay about durable software margins, cited by Priya Raman."}]})
    types = {f["type"] for f in res}
    assert "essay_title" not in types, res


# ---- 5. engine_settings default model --------------------------------------------------------------------------

def test_engine_settings_falls_back_to_hook_path_default_model(aw, monkeypatch):
    from vcoutreach.web import drafts_service
    from vcoutreach.web.llm import chain
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    s = drafts_service.engine_settings()
    assert s.anthropic_model and s.anthropic_model == chain._CLAUDE_HAIKU


def test_engine_settings_respects_explicit_model(aw, monkeypatch):
    from vcoutreach.web import drafts_service
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-custom-9")
    assert drafts_service.engine_settings().anthropic_model == "claude-custom-9"


# ---- 6. redirects ----------------------------------------------------------------------------------------------

class Resp:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {"Location": location} if location else {}


class FakeRequests:
    def __init__(self, chain):
        self.chain = dict(chain)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.chain.get(url, Resp(200))


@pytest.fixture
def redirects(monkeypatch):
    from vcoutreach import web
    from vcoutreach.web import vendor
    ips = {"intranet.example.org": "10.0.0.5"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ips.get(host, "93.184.216.34"), 0))])

    def install(chain):
        fr = FakeRequests(chain)
        monkeypatch.setattr(vendor, "require_dependency", lambda name: fr)
        return fr
    return install, vendor


def test_redirect_to_www_variant_is_followed(redirects):
    install, vendor = redirects
    fr = install({"https://acme.com/": Resp(301, "https://www.acme.com/")})
    r = vendor.public_requests_get("https://acme.com/")
    assert r.status_code == 200 and fr.calls[-1] == "https://www.acme.com/"


def test_three_redirects_then_success(redirects):
    install, vendor = redirects
    install({"https://acme.com/": Resp(301, "https://www.acme.com/a"),
             "https://www.acme.com/a": Resp(302, "/b"),
             "https://www.acme.com/b": Resp(302, "/c")})
    assert vendor.public_requests_get("https://acme.com/").status_code == 200


def test_fourth_redirect_is_refused(redirects):
    install, vendor = redirects
    install({"https://acme.com/": Resp(301, "/a"), "https://acme.com/a": Resp(301, "/b"),
             "https://acme.com/b": Resp(301, "/c"), "https://acme.com/c": Resp(301, "/d")})
    with pytest.raises(ValueError):
        vendor.public_requests_get("https://acme.com/")


@pytest.mark.parametrize("target", [
    "https://evil.example.net/",
    "http://acme.com/",
    "https://intranet.example.org/",
    "https://127.0.0.1/admin",
    "https://acme.com.evil.net/",
])
def test_unsafe_redirect_targets_are_refused(redirects, target):
    install, vendor = redirects
    fr = install({"https://acme.com/": Resp(302, target)})
    with pytest.raises(ValueError):
        vendor.public_requests_get("https://acme.com/")
    assert fr.calls == ["https://acme.com/"]


def test_essay_with_by_partner_in_title_is_essay_title():
    res = extract_facts("web_search", {"partner_name": "Priya Raman", "results": [{
        "url": "https://priyaraman.substack.com/p/x",
        "title": "Boring Businesses Win by Priya Raman", "snippet": "Durable software margins"}]})
    assert any(f["type"] == "essay_title" for f in res), res


# ---- 7. added: overlap threshold, quotes, titles, partner name, private hosts ---------------------------------

SNIP = [{"type": "essay_title", "source_url": "https://lattice.example/blog/pricing",
         "text": "Priya Raman wrote about pricing discipline for vertical software startups and churn"}]


def g(hook, facts=None):
    return check_hook_grounding(hook, PARTNER, SNIP if facts is None else facts, SENDER, SENDER_FACTS)


def test_overlap_nearly_all_words_is_grounded():
    ok, reason = g("You wrote about pricing discipline for vertical software startups.")
    assert ok, reason


def test_overlap_three_of_four_content_words_is_grounded():
    ok, reason = g("You wrote about pricing discipline vertical hardware.")
    assert ok, reason


def test_overlap_two_of_four_content_words_is_ungrounded():
    ok, reason = g("You wrote about pricing discipline orbital hardware.")
    assert not ok and reason


def test_overlap_about_half_is_ungrounded():
    ok, reason = g("You wrote about pricing strategy, hiring plans, orbital hardware, startups.")
    assert not ok and reason


def test_quoted_phrase_absent_from_snippets_is_ungrounded_even_if_rest_fine():
    ok, reason = g('You wrote about pricing discipline for vertical software startups, "ship it ugly".')
    assert not ok and reason


def test_quoted_phrase_present_in_snippets_is_grounded():
    facts = [{"type": "essay_title", "source_url": "https://lattice.example/blog/pricing",
              "text": 'Priya Raman wrote "pricing discipline" for vertical software startups'}]
    ok, reason = g('You wrote "pricing discipline" for vertical software startups.', facts)
    assert ok, reason


def test_two_word_title_not_in_snippets_is_ungrounded():
    ok, reason = g("Your piece Quiet Compounders stayed with me.")
    assert not ok and reason


def test_two_word_title_in_snippet_is_grounded():
    facts = [{"type": "essay_title", "source_url": "https://lattice.example/blog/qc",
              "text": "Priya Raman wrote Quiet Compounders about durable software companies"}]
    ok, reason = g("You wrote Quiet Compounders about durable software companies.", facts)
    assert ok, reason


@pytest.mark.parametrize("hook", [
    "Congrats on the round.",
    "Thanks for writing about pricing discipline for vertical software startups.",
    "Congrats on the round, loved your pricing discipline for vertical software startups.",
])
def test_sentence_initial_capital_word_is_not_an_invented_title(hook):
    ok, reason = g(hook)
    assert ok, reason


def test_partner_own_name_is_not_a_supporting_claim_word():
    facts = [{"type": "current_role", "source_url": "https://lattice.example/team",
              "text": "Priya Raman is a partner at Lattice Ventures"}]
    ok, reason = g("You back Priya Raman style founders.", facts)
    assert not ok and reason


def test_private_and_loopback_hosts_are_rejected_directly(redirects):
    install, vendor = redirects
    fr = install({})
    for host in ("10.0.0.5", "192.168.1.10", "127.0.0.1", "[::1]", "169.254.169.254"):
        with pytest.raises(ValueError):
            vendor.public_requests_get("https://%s/" % host)
        with pytest.raises(ValueError):
            vendor.validated_url("https://%s/" % host)
    assert fr.calls == []


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.10", "127.0.0.1", "[::1]", "169.254.169.254"])
def test_private_ip_literal_redirect_targets_are_refused(redirects, host):
    install, vendor = redirects
    fr = install({"https://acme.com/": Resp(302, "https://%s/latest/meta-data" % host)})
    with pytest.raises(ValueError):
        vendor.public_requests_get("https://acme.com/")
    assert fr.calls == ["https://acme.com/"]


def test_weak_podcast_marker_on_blog_is_not_podcast_appearance():
    t = types_for("https://priyaraman.substack.com/p/notes", "Notes on pricing",
                  "Priya Raman interview notes on pricing for vertical SaaS")
    assert "podcast_appearance" not in t


def test_blog_merely_mentioning_listen_is_not_podcast_appearance():
    t = types_for("https://blog.lattice.example/posts/pricing", "Pricing notes",
                  "Priya Raman shares pricing notes. You can listen to her talk about seed investing.")
    assert "podcast_appearance" not in t


def test_clear_podcast_page_is_podcast_appearance():
    t = types_for("https://podcasts.apple.com/us/podcast/seed-stage-weekly/id123",
                  "Seed Stage Weekly Podcast, episode 42",
                  "Seed Stage Weekly Podcast, episode 42, hosted by Dana Cole, with guest Priya Raman on vertical SaaS pricing")
    assert "podcast_appearance" in t


def test_essay_never_typed_essay_title_without_by_partner():
    t = types_for("https://priyaraman.substack.com/p/margins", "Durable Margins",
                  "An essay on durable software margins. Priya Raman is quoted in the comments.")
    assert "essay_title" not in t


# ---- 8. added: mutation-gap coverage ---------------------------------------------------------------------------

def test_invented_quote_without_claim_wording_is_ungrounded():
    ok, reason = g('Loved the "ship it ugly" mindset.')
    assert not ok and reason


def test_invented_two_word_title_mid_sentence_is_ungrounded():
    ok, reason = g("We admired Quiet Compounders at the conference.")
    assert not ok and reason


def test_firm_name_in_claim_is_not_required_in_snippets():
    facts = [{"type": "essay_title", "source_url": "https://lattice.example/blog/pricing",
              "text": "Pricing discipline for vertical software startups"}]
    ok, reason = g("Your work at Lattice Ventures on pricing discipline.", facts)
    assert ok, reason


def test_trigger_verb_itself_is_not_required_in_snippets():
    facts = [{"type": "essay_title", "source_url": "https://lattice.example/blog/pricing",
              "text": "Pricing discipline"}]
    ok, reason = g("You published pricing discipline.", facts)
    assert ok, reason


def test_trigger_with_no_specific_claim_is_grounded():
    ok, reason = g("You wrote about this.")
    assert ok, reason


def test_weak_markers_alone_on_non_podcast_site_are_not_podcast_appearance():
    t = types_for("https://lattice.example/team/priya", "Priya Raman profile",
                  "Priya Raman discusses pricing in an interview on seed investing")
    assert "podcast_appearance" not in t


def test_essay_without_by_partner_is_not_essay_title_even_with_other_author():
    t = types_for("https://priyaraman.substack.com/p/margins", "Why Durable Margins Beat Growth Hype",
                  "An essay about durable software margins. Edited by Dana Cole, with a quote from Priya Raman.")
    assert "essay_title" not in t


@pytest.mark.parametrize("snippet", ["An essay BY PRIYA RAMAN on margins and teams.",
                                     "An essay by priya raman on margins and teams."])
def test_by_partner_match_ignores_case(snippet):
    t = types_for("https://priyaraman.substack.com/p/margins", "Why Durable Margins Beat Growth Hype", snippet)
    assert "essay_title" in t


@pytest.mark.parametrize("host", ["intranet.local", "wiki.internal", "app.localhost"])
def test_local_style_hostnames_are_rejected(host):
    from vcoutreach.web import vendor
    with pytest.raises(ValueError):
        vendor.validated_url("https://%s/" % host)


def test_port_zero_is_rejected_and_valid_ports_accepted():
    from vcoutreach.web import vendor
    with pytest.raises(ValueError):
        vendor.validated_url("https://acme.com:0/")
    assert vendor.validated_url("https://acme.com:443/")
    assert vendor.validated_url("https://acme.com:65535/")
    with pytest.raises(ValueError):
        vendor.validated_url("https://acme.com:65536/")
