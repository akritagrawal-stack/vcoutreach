"""F2 to F8: the P1 audit fixes (spec: outputs/ux_pass_20261021/fix_spec.md).

A new user imports a CSV, clicks Write drafts, reads the editor, approves, and tries to send a test with Gmail not
connected. Everything goes through the Flask test client in TEST send mode, no network.
"""
from __future__ import annotations

import re

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures + autouse env isolation)
from web_helpers import (
    ADA, MINA, TEST_INBOX, Web, body, contact_ref, create_web_app, csv_row, export_rows, page_text,
    read_draft_file, seed_contacts,
)

from audit_fix_helpers import dom, find_form, flashes, form_data, has_dash, submit_form, words, write_drafts

ELI = csv_row(id="eli", name="Eli Stone", email="eli@example.com", org="Stone Labs", role="Founder",
              facts="Wrote an essay on accessibility in developer tools", source_urls="https://example.com/a11y")
RAJ = csv_row(id="raj", name="Raj Patel", email="raj@example.com", org="Patel Fund", role="Partner",
              facts="Hosts a weekly podcast about open source funding", source_urls="https://example.com/pod")


def _env():
    return {"SENDER_EMAIL": "sender@example.com", "TEST_RECIPIENT_EMAIL": TEST_INBOX}


def _bare_sender():
    """A sender who has not written any product or traction lines (the audit's 18 word draft)."""
    return dict(product_lines=[], traction_lines=[], approved_claims={})


def _lower(text: str) -> str:
    return " ".join(text.split()).lower()


# =================================================================================================================
# F2: the offline template draft is grammatical and lint clean
# =================================================================================================================


@pytest.mark.parametrize("contact", [ELI], ids=["wrote-an-essay"])
def test_template_draft_does_not_paste_the_fact_into_a_sentence_stem(make_web, contact):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [contact])
    write_drafts(web)
    text = _lower(read_draft_file(web.ws, contact["id"]).body)
    fact = _lower(contact["facts"])
    stem = re.compile(r"(came across|noticed|saw|read|loved|enjoyed|admire[d]?)\s+" + re.escape(fact))
    assert not stem.search(text), f"fact pasted into a sentence stem: {text!r}"


@pytest.mark.parametrize("contact", [ELI], ids=["wrote-an-essay"])
def test_template_draft_for_a_contact_with_a_fact_has_50_to_130_words(make_web, contact):
    web = make_web(_env(), sender=True, **_bare_sender())
    seed_contacts(web, [contact])
    write_drafts(web)
    draft = read_draft_file(web.ws, contact["id"])
    count = words(draft.body)
    assert 50 <= count <= 130, f"{count} words: {draft.body!r}"


def test_template_draft_with_the_default_sender_also_passes_lint(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ELI, RAJ])
    write_drafts(web)
    for slug in ("eli", "raj"):
        page = dom(web, f"/crm/drafts/{slug}").get_text(" ", strip=True)
        assert "No problems found" in page, (slug, page[-900:])
        assert 50 <= words(read_draft_file(web.ws, slug).body) <= 130, slug


def test_template_draft_subject_is_not_a_bare_placeholder(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ELI])
    write_drafts(web)
    subject = read_draft_file(web.ws, "eli").subject
    assert subject.strip()
    assert "{" not in subject and "}" not in subject, subject
    assert len(subject.split()) >= 2, subject


def test_drafts_never_carry_the_credibility_line(make_web):
    web = make_web(_env(), sender=True, **_bare_sender())
    seed_contacts(web, [ELI, RAJ, ADA])
    write_drafts(web)
    root = dom(web, "/crm/drafts/eli")
    editor_text = " ".join(
        [t.get_text() for t in root.select("textarea")] + [i.get("value", "") for i in root.select("input")]
    )
    for slug in ("eli", "raj", "ada"):
        draft = read_draft_file(web.ws, slug)
        blob = _lower(draft.subject + " " + draft.body)
        assert "50+ meetings" not in blob and "$600k" not in blob and "600k" not in blob, (slug, blob)
    blob = _lower(editor_text)
    assert "50+ meetings" not in blob and "$600k" not in blob, blob


def test_template_draft_has_no_dashes(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ELI])
    write_drafts(web)
    draft = read_draft_file(web.ws, "eli")
    assert not has_dash(draft.subject + draft.body)


