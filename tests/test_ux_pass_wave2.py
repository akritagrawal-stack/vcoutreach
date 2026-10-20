"""Publikreach UX pass, wave 2: spec only, observed through the Flask test client.

No network, a throwaway workspace per test, obviously fake values. No dash characters in this file.
"""
from __future__ import annotations

import html as html_lib
import re
import sqlite3
from html.parser import HTMLParser

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import BASE, CSP_EXPECTED, STANDARD_ROWS, body, go_live, page_text

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BRAND_PAGES = ["/", "/crm", "/partners", "/candidates", "/replies", "/scheduled", "/settings"]


class Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], []

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def find_all(self, tag=None, cls=None):
        return [n for n in self.walk() if (tag is None or n.tag == tag) and (cls is None or cls in n.classes)]

    def find(self, tag=None, cls=None):
        found = self.find_all(tag, cls)
        return found[0] if found else None

    def inside(self, tag):
        n = self.parent
        while n is not None:
            if n.tag == tag:
                return True
            n = n.parent
        return False

    def all_text(self):
        parts = list(self.text) + [c.all_text() for c in self.children]
        return " ".join(" ".join(parts).split())


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text.append(data)


def parse(markup):
    b = _Builder()
    b.feed(markup)
    b.close()
    return b.root


def dom(web, path, **kw):
    r = web.get(path, follow_redirects=True, **kw)
    assert r.status_code == 200, f"{path} answered {r.status_code}"
    return parse(body(r))


def raw(web, path):
    r = web.get(path, follow_redirects=True)
    assert r.status_code == 200, f"{path} answered {r.status_code}"
    return body(r)


def brand_text(markup):
    root = parse(markup)
    link = root.find("a", "nav-brand")
    assert link is not None, "no header brand link"
    parts = []

    def collect(n):
        if n.attrs.get("aria-hidden") == "true":
            return
        parts.extend(n.text)
        for c in n.children:
            collect(c)

    collect(link)
    return " ".join(" ".join(parts).split())


def title_of(markup):
    m = re.search(r"(?is)<title>(.*?)</title>", markup)
    assert m, "no title"
    return html_lib.unescape(" ".join(m.group(1).split()))


def seeded(send_web, **kw):
    return send_web(STANDARD_ROWS, **kw)


def db(web):
    return sqlite3.connect(str(web.ws / "data" / "outreach.db"))


# ---------------------------------------------------------------------------------------------------------------
# 1. Brand and title
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", BRAND_PAGES)
def test_1_header_brand_reads_publikreach_as_one_word(web, path):
    assert brand_text(raw(web, path)) == "Publikreach"


@pytest.mark.parametrize("path", BRAND_PAGES)
def test_1_title_ends_with_pipe_publikreach(web, path):
    assert title_of(raw(web, path)).endswith("| Publikreach")


@pytest.mark.parametrize("path", BRAND_PAGES)
def test_1_title_names_publikreach_exactly_once(web, path):
    assert title_of(raw(web, path)).count("Publikreach") == 1


@pytest.mark.parametrize("path", BRAND_PAGES)
def test_1_title_has_no_slash_publikreach(web, path):
    assert "/ Publikreach" not in title_of(raw(web, path))


# ---------------------------------------------------------------------------------------------------------------
# 2. Send-mode banner first
# ---------------------------------------------------------------------------------------------------------------

BANNER_PAGES = ["/crm/drafts", "/crm/send-plan", "/crm/drafts/ada"]


def first_body_content(root):
    body_node = root.find("body")
    assert body_node is not None
    for c in body_node.children:
        if c.tag in ("script", "noscript", "style"):
            continue
        if "skip-link" in c.classes:
            continue
        return c
    return None


@pytest.mark.parametrize("path", BANNER_PAGES)
def test_2_send_mode_banner_is_the_first_body_content(send_web, path):
    w = seeded(send_web)
    first = first_body_content(dom(w, path))
    assert first is not None and "send-banner" in first.classes


