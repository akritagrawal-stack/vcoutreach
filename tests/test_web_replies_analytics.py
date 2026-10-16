"""Replies (added by hand) and analytics (funnel, reply rate with a Wilson interval, tags, draft quality).

SPEC_V3 section 2, SPEC_V2 section 2, SPEC.md sections 4 and 5. Known Wilson 95 percent interval values
are fixed numbers from the formula, not recomputed by the code under test.
No dash characters appear in this file.
"""
from __future__ import annotations

import re

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import (
    ADA, GRACE, LINUS, REPLY_LABELS, SENDER_EMAIL, STAGES, TEST_INBOX, Web, analyze, approve_drafts,
    approved_draft_ids, body, contact_form_fields, contact_ref, csv_row, csv_text, engine_draft_all, export_rows,
    go_live, import_csv, mark_verified, page_text, send_batch, seed_contacts, set_stage,
)

ACTIVITY_NAMES_KIND = ("kind", "activity_kind", "type", "activity_type")
ACTIVITY_NAMES_TEXT = ("body", "note", "text", "summary", "details", "snippet", "subject")


def add_reply(w: Web, sender: str, subject: str, text: str, follow: bool = True):
    fields = {}
    for name in ("from_email", "from", "email", "sender"):
        fields[name] = sender
    fields["subject"] = subject
    for name in ("body", "message", "text"):
        fields[name] = text
    return w.post(w.url("reply_add"), fields, follow=follow)


def reply_ids(w: Web):
    ids = []
    for found in re.findall(r"/replies/(\d+)", w.html("/replies")):
        if found not in ids:
            ids.append(found)
    return ids


def unread_badge(w: Web, path: str = "/crm") -> int:
    markup = w.html(path)
    for match in re.finditer(r'(?is)<a\b[^>]*href="/replies"[^>]*>(.*?)</a>', markup):
        digits = re.findall(r"\d+", re.sub(r"<[^>]+>", " ", match.group(1)))
        if digits:
            return int(digits[-1])
    return 0


def selected_label(w: Web, rid: str):
    markup = w.html("/replies")
    for chunk in markup.split("<form")[1:]:
        action = re.match(r'[^>]*action="([^"]*)"', chunk)
        if action and re.search(rf"/replies/{rid}/(update-)?label", action.group(1)):
            for option in re.finditer(r'(?is)<option\b([^>]*)>', chunk):
                if re.search(r"\bselected\b", option.group(1)):
                    value = re.search(r'value="([^"]*)"', option.group(1))
                    return value.group(1) if value else None
    return None


def label_post(w: Web, rid: str, label: str):
    return w.post(w.url("reply_label", id=rid), {"label": label, "user_label": label}, follow=True)


# SPEC.md section 4 names the labels interested, needs_info, pass, out_of_office, bounce, unlabeled; the kept old app
# uses its own set (auto_reply, bounce, interested, needs_more_info, other, out_of_office, pass). The labels both share
# are required; the rest are taken from the page itself (SPEC_QUESTIONS.md Q2).
COMMON_LABELS = ["interested", "pass", "out_of_office", "bounce"]


def label_options(w: Web, rid: str):
    markup = w.html("/replies")
    for chunk in markup.split("<form")[1:]:
        action = re.match(r'[^>]*action="([^"]*)"', chunk)
        if action and re.search(rf"/replies/{rid}/(update-)?label", action.group(1)):
            return [v for v in re.findall(r'<option\b[^>]*value="([^"]+)"', chunk)]
    return []


def shows_number(text: str, *alternatives: str) -> bool:
    return any(re.search(rf"(?<![\d.]){re.escape(a)}(?![\d])", text) for a in alternatives)


# ---------------------------------------------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------------------------------------------

def test_the_replies_page_has_an_add_a_reply_form(web):
    markup = web.html("/replies")
    for field in ("from_email", "subject", "body"):
        assert re.search(rf'name="{field}"', markup), field


def test_a_reply_is_matched_to_its_contact_by_email_ignoring_case(web):
    seed_contacts(web, [ADA])
    add_reply(web, "ADA@Example.com", "Re: your note", "Sounds interesting, tell me more.")
    assert "Ada Lovelace" in web.text("/replies")
    ref = contact_ref(web, "ada@example.com")
    assert "Re: your note" in web.text(f"/crm/{ref}")