# =================================================================================================================
# F3: one consistent grounding verdict
# =================================================================================================================

UNGROUNDED_BODY = (
    "Hi Ada,\n\n"
    "Congratulations on selling Northwind Labs to Contoso Corporation for $40 million in 2019, and on the Series B "
    "you raised from Sequoia last spring. I also read your essay on how small teams ship weekly with great interest. "
    "We build a tool that turns a spreadsheet of contacts into plain drafts a founder can actually send. Last month "
    "we onboarded a handful of design partners.\n\n"
    "If a short call makes sense, here is my calendar link: https://cal.example.com/sam\n\n"
    "Sam Rivera\nFounder, Lumenlist\nhttps://lumenlist.example.com"
)


def _save_ungrounded(web, extra=None):
    """Open Ada's editor, replace the body with an invented claim, press Save and check."""
    root = dom(web, "/crm/drafts/ada")
    form = find_form(root, action_contains="/crm/drafts/ada", textarea_like="body")
    assert form is not None, "no editor form"
    overrides = {"body": UNGROUNDED_BODY}
    overrides.update(extra or {})
    return submit_form(web, form, overrides, fallback_path="/crm/drafts/ada")


def test_blocked_draft_does_not_also_say_no_lint_issues(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    page = body(_save_ungrounded(web))
    text = page_text(page)
    assert "blocked" in text.lower(), "setup: the invented claim should have been blocked"
    assert "No problems found" not in text, "blocked draft also claims it is clean"


def test_blocked_draft_tells_the_user_how_to_fix_it(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    text = _lower(page_text(body(_save_ungrounded(web))))
    assert "blocked" in text, "setup: the invented claim should have been blocked"
    assert "stored fact" in text, "no guidance that claims must reuse a stored fact"
    assert "quote" in text or "add the fact" in text, "no guidance to add or quote the fact"


# =================================================================================================================
# F4: approval is reachable
# =================================================================================================================


def _approve_forms(root):
    return [f for f in root.select("form") if (f.get("action") or "").rstrip("/").endswith("/approve")]


def _approval_reachable(web, root):
    """The approve form is on this page, or one link away behind a link that talks about approving."""
    if _approve_forms(root):
        return True
    for link in root.select("a[href]"):
        href = link.get("href", "")
        label = (link.get_text(" ", strip=True) + " " + link.get("title", "") + " " + link.get("aria-label", "")).lower()
        if "approv" not in label or href.startswith("#") or href.startswith("http"):
            continue
        target = web.get(href.split("#")[0])
        if target.status_code == 200 and _approve_forms(dom_of(target)):
            return True
    return False


def dom_of(response):
    from audit_fix_helpers import soup

    return soup(body(response))


def _plan_row(web, name):
    for tr in dom(web, "/crm/send-plan").select("tr"):
        if name in tr.get_text(" ", strip=True):
            return tr
    pytest.fail(f"{name} is not on the send plan")


def test_editor_of_a_clean_draft_offers_a_way_to_approve(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    root = dom(web, "/crm/drafts/ada")
    assert read_draft_file(web.ws, "ada").status == "ok", "setup: Ada's template draft should be grounded"
    assert _approval_reachable(web, root), "the editor has neither an Approve button nor a link to one"


def test_editor_of_a_blocked_draft_does_not_offer_approve(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [MINA])
    write_drafts(web)
    root = dom(web, "/crm/drafts/mina")
    assert "blocked" in root.get_text(" ", strip=True).lower(), "setup: Mina has no facts and must be blocked"
    assert _approve_forms(root) == []
    for button in root.select("button, input[type=submit]"):
        label = (button.get_text(" ", strip=True) or button.get("value", "")).lower()
        if "approve" in label:
            assert button.has_attr("disabled"), f"enabled approve control on a blocked draft: {label!r}"


def test_approving_from_the_editor_clears_the_send_plan_hold(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    assert "not approved" in _plan_row(web, "Ada Lovelace").get_text(" ", strip=True).lower(), "setup"
    root = dom(web, "/crm/drafts/ada")
    forms = _approve_forms(root)
    if not forms:
        link = next((a for a in root.select("a[href]") if "approv" in a.get_text(" ", strip=True).lower()), None)
        assert link is not None, "no Approve form or link in the editor"
        forms = _approve_forms(dom(web, link["href"].split("#")[0]))
    assert forms, "could not reach an approve form"
    submit_form(web, forms[0], {}, fallback_path="")
    assert "not approved" not in _plan_row(web, "Ada Lovelace").get_text(" ", strip=True).lower()


def test_send_plan_not_approved_row_links_to_the_place_to_approve(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    row = _plan_row(web, "Ada Lovelace")
    assert "not approved" in row.get_text(" ", strip=True).lower(), "setup"
    hrefs = [a.get("href", "") for a in row.select("a[href]")]
    ada_links = [h for h in hrefs if "/crm/drafts/ada" in h or "/partners/" in h or "/crm/" in h]
    assert ada_links, f"the 'Draft is not approved' row has no link to approve: {hrefs}"
    reached = False
    for href in ada_links:
        target = web.get(href.split("#")[0])
        if target.status_code == 200 and _approval_reachable(web, dom_of(target)):
            reached = True
    assert reached, f"none of {ada_links} leads to an Approve action"


def test_save_and_mark_reviewed_approves_or_points_to_approval(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    root = dom(web, "/crm/drafts/ada")
    form = find_form(root, action_contains="/crm/drafts/ada", textarea_like="body")
    assert form is not None
    page = body(submit_form(web, form, {"action": "review"}, fallback_path="/crm/drafts/ada"))
    approved = "not approved" not in _plan_row(web, "Ada Lovelace").get_text(" ", strip=True).lower()
    pointed = any("approv" in text.lower() and links for text, links in flashes(page))
    assert approved or pointed, (
        "Save and mark reviewed neither approved the draft nor told the user, with a link, where to approve it: "
        f"{flashes(page)}"
    )


# =================================================================================================================
# F5: one source of truth for facts
# =================================================================================================================


def _enrichment_fact_count(web, name):
    root = dom(web, "/enrichment")
    headers = [th.get_text(" ", strip=True) for th in root.select("table thead th")]
    index = next((i for i, h in enumerate(headers) if h.lower().startswith("facts")), None)
    assert index is not None, f"no Facts column in {headers}"
    for tr in root.select("table tbody tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.select("td")]
        if cells and name in cells[0]:
            return int(re.sub(r"\D", "", cells[index]) or 0)
    pytest.fail(f"{name} is not on /enrichment")


def test_manual_facts_count_on_the_enrichment_page(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA, MINA])
    assert _enrichment_fact_count(web, "Ada Lovelace") >= 1, "Ada has two facts from the CSV"
    assert _enrichment_fact_count(web, "Mina Park") == 0


def test_manual_facts_are_not_called_missing_on_the_partner_page(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, ADA["email"])
    text = page_text(body(web.get(f"/partners/{ref}")))
    assert "No enrichment data yet" not in text
    assert "your essay on how small teams ship weekly" in text


def test_regenerate_hook_works_for_a_contact_with_only_manual_facts(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, ADA["email"])
    response = web.post(f"/partners/{ref}/generate-hook", {}, follow=True)
    assert response.status_code == 200
    text = _lower(page_text(body(response)))
    assert "no enrichment facts yet" not in text, "hook generation ignored the contact's own facts"
    assert "cannot generate a hook without enrichment facts" not in text


def test_no_facts_hook_error_appears_once(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [MINA])
    ref = contact_ref(web, MINA["email"])
    response = web.post(f"/partners/{ref}/generate-hook", {}, follow=True)
    text = _lower(page_text(body(response)))
    assert text.count("without enrichment facts") + text.count("no enrichment facts yet") >= 1, "setup: expected an error"
    assert text.count("enrich the contact first") == 1, "the no-facts error is shown more than once"


# =================================================================================================================
# F6: SearXNG Test connection binds regardless of script load order
# =================================================================================================================


def _scripts_with_searxng_binding(web):
    root = dom(web, "/settings")
    assert root.select_one("[data-searxng-form] [data-searxng-test]") is not None, "setup: no SearXNG test button"
    found = []
    for tag in root.select("script[src]"):
        src = tag["src"]
        if not src.startswith("/static/"):
            continue
        response = web.get(src)
        source = body(response)
        if "data-searxng-test" in source:
            found.append((tag, source))
    for tag in root.select("script:not([src])"):
        if "data-searxng-test" in (tag.string or ""):
            found.append((tag, tag.string or ""))
    return found


def test_searxng_test_handler_is_not_bound_at_script_load_time(make_web):
    web = make_web(_env(), sender=True)
    scripts = _scripts_with_searxng_binding(web)
    assert scripts, "no script binds the SearXNG Test connection button"
    for tag, source in scripts:
        deferred = tag.has_attr("defer") or tag.get("type") == "module"
        index = source.find("data-searxng")
        before = source[max(0, index - 700):index]
        waits_for_dom = "DOMContentLoaded" in before or "readyState" in before
        delegated = re.search(r"document\.addEventListener\(\s*['\"]click['\"]", source[max(0, index - 300):index + 1500])
        assert deferred or waits_for_dom or delegated, (
            "The SearXNG handler runs while the script loads, before the button exists, so the click does nothing"
        )



# =================================================================================================================
# F7: send test with no draft
# =================================================================================================================


def test_send_test_without_a_draft_returns_to_the_page_with_a_flash(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, ADA["email"])
    response = web.post(f"/partners/{ref}/send-test", {}, follow=True)
    assert response.status_code == 200, "send test without a draft must not be an error page"
    assert any("write a draft first" in t.lower() for t, _ in flashes(body(response))), flashes(body(response))


def test_contact_page_without_a_draft_has_one_disabled_send_control_with_a_hint(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    ref = contact_ref(web, ADA["email"])
    root = dom(web, f"/crm/{ref}")
    send_forms = [f for f in root.select("form")
                  if (f.get("action") or "").rstrip("/").endswith(("/send-test", "/send"))]
    assert len(send_forms) <= 1, f"duplicate near-identical send buttons: {[f.get('action') for f in send_forms]}"
    assert send_forms, "the contact page lost its send control"
    for form in send_forms:
        buttons = form.select("button, input[type=submit]")
        assert buttons and all(b.has_attr("disabled") for b in buttons), "send is clickable with no draft"
    assert "write a draft first" in _lower(root.get_text(" ", strip=True))


def test_contact_page_with_a_draft_keeps_exactly_one_enabled_send_test_control(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    write_drafts(web)
    ref = contact_ref(web, ADA["email"])
    root = dom(web, f"/crm/{ref}")
    send_forms = [f for f in root.select("form")
                  if (f.get("action") or "").rstrip("/").endswith(("/send-test", "/send"))]
    assert len(send_forms) == 1, [f.get("action") for f in send_forms]
    assert not any(b.has_attr("disabled") for b in send_forms[0].select("button"))


# =================================================================================================================
# F8: friendly CSRF page and a per-workspace session cookie
# =================================================================================================================


def test_invalid_csrf_token_shows_a_styled_session_expired_page(make_web):
    web = make_web(_env(), sender=True)
    response = web.post(web.url("drafts_write"), {"provider": "template"}, csrf="not-a-real-token")
    assert response.status_code == 400
    assert "html" in response.content_type
    text = _lower(page_text(body(response)))
    assert "expired" in text
    assert "reload" in text
    assert dom_of(response).select("a[href]"), "no link back"


def test_invalid_csrf_does_not_change_anything(make_web):
    web = make_web(_env(), sender=True)
    seed_contacts(web, [ADA])
    web.post(web.url("drafts_write"), {"provider": "template"}, csrf="not-a-real-token")
    drafts = web.ws / "drafts"
    assert not drafts.exists() or not list(drafts.rglob("*.md"))


def _session_cookie_names(web):
    return {name for name, _ in web.cookie_pairs()}


def test_both_sessions_survive_in_one_browser(make_web):
    first = make_web(_env(), sender=True, dirname="work-alpha")
    second = make_web(_env(), sender=True, dirname="work-beta")
    jar = first.cookie_pairs() + second.cookie_pairs()
    assert first.raw("get", "/crm", cookies=jar).status_code == 200, "first instance lost its session"
    assert second.raw("get", "/crm", cookies=jar).status_code == 200, "second instance lost its session"


def test_the_same_workspace_keeps_its_cookie_name_across_restarts(make_web, tmp_path):
    first = make_web(_env(), sender=True, dirname="work-gamma")
    app = create_web_app(first.ws, None)
    again = Web(app, first.ws, None)
    again.login()
    assert _session_cookie_names(first) == _session_cookie_names(again)
