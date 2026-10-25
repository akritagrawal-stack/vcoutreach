"""Finish pass UI contracts: no Twitter or channel filter, no owner paths, correct pluralisation.

Spec only. Pages are read through the Flask test client on throwaway workspaces, no network.
No dash characters appear in this file.
"""
from __future__ import annotations

import re

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures + autouse env isolation)
from web_helpers import ADA, GRACE, body, page_text, seed_contacts
from audit_fix_helpers import soup

PAGES = ["/", "/crm", "/crm/drafts", "/crm/analytics", "/partners", "/replies", "/settings", "/setup/ai"]
LABEL_TAGS = ["label", "legend", "option", "th", "button", "summary", "h1", "h2", "h3", "h4", "a", "span", "p", "li"]


def fetch(web, path):
    response = web.get(path, follow_redirects=True)
    assert response.status_code == 200, f"GET {path} returned {response.status_code}"
    return body(response)


def seeded(make_web, replies=0):
    web = make_web({"SENDER_EMAIL": SENDER_EMAIL}, sender=True)
    seed_contacts(web, [ADA, GRACE])
    web.post(web.url("drafts_write"), {"provider": "template"}, follow=True)
    for n in range(replies):
        web.post("/replies/add", {"from_email": "ada@example.com", "subject": f"Re: hello {n}",
                                  "body": "sounds good, happy to talk"}, follow=True)
    return web


# ---------------------------------------------------------------------------------------------------------------
# No Twitter, no Channel filter, no owner path
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_empty_workspace_pages_have_no_twitter_channel_or_owner_path(make_web, path):
    web = make_web()
    markup = fetch(web, path)
    visible = page_text(markup)
    assert "twitter" not in visible.lower(), "the word Twitter appears"
    assert "twitter" not in markup.lower(), "Twitter appears in the markup"
    assert "/users/akrit" not in markup.lower()
    assert "/users/akrit" not in visible.lower()


@pytest.mark.parametrize("path", PAGES)
def test_seeded_workspace_pages_have_no_twitter_channel_or_owner_path(make_web, path):
    web = seeded(make_web, replies=1)
    markup = fetch(web, path)
    assert "twitter" not in markup.lower()
    assert "/users/akrit" not in markup.lower()


@pytest.mark.parametrize("path", PAGES)
def test_no_element_is_labelled_channel(make_web, path):
    """"Channel" must not be a filter label (label, option, heading, button or table header text)."""
    for web in (make_web(), seeded(make_web, replies=1)):
        page = soup(fetch(web, path))
        for tag in LABEL_TAGS:
            for el in page.select(tag):
                text = " ".join(el.get_text(" ").split())
                assert not re.search(r"\bChannel\b", text), f"<{tag}> on {path} says: {text[:80]}"
        for el in page.select("[name], [id], [aria-label], [placeholder], [title]"):
            for attr in ("aria-label", "placeholder", "title"):
                assert not re.search(r"\bChannel\b", el.get(attr) or ""), f"{attr} on {path}: {el.get(attr)}"
        for sel in page.select("select"):
            name = (sel.get("name") or "").lower()
            assert name != "channel", f"a channel filter select exists on {path}"


@pytest.mark.parametrize("path", ["/crm", "/crm/drafts", "/crm/analytics", "/partners", "/replies"])
def test_no_channel_query_filter_is_offered_in_links_or_forms(make_web, path):
    web = seeded(make_web, replies=1)
    markup = fetch(web, path)
    assert not re.search(r"[?&;]channel=", markup, re.I)
    assert not re.search(r'name=["\']channel["\']', markup, re.I)


# ---------------------------------------------------------------------------------------------------------------
# Pluralisation
# ---------------------------------------------------------------------------------------------------------------

def test_one_reply_reads_1_reply_not_1_replies(make_web):
    web = seeded(make_web, replies=1)
    text = page_text(body(web.get("/replies")))
    assert "1 replies" not in text
    assert re.search(r"\b1 reply\b", text), text[:600]


@pytest.mark.parametrize("path", ["/crm", "/crm/analytics", "/partners", "/replies", "/crm/drafts"])
def test_no_page_ever_says_1_replies(make_web, path):
    web = seeded(make_web, replies=1)
    text = page_text(fetch(web, path))
    assert "1 replies" not in text, path


def test_two_replies_reads_2_replies(make_web):
    web = seeded(make_web, replies=2)
    text = page_text(body(web.get("/replies")))
    assert re.search(r"\b2 replies\b", text), text[:600]


def test_zero_replies_never_reads_0_reply(make_web):
    web = seeded(make_web, replies=0)
    text = page_text(body(web.get("/replies")))
    assert not re.search(r"\b0 reply\b", text)


# ---------------------------------------------------------------------------------------------------------------
# No dash characters shown to the user on the touched pages
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/crm/drafts", "/setup/ai"])
def test_the_touched_pages_use_no_em_dash(make_web, path):
    web = make_web()
    assert chr(0x2014) not in page_text(fetch(web, path))