def test_the_replies_page_shows_every_reply_matched_or_not(web):
    seed_contacts(web, [ADA, GRACE])
    senders = ["ada@example.com", "grace@example.com", "stranger@example.net", "other@example.org"]
    for i, sender in enumerate(senders):
        add_reply(web, sender, f"Subject number {i}", f"Body number {i}")
    text = web.text("/replies")
    for i, sender in enumerate(senders):
        assert f"Subject number {i}" in text, sender
    assert "stranger@example.net" in text


def test_a_reply_without_a_valid_sender_address_is_refused(web):
    r = add_reply(web, "not-an-email", "Re: hello UNIQUE-SUBJECT-MARK", "some body")
    assert r.status_code < 500
    assert "UNIQUE-SUBJECT-MARK" not in web.text("/replies")


def test_a_reply_with_no_text_is_not_a_server_error(web):
    # The specs do not say a body is mandatory (SPEC_QUESTIONS.md Q1): either answer is fine, a crash is not.
    r = add_reply(web, "ada@example.com", "Re: hello", "")
    assert r.status_code < 500 and "Traceback" not in body(r)
    assert web.get("/replies").status_code == 200


def test_hostile_reply_text_is_escaped(web):
    add_reply(web, "ada@example.com", "<script>alert(1)</script>", '<img src=x onerror="alert(2)">')
    markup = web.html("/replies")
    assert "<script>alert(1)</script>" not in markup and "<img src=x" not in markup
    assert analyze(markup).inline_scripts == 0 and analyze(markup).handlers == []


def test_every_reply_label_can_be_set_and_is_remembered(web):
    add_reply(web, "ada@example.com", "Re: note", "text")
    rid = reply_ids(web)[0]
    offered = label_options(web, rid)
    assert set(COMMON_LABELS) <= set(offered), f"the label select offers {offered}"
    for label in offered:
        label_post(web, rid, label)
        assert selected_label(web, rid) == label, label


def test_an_unknown_label_is_refused_and_the_old_label_stays(web):
    add_reply(web, "ada@example.com", "Re: note", "text")
    rid = reply_ids(web)[0]
    label_post(web, rid, "interested")
    r = label_post(web, rid, "spam")
    assert r.status_code < 500, "an unknown label is a refusal, not a server error"
    assert selected_label(web, rid) == "interested"


def test_the_unread_badge_counts_unreviewed_replies_and_the_reviewed_toggle_changes_it(web):
    add_reply(web, "a@example.com", "First", "text")
    add_reply(web, "b@example.com", "Second", "text")
    first, second = reply_ids(web)[:2]
    assert unread_badge(web) == 2
    web.post(web.url("reply_reviewed", id=first), {}, follow=True)
    assert unread_badge(web) == 1
    web.post(web.url("reply_reviewed", id=second), {}, follow=True)
    assert unread_badge(web) == 0


def test_the_reviewed_control_is_a_toggle(web):
    """SPEC_V2 section 2 and SPEC.md section 5 say "reviewed toggle": a second click makes the reply unread again."""
    add_reply(web, "a@example.com", "First", "text")
    first = reply_ids(web)[0]
    assert unread_badge(web) == 1
    web.post(web.url("reply_reviewed", id=first), {}, follow=True)
    assert unread_badge(web) == 0
    web.post(web.url("reply_reviewed", id=first), {}, follow=True)
    assert unread_badge(web) == 1


def test_a_single_reply_page_shows_its_text_and_missing_replies_are_not_server_errors(web):
    add_reply(web, "ada@example.com", "Re: note", "A distinctive reply sentence.")
    rid = reply_ids(web)[0]
    assert "A distinctive reply sentence." in web.text(f"/replies/{rid}")
    assert web.get("/replies/987654").status_code in (302, 303, 404), "a missing reply is a redirect or a 404"
    assert web.post(web.url("reply_label", id=987654), {"label": "pass"}).status_code in (302, 303, 404)
    assert web.post(web.url("reply_reviewed", id=987654), {}).status_code in (302, 303, 404)


# ---------------------------------------------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("successes, n, low, high", [
    (0, 0, 0.0, 0.0),
    (5, 10, 0.2366, 0.7634),
    (1, 10, 0.0179, 0.4042),
    (0, 10, 0.0, 0.2775),
])
def test_the_wilson_interval_matches_known_values(successes, n, low, high):
    from vcoutreach.web.crm_analytics import _wilson_interval

    got_low, got_high = _wilson_interval(successes, n)
    assert got_low == pytest.approx(low, abs=0.001)
    assert got_high == pytest.approx(high, abs=0.001)