@pytest.mark.parametrize("path", BANNER_PAGES)
def test_2_banner_says_test_mode_in_test_mode(send_web, path):
    w = seeded(send_web)
    first = first_body_content(dom(w, path))
    assert "TEST MODE" in first.all_text()


@pytest.mark.parametrize("path", BANNER_PAGES)
def test_2_banner_says_live_mode_in_live_mode(send_web, path):
    w = seeded(send_web)
    go_live(w)
    first = first_body_content(dom(w, path))
    assert first is not None and "send-banner" in first.classes
    assert "LIVE MODE" in first.all_text()


@pytest.mark.parametrize("path", BANNER_PAGES)
def test_2_go_live_control_still_exists_in_test_mode(send_web, path):
    w = seeded(send_web)
    root = dom(w, path)
    forms = [f for f in root.find_all("form") if f.attrs.get("action", "").endswith("/toggle-send-mode")]
    assert forms, "no toggle-send-mode form"
    assert "GO LIVE" in " ".join(f.all_text() + " " + " ".join(
        (i.attrs.get("placeholder", "") + i.attrs.get("pattern", "")) for i in f.find_all("input")) for f in forms)


# ---------------------------------------------------------------------------------------------------------------
# 3. Drafts list has no per-row send
# ---------------------------------------------------------------------------------------------------------------

def _send_like(action):
    path = action.split("?")[0].rstrip("/")
    return bool(re.search(r"/send(-test)?$", path))


def test_3_drafts_has_no_form_posting_to_a_per_row_send_test_route(send_web):
    w = seeded(send_web)
    actions = [f.attrs.get("action", "") for f in dom(w, "/crm/drafts").find_all("form")]
    assert not [a for a in actions if a.endswith("/send-test")]


def test_3_drafts_has_no_form_posting_to_a_per_row_send_route(send_web):
    w = seeded(send_web)
    actions = [f.attrs.get("action", "") for f in dom(w, "/crm/drafts").find_all("form")]
    assert not [a for a in actions if _send_like(a) and "/drafts/" in a]