def test_analytics_on_an_empty_workspace_is_an_explained_empty_state_with_no_broken_numbers(web):
    r = web.get(web.url("analytics"))
    assert r.status_code == 200
    text = page_text(r)
    assert text and not re.search(r"(?i)\bnan\b|\binf(inity)?\b|division|traceback", text)


def test_the_funnel_counts_contacts_per_stage(web):
    rows = [csv_row(id=f"f{i}", name=f"Person {i}", email=f"f{i}@example.com") for i in range(6)]
    seed_contacts(web, rows)
    for i, stage in ((0, "warm"), (1, "warm"), (2, "contacted"), (3, "meeting")):
        set_stage(web, f"f{i}@example.com", stage)
    text = web.text(web.url("analytics"))
    expected = {"prospect": 2, "contacted": 1, "warm": 2, "meeting": 1, "passed": 0, "hold": 0}
    for stage, count in expected.items():
        assert re.search(rf"(?i){stage}\D{{0,40}}?\b{count}\b", text), f"{stage} should show {count}: {text[:600]}"


def test_reply_rate_per_batch_shows_the_rate_and_the_wilson_interval(make_web):
    """Ten contacts imported as batch-a, all sent LIVE through the app, five reply: 50 percent, Wilson 23.7 to 76.3."""
    w = make_web({"SENDER_EMAIL": SENDER_EMAIL, "TEST_RECIPIENT_EMAIL": TEST_INBOX}, sender=True)
    rows = [csv_row(id=f"b{i}", name=f"Batch Person {i}", email=f"b{i}@example.com",
                    facts=f"your note number {i} on small teams", source_urls=f"https://example.com/n{i}")
            for i in range(10)]
    import_csv(w, csv_text(rows), filename="batch-a.csv")
    w.post(w.url("drafts_write"), {"provider": "template"}, follow=True)
    approve_drafts(w, [r["id"] for r in rows])
    mark_verified(w)
    go_live(w)
    send_batch(w, "SEND BATCH OF 10", ids=approved_draft_ids(w))
    assert len(w.transport.calls) == 10, "test setup: the ten drafts must go out through the app"
    for row in rows[:5]:
        add_reply(w, row["email"], "Re: hello", "Yes please")
    text = w.text(w.url("analytics"))
    assert "batch-a" in text
    assert shows_number(text, "50", "50.0", "0.5", "0.50"), "reply rate of 5 in 10"
    assert shows_number(text, "23.7", "24", "0.237", "0.24"), "Wilson lower bound"
    assert shows_number(text, "76.3", "76", "0.763", "0.76"), "Wilson upper bound"


def test_a_batch_with_nothing_sent_does_not_break_the_page(web):
    import_csv(web, csv_text([csv_row(id="z1", name="Zero Sent", email="z1@example.com")]), filename="unsent.csv")
    r = web.get(web.url("analytics"))
    assert r.status_code == 200
    assert not re.search(r"(?i)\bnan\b|division|traceback", page_text(r))


def test_contacts_by_tag_are_counted(web):
    for row, tags in ((ADA, "alpha,beta"), (GRACE, "alpha"), (LINUS, "")):
        web.post(web.url("contact_new"), {**contact_form_fields(row), "tags": tags}, follow=True)
    assert len(export_rows(web)) == 3
    text = web.text(web.url("analytics"))
    assert re.search(r"(?i)alpha\D{0,30}?\b2\b", text) and re.search(r"(?i)beta\D{0,30}?\b1\b", text)


def test_draft_quality_shows_ok_and_blocked_counts_and_the_top_lint_code(send_web):
    thin = {**LINUS, "id": "thin", "email": "thin@example.com", "name": "Thin Person", "facts": ""}
    w = send_web([ADA, GRACE, LINUS, thin])
    text = w.text(w.url("analytics"))
    assert "insufficient_facts" in text.lower() or "insufficient facts" in text.lower()
    assert re.search(r"(?i)ok\D{0,30}?\b3\b", text) and re.search(r"(?i)blocked\D{0,30}?\b1\b", text)