def test_3_drafts_has_no_send_form_inside_a_table_row(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm/drafts")
    stray = [f.attrs.get("action") for f in root.find_all("form") if f.inside("tr")]
    assert not stray


def test_3_drafts_has_no_send_button_in_rows_even_in_live_mode(send_web):
    w = seeded(send_web)
    go_live(w)
    root = dom(w, "/crm/drafts")
    buttons = [b.all_text() for tr in root.find_all("tr") for b in tr.find_all("button")]
    assert not [t for t in buttons if t.lower().startswith("send")]
    actions = [f.attrs.get("action", "") for f in root.find_all("form")]
    assert not [a for a in actions if _send_like(a)]


def test_3_batch_send_form_still_exists_on_partners(send_web):
    w = seeded(send_web)
    actions = [f.attrs.get("action", "") for f in dom(w, "/partners").find_all("form")]
    assert any(a.endswith("/send-batch") for a in actions)


def test_3_batch_send_still_refuses_without_the_typed_gate_in_live_mode(send_web):
    w = seeded(send_web)
    go_live(w)
    send_batch(w, None)
    assert not w.transport.calls


def test_3_batch_send_still_sends_with_the_typed_gate_in_live_mode(send_web):
    w = seeded(send_web)
    go_live(w)
    send_batch(w, "SEND BATCH OF 3")
    assert len(w.transport.calls) == 3


# ---------------------------------------------------------------------------------------------------------------
# 4. /crm segment control and the Gmail chip
# ---------------------------------------------------------------------------------------------------------------

def test_4_crm_has_exactly_one_crm_segment_select(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm")
    sel = [s for s in root.find_all("select") if "crmSegment" in (s.attrs.get("id"), s.attrs.get("name"))]
    assert len(sel) == 1


def test_4_segment_select_offers_several_segments(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm")
    sel = [s for s in root.find_all("select") if "crmSegment" in (s.attrs.get("id"), s.attrs.get("name"))]
    assert sel and len(sel[0].find_all("option")) >= 8


def test_4_crm_no_longer_has_a_row_of_segment_links(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm")
    links = [a for a in root.find_all("a") if re.match(r"^/crm\?", a.attrs.get("href", ""))]
    assert len(links) < 5, f"{len(links)} segment style links remain"


def test_4_gmail_status_chip_appears_once(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm")
    chips = [n for n in root.walk() if "crm-chip" in n.classes and "gmail" in n.all_text().lower()]
    assert len(chips) == 1


def test_4_gmail_status_chip_links_to_settings_gmail_card(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm")
    links = [a for a in root.find_all("a") if a.attrs.get("href") == "/settings#svc-gmail"]
    assert len(links) == 1


# ---------------------------------------------------------------------------------------------------------------
# 5. Analytics
# ---------------------------------------------------------------------------------------------------------------

def analytics_web(send_web):
    w = seeded(send_web)
    go_live(w)
    send_batch(w, "SEND BATCH OF 3")
    assert len(w.transport.calls) == 3, "setup: three live sends"
    return w


def test_5_analytics_keeps_wilson_strings(send_web):
    w = analytics_web(send_web)
    assert "Wilson" in page_text(raw(w, "/crm/analytics"))


def test_5_analytics_keeps_the_small_sample_marker(send_web):
    w = analytics_web(send_web)
    assert "(n<20)" in page_text(raw(w, "/crm/analytics"))


def test_5_segment_dimensions_share_one_table(send_web):
    w = analytics_web(send_web)
    root = dom(w, "/crm/analytics")
    tables = [t for t in root.find_all("table") if "dimension" in t.all_text().lower().split("segment")[0]
              or any(th.all_text().lower() == "dimension" for th in t.find_all("th"))]
    assert len(tables) == 1


def test_5_segment_rows_for_every_dimension_live_in_that_one_table(send_web):
    w = analytics_web(send_web)
    root = dom(w, "/crm/analytics")
    t = [t for t in root.find_all("table") if any(th.all_text().lower() == "dimension" for th in t.find_all("th"))][0]
    dims = {tr.attrs.get("data-dim") or tr.find_all("td")[0].all_text() for tr in t.find_all("tr") if tr.find_all("td")}
    all_dims = {tr.attrs.get("data-dim") for tr in root.find_all("tr") if tr.attrs.get("data-dim")}
    assert len(dims - {None}) >= 3
    assert all_dims <= dims


def test_5_signal_buckets_share_one_table(send_web):
    w = analytics_web(send_web)
    root = dom(w, "/crm/analytics")
    tables = [t for t in root.find_all("table") if any(th.all_text().lower() == "bucket" for th in t.find_all("th"))]
    assert len(tables) == 1


def test_5_analytics_has_far_fewer_than_ten_tables(send_web):
    w = analytics_web(send_web)
    assert len(dom(w, "/crm/analytics").find_all("table")) < 6


# ---------------------------------------------------------------------------------------------------------------
# 6. Partners, candidates, enrichment, verification
# ---------------------------------------------------------------------------------------------------------------

CLEAN_PAGES = ["/partners", "/candidates", "/enrichment", "/email-verification"]


def seed_partner_states(w):
    """Two verified, one dropped. Returns (verified_count, dropped_count) as seeded."""
    c = db(w)
    try:
        c.execute("UPDATE partners SET verification_status='verified', email_verification_status='valid' WHERE slug IN ('ada','grace')")
        c.execute("UPDATE partners SET verification_status='dropped', dropped_reason='bad domain', "
                  "email_verification_status=NULL WHERE slug='linus'")
        c.commit()
        verified = c.execute("SELECT COUNT(*) FROM partners WHERE verification_status='verified' "
                             "AND dropped_reason IS NULL").fetchone()[0]
        dropped = c.execute("SELECT COUNT(*) FROM partners WHERE dropped_reason IS NOT NULL").fetchone()[0]
    finally:
        c.close()
    return verified, dropped


@pytest.mark.parametrize("path", CLEAN_PAGES)
def test_6_page_answers_200(send_web, path):
    w = seeded(send_web)
    seed_partner_states(w)
    assert w.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/partners", "/enrichment", "/email-verification"])
def test_6_page_shows_the_seeded_contacts(send_web, path):
    w = seeded(send_web)
    seed_partner_states(w)
    text = page_text(raw(w, path))
    assert "Ada Lovelace" in text and "Grace Hopper" in text


@pytest.mark.parametrize("path", CLEAN_PAGES)
def test_6_page_has_no_two_col_class(send_web, path):
    w = seeded(send_web)
    seed_partner_states(w)
    root = dom(w, path)
    assert not [n.tag for n in root.walk() if "two-col" in n.classes]


@pytest.mark.parametrize("path", CLEAN_PAGES)
def test_6_page_has_no_inline_style_attribute(send_web, path):
    w = seeded(send_web)
    seed_partner_states(w)
    root = dom(w, path)
    assert not [n.tag for n in root.walk() if "style" in n.attrs]


def counter_text(w):
    markup = raw(w, "/partners")
    m = re.search(r'(?is)<[^>]*class="[^"]*\bpl-counters\b[^"]*"[^>]*>(.*?)</p>', markup)
    assert m, "no header counters"
    return page_text(m.group(1))


def test_6_partners_header_verified_counter_equals_seeded_verified(send_web):
    w = seeded(send_web)
    verified, _ = seed_partner_states(w)
    assert verified == 2
    m = re.search(r"(\d+)\s+verified", counter_text(w))
    assert m and int(m.group(1)) == verified


def test_6_partners_header_dropped_counter_equals_seeded_dropped(send_web):
    w = seeded(send_web)
    _, dropped = seed_partner_states(w)
    assert dropped == 1
    m = re.search(r"(\d+)\s+dropped", counter_text(w))
    assert m and int(m.group(1)) == dropped


# ---------------------------------------------------------------------------------------------------------------
# 7. Replies
# ---------------------------------------------------------------------------------------------------------------

def add_reply(w, sender="ada@example.com", subject="Re: hi", text="sounds good"):
    r = w.post("/replies/add", {"from_email": sender, "subject": subject, "body": text}, follow=True)
    assert r.status_code == 200
    ids = re.findall(r'href="/replies/(\d+)"', body(w.get("/replies")))
    assert ids
    return ids[0]


def test_7_add_a_reply_form_is_inside_a_details_element(web):
    root = dom(web, "/replies")
    form = [f for f in root.find_all("form") if f.attrs.get("action", "").endswith("/replies/add")]
    assert len(form) == 1 and form[0].inside("details")


def test_7_add_a_reply_details_is_collapsed(web):
    root = dom(web, "/replies")
    form = [f for f in root.find_all("form") if f.attrs.get("action", "").endswith("/replies/add")][0]
    n = form.parent
    while n is not None and n.tag != "details":
        n = n.parent
    assert n is not None and "open" not in n.attrs


def test_7_replies_page_has_no_btn_full_button_or_anchor(web):
    add_reply(web)
    root = dom(web, "/replies")
    assert not [n.tag for n in root.walk() if n.tag in ("button", "a") and "btn-full" in n.classes]


def test_7_reply_detail_has_no_btn_full_button_or_anchor(web):
    rid = add_reply(web)
    root = dom(web, f"/replies/{rid}")
    assert not [n.tag for n in root.walk() if n.tag in ("button", "a") and "btn-full" in n.classes]


LABEL_WORDS = {"unlabeled", "interested", "needs_more_info", "needs more info", "pass", "bounce", "other",
               "out_of_office", "out of office", "auto_reply", "auto reply"}


def label_badges(root):
    main = root.find("main")
    return [n for n in main.walk() if "badge" in n.classes and n.all_text().strip().lower() in LABEL_WORDS]


def test_7_reply_detail_has_exactly_one_label_badge_when_unlabeled(web):
    rid = add_reply(web)
    assert len(label_badges(dom(web, f"/replies/{rid}"))) == 1


def test_7_reply_detail_has_exactly_one_label_badge_after_a_label_is_chosen(web):
    rid = add_reply(web)
    web.post(f"/replies/{rid}/update-label", {"user_label": "interested"}, follow=True)
    assert len(label_badges(dom(web, f"/replies/{rid}"))) == 1


def test_7_reply_detail_has_no_slash_placeholder_text(web):
    rid = add_reply(web)
    main = dom(web, f"/replies/{rid}").find("main")
    assert " / " not in main.all_text()


def test_7_reply_detail_has_no_slash_placeholder_after_a_label_is_chosen(web):
    rid = add_reply(web)
    web.post(f"/replies/{rid}/update-label", {"user_label": "pass"}, follow=True)
    main = dom(web, f"/replies/{rid}").find("main")
    assert " / " not in main.all_text()


# ---------------------------------------------------------------------------------------------------------------
# 8. Scheduled
# ---------------------------------------------------------------------------------------------------------------

def add_scheduled(w):
    c = db(w)
    try:
        c.execute("INSERT INTO scheduled_sends(draft_ids, mode, scheduled_for) VALUES ('[1]', 'test', '2027-01-01T10:00:00')")
        c.commit()
    finally:
        c.close()


def test_8_scheduled_time_column_shows_utc(send_web):
    w = seeded(send_web)
    add_scheduled(w)
    root = dom(w, "/scheduled")
    t = root.find("table")
    assert t is not None
    ths = [th.all_text() for th in t.find_all("th")]
    cells = [td.all_text() for td in t.find_all("td")]
    assert any("UTC" in x for x in ths + cells)


def test_8_scheduled_row_time_cell_says_utc(send_web):
    w = seeded(send_web)
    add_scheduled(w)
    root = dom(w, "/scheduled")
    assert any("2027-01-01" in td.all_text() and "UTC" in td.all_text() for td in root.find_all("td"))


def test_8_scheduled_has_no_inline_style_with_rows(send_web):
    w = seeded(send_web)
    add_scheduled(w)
    assert not [n.tag for n in dom(w, "/scheduled").walk() if "style" in n.attrs]


def test_8_scheduled_has_no_inline_style_when_empty(web):
    assert not [n.tag for n in dom(web, "/scheduled").walk() if "style" in n.attrs]


def test_8_scheduled_empty_state_mentions_utc(web):
    assert "UTC" in page_text(raw(web, "/scheduled"))


# ---------------------------------------------------------------------------------------------------------------
# 9. Send plan
# ---------------------------------------------------------------------------------------------------------------

def test_9_send_plan_says_nothing_is_sent_from_this_screen(send_web):
    w = seeded(send_web)
    assert "Nothing is sent from this screen" in page_text(raw(w, "/crm/send-plan"))


def test_9_send_plan_has_a_stat_strip_with_planned(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm/send-plan")
    strip = root.find(None, "send-stats") or root.find(None, "crm-metrics")
    assert strip is not None and "planned" in strip.all_text().lower()


def test_9_send_plan_stat_strip_has_held(send_web):
    w = seeded(send_web)
    root = dom(w, "/crm/send-plan")
    strip = root.find(None, "send-stats") or root.find(None, "crm-metrics")
    assert strip is not None and "held" in strip.all_text().lower()


def test_9_send_plan_stat_strip_comes_before_the_table(send_web):
    w = seeded(send_web)
    markup = raw(w, "/crm/send-plan")
    low = markup.lower()
    assert low.index("planned") < low.index("<table")


def test_9_send_plan_has_a_single_combined_header_containing_reason(send_web):
    w = seeded(send_web)
    t = dom(w, "/crm/send-plan").find("table")
    ths = [th.all_text().lower() for th in t.find_all("th")]
    assert len([x for x in ths if "reason" in x]) == 1


def test_9_send_plan_has_no_separate_result_column_next_to_reason(send_web):
    w = seeded(send_web)
    t = dom(w, "/crm/send-plan").find("table")
    ths = [th.all_text().lower() for th in t.find_all("th")]
    assert len([x for x in ths if "reason" in x or "result" in x]) == 1


# ---------------------------------------------------------------------------------------------------------------
# 10. POST /api/tracking/test
# ---------------------------------------------------------------------------------------------------------------

def track_post(w):
    return w.client.post("/api/tracking/test", base_url=BASE, headers={"X-CSRF-Token": w.csrf, "Origin": BASE})


def app_module():
    import importlib

    return importlib.import_module("vcoutreach.web.server.app")


def test_10_unconfigured_returns_400(web):
    assert track_post(web).status_code == 400


def test_10_unconfigured_returns_json_ok_false(web):
    r = track_post(web)
    data = r.get_json()
    assert data is not None and data.get("ok") is False


def test_10_unconfigured_reply_contains_no_secret_values(web):
    secret = "tok_secret_0123456789abcdef"
    web.post("/settings/keys", {"CLOUDFLARE_TRACKING_TOKEN": secret, "TRACKING_API_TOKEN": secret}, follow=True)
    r = track_post(web)
    assert secret not in body(r)


def test_10_patched_snapshot_returns_200(web, monkeypatch):
    monkeypatch.setattr(app_module(), "fetch_tracking_snapshot", lambda *a, **k: {"events": [], "total": 0})
    assert track_post(web).status_code == 200


def test_10_patched_snapshot_returns_ok_true(web, monkeypatch):
    monkeypatch.setattr(app_module(), "fetch_tracking_snapshot", lambda *a, **k: {"events": [], "total": 0})
    data = track_post(web).get_json()
    assert data is not None and data.get("ok") is True


def _boom(*a, **k):
    raise RuntimeError("kaboom-secret-detail-4242")


def test_10_failing_snapshot_returns_400_ok_false(web, monkeypatch):
    monkeypatch.setattr(app_module(), "fetch_tracking_snapshot", _boom)
    r = track_post(web)
    assert r.status_code == 400
    assert r.get_json().get("ok") is False


def test_10_failing_snapshot_message_is_generic_and_hides_the_exception_text(web, monkeypatch):
    monkeypatch.setattr(app_module(), "fetch_tracking_snapshot", _boom)
    r = track_post(web)
    data = r.get_json()
    assert "kaboom-secret-detail-4242" not in body(r)
    assert isinstance(data.get("message"), str) and data["message"].strip()


# ---------------------------------------------------------------------------------------------------------------
# 11. Static page assets
# ---------------------------------------------------------------------------------------------------------------

ASSETS = [f"/static/pages/{n}.{ext}" for n in ("partners", "crm", "replies", "misc") for ext in ("css", "js")]


@pytest.mark.parametrize("path", ASSETS)
def test_11_asset_is_served_200(web, path):
    r = web.get(path)
    assert r.status_code == 200 and len(r.get_data()) > 0


@pytest.mark.parametrize("path", ASSETS)
def test_11_asset_carries_the_csp_header(web, path):
    csp = web.get(path).headers.get("Content-Security-Policy", "")
    assert CSP_EXPECTED <= {p.strip() for p in csp.split(";")}


@pytest.mark.parametrize("path", ASSETS)
def test_11_base_page_references_each_asset_exactly_once(web, path):
    markup = raw(web, "/crm")
    refs = re.findall(r'(?:href|src)="' + re.escape(path) + r'(?:\?[^"]*)?"', markup)
    assert len(refs) == 1
