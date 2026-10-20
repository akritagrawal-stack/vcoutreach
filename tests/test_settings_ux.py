"""Settings and replies page UX contract (GET /settings and GET /replies).

Spec only: the tests read the served HTML through the Flask test client and parse it with html.parser.
No network, a throwaway workspace per test, obviously fake key values.
No dash characters appear in this file.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import body

STATES = {"unset", "saved", "ok", "fail"}
GMAIL_VIEWS = {"no-client", "ready", "running", "connected", "failed"}
SERVICES = ["pitchbook", "apollo", "enrow", "gemini", "tracking"]
KEY_SERVICES = ["apollo", "enrow", "gemini", "tracking"]
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


# ---------------------------------------------------------------------------------------------------------------
# A small DOM built with the standard library parser
# ---------------------------------------------------------------------------------------------------------------

class Node:
    def __init__(self, tag, attrs, parent, idx):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.idx = idx
        self.children = []
        self.text = []

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def has(self, name):
        return name in self.attrs

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()

    def find_all(self, tag=None, **attrs):
        out = []
        for n in self.walk():
            if tag and n.tag != tag:
                continue
            if all((n.has(k) if v is True else n.attrs.get(k) == v) for k, v in attrs.items()):
                out.append(n)
        return out

    def find(self, tag=None, **attrs):
        found = self.find_all(tag, **attrs)
        return found[0] if found else None

    def ancestors(self):
        n = self.parent
        while n is not None:
            yield n
            n = n.parent

    def inside(self, other):
        return any(a is other for a in self.ancestors())

    def visible_by_default(self):
        return not self.has("hidden") and not any(a.has("hidden") for a in self.ancestors())

    def all_text(self):
        parts = list(self.text)
        for c in self.children:
            parts.append(c.all_text())
        return " ".join(" ".join(parts).split())


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None, 0)
        self.cur = self.root
        self.n = 0

    def handle_starttag(self, tag, attrs):
        self.n += 1
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur, self.n)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.n += 1
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur, self.n)
        self.cur.children.append(node)

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


def page(web, path="/settings"):
    return parse(web.html(path))


def card(root, service):
    found = root.find_all("article", id=f"svc-{service}")
    assert len(found) == 1, f"expected exactly one card with id svc-{service}, found {len(found)}"
    return found[0]


def fields_of(form):
    return [n for n in form.walk() if n.tag in ("input", "select", "textarea")]


def text_fields(form):
    return [n for n in fields_of(form)
            if n.tag != "input" or (n.attrs.get("type", "text").lower() not in ("hidden", "submit", "button", "file", "checkbox", "radio"))]


GOOD_CLIENT = json.dumps({"installed": {
    "client_id": "1234567890-abcdef.apps.googleusercontent.com",
    "client_secret": "GOCSPX-unit-test-secret-9f3a",
    "project_id": "demo",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"],
}}).encode()


def store_client(web):
    from vcoutreach.web.gmail import setup as gmail_setup

    gmail_setup.store_oauth_client(GOOD_CLIENT, web.app.extensions["workspace"])


def store_token(web):
    path = web.app.extensions["workspace"].path("data", ".gmail_token.json")
    path.write_text(json.dumps({"refresh_token": "fake-refresh", "token": "fake-access"}))


# ---------------------------------------------------------------------------------------------------------------
# 1. One card per service
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("service", SERVICES + ["gmail"])
def test_1_exactly_one_card_with_matching_id_and_data_service(web, service):
    c = card(page(web), service)
    assert "svc-card" in c.classes
    assert c.attrs.get("data-service") == service


@pytest.mark.parametrize("service", SERVICES + ["gmail"])
def test_1_card_data_state_is_one_of_the_allowed_values(web, service):
    assert card(page(web), service).attrs.get("data-state") in STATES


def test_1_gmail_card_data_gmail_view_is_allowed(web):
    assert card(page(web), "gmail").attrs.get("data-gmail-view") in GMAIL_VIEWS


def test_1_every_svc_card_uses_article_with_a_svc_id(web):
    cards = [n for n in page(web).walk() if "svc-card" in n.classes]
    assert cards
    for c in cards:
        assert c.tag == "article"
        assert c.attrs.get("id") == "svc-" + str(c.attrs.get("data-service"))


@pytest.mark.parametrize("service", SERVICES)
def test_1_no_key_form_for_a_service_outside_its_card(web, service):
    root = page(web)
    own = card(root, service)
    stray = []
    for f in root.find_all("form"):
        if f.inside(own):
            continue
        for fld in fields_of(f):
            name = (fld.attrs.get("name") or "").upper()
            value = (fld.attrs.get("value") or "").lower()
            if service.upper() in name and name != "PITCHBOOK_ENABLED":
                stray.append(name)
            if name == "PROVIDER" and value == service:
                stray.append(f"provider={value}")
    assert not stray, f"{service} key fields outside its card: {stray}"


def test_1_no_key_field_name_appears_twice_on_the_page(web):
    root = page(web)
    seen = {}
    for fld in root.find_all("input"):
        name = fld.attrs.get("name") or ""
        if fld.attrs.get("type", "text").lower() in ("hidden", "submit", "button", "file"):
            continue
        if name.isupper() and any(w in name for w in ("KEY", "TOKEN", "_URL", "SECRET")):
            seen[name] = seen.get(name, 0) + 1
    dup = {k: v for k, v in seen.items() if v > 1}
    assert not dup, f"duplicate key fields: {dup}"


# ---------------------------------------------------------------------------------------------------------------
# 2. Status strip
# ---------------------------------------------------------------------------------------------------------------

def tiles(root):
    return [n for n in root.find_all("a") if "svc-tile" in n.classes]


def tile_with_label(root, label):
    for t in tiles(root):
        if label.lower() in t.all_text().lower():
            return t
    return None


@pytest.mark.parametrize("label,href", [
    ("PitchBook", "#svc-pitchbook"), ("Gmail", "#svc-gmail"), ("Database", "#sec-identity"),
    ("Pitch deck", "#sec-package"), ("Calendar", "#sec-snapshot"),
])
def test_2_tile_is_an_anchor_with_the_expected_href(web, label, href):
    t = tile_with_label(page(web), label)
    assert t is not None, f"no status tile labelled {label}"
    assert t.attrs.get("href") == href


def test_2_every_tile_href_target_exists(web):
    root = page(web)
    ids = {n.attrs["id"] for n in root.walk() if "id" in n.attrs}
    ts = tiles(root)
    assert ts
    missing = [t.attrs.get("href") for t in ts if (t.attrs.get("href") or "").lstrip("#") not in ids]
    assert not missing, f"tile targets missing from page: {missing}"


# ---------------------------------------------------------------------------------------------------------------
# 3. Document order
# ---------------------------------------------------------------------------------------------------------------

def test_3_sample_lives_in_details_sec_sample(web):
    d = page(web).find("details", id="sec-sample")
    assert d is not None


@pytest.mark.parametrize("service", SERVICES + ["gmail"])
def test_3_card_comes_before_sample(web, service):
    root = page(web)
    assert card(root, service).idx < root.find("details", id="sec-sample").idx


def test_3_sample_is_the_last_details_section(web):
    root = page(web)
    sample = root.find("details", id="sec-sample")
    later = [d.attrs.get("id") for d in root.find_all("details") if d.idx > sample.idx and not d.inside(sample)]
    assert not later, f"sections after the sample: {later}"


@pytest.mark.parametrize("service", SERVICES + ["gmail"])
def test_3_card_is_not_after_sec_more(web, service):
    root = page(web)
    assert card(root, service).idx < root.find("details", id="sec-more").idx


# ---------------------------------------------------------------------------------------------------------------
# 4. Non-Gmail card controls
# ---------------------------------------------------------------------------------------------------------------

def svc_form(c):
    forms = [f for f in c.find_all("form") if f.has("data-svc-form")]
    return forms[0] if forms else None


@pytest.mark.parametrize("service", SERVICES)
def test_4_card_has_a_data_svc_form(web, service):
    assert svc_form(card(page(web), service)) is not None


@pytest.mark.parametrize("service", SERVICES)
def test_4_form_has_hidden_csrf_token(web, service):
    f = svc_form(card(page(web), service))
    assert f is not None
    tok = [i for i in f.find_all("input") if i.attrs.get("name") == "csrf_token"]
    assert tok and tok[0].attrs.get("type") == "hidden" and tok[0].attrs.get("value")


@pytest.mark.parametrize("service", KEY_SERVICES)
def test_4_form_has_a_submit_button(web, service):
    f = svc_form(card(page(web), service))
    assert f is not None
    subs = [b for b in f.find_all("button") if b.attrs.get("type", "submit").lower() == "submit"]
    subs += [i for i in f.find_all("input") if i.attrs.get("type", "").lower() == "submit"]
    assert subs


@pytest.mark.parametrize("service", KEY_SERVICES)
def test_4_card_has_a_data_svc_test_button(web, service):
    c = card(page(web), service)
    assert [n for n in c.walk() if n.has("data-svc-test") and n.tag == "button"]


def test_4_pitchbook_has_no_key_text_field(web):
    f = svc_form(card(page(web), "pitchbook"))
    assert f is not None
    assert not text_fields(f)


def test_4_pitchbook_card_has_no_key_input_fields(web):
    c = card(page(web), "pitchbook")
    assert not text_fields(c)


def test_4_pitchbook_card_has_no_store_button_and_no_svc_test(web):
    c = card(page(web), "pitchbook")
    assert not [n for n in c.walk() if n.has("data-svc-test")]
    stores = [b for b in c.find_all("button") if "store" in b.all_text().lower()]
    assert not stores


def test_4_pitchbook_card_has_open_sign_in_button(web):
    c = card(page(web), "pitchbook")
    assert [n for n in c.walk() if n.has("data-pitchbook-launch") and n.tag == "button"]


def test_4_pitchbook_card_has_check_session_button(web):
    c = card(page(web), "pitchbook")
    assert [n for n in c.walk() if n.has("data-pitchbook-check") and n.tag == "button"]


def test_4_tracking_card_has_a_test_connection_button(web):
    c = card(page(web), "tracking")
    assert [n for n in c.walk() if n.has("data-svc-test") and n.tag == "button"]


TRACKING_ALIASES = ["CLOUDFLARE_TRACKING_TOKEN", "CLOUDFLARE_TRACKING_URL", "TRACKING_API_TOKEN",
                    "TRACKING_ORIGIN", "TRACKER_PUBLIC_URL", "GOOGLE_API_KEY"]


@pytest.mark.parametrize("alias", TRACKING_ALIASES)
def test_4_tracking_key_alias_is_never_an_input_outside_its_card(web, alias):
    root = page(web)
    own = card(root, "tracking")
    gemini = card(root, "gemini")
    stray = []
    for fld in root.walk():
        if fld.tag not in ("input", "select", "textarea"):
            continue
        if (fld.attrs.get("name") or "").upper() != alias:
            continue
        if fld.inside(own) or (alias == "GOOGLE_API_KEY" and fld.inside(gemini)):
            continue
        stray.append(alias)
    assert not stray


def test_4_pitchbook_has_launch_control(web):
    c = card(page(web), "pitchbook")
    assert [n for n in c.walk() if n.has("data-pitchbook-launch")]


# ---------------------------------------------------------------------------------------------------------------
# 5. State after a store
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("service", SERVICES)
def test_5_card_is_unset_without_a_stored_key(web, service):
    assert card(page(web), service).attrs.get("data-state") == "unset"


def test_5_tiles_agree_with_card_state_at_start(web):
    root = page(web)
    for t in tiles(root):
        sid = t.attrs.get("data-tile-for")
        if sid:
            assert t.attrs.get("data-state") == card(root, sid).attrs.get("data-state"), sid


def store_key(web, service):
    f = svc_form(card(page(web), service))
    assert f is not None
    data = {}
    for fld in fields_of(f):
        name = fld.attrs.get("name")
        if not name:
            continue
        kind = fld.attrs.get("type", "text").lower()
        if kind == "hidden":
            data[name] = fld.attrs.get("value", "")
        elif kind == "url":
            data[name] = "https://fake-tracker.example.com"
        elif kind in ("text", "password", ""):
            data[name] = "fakekey_" + service + "_0123456789abcdef"
    action = f.attrs.get("action")
    assert action
    return web.post(action, data, follow=True)


@pytest.mark.parametrize("service", KEY_SERVICES)
def test_5_card_is_saved_after_storing_a_key(web, service):
    r = store_key(web, service)
    assert r.status_code < 400, f"store route answered {r.status_code}"
    assert card(page(web), service).attrs.get("data-state") == "saved"


@pytest.mark.parametrize("service", KEY_SERVICES)
def test_5_tile_never_disagrees_with_card_after_store(web, service):
    store_key(web, service)
    root = page(web)
    c = card(root, service)
    for t in tiles(root):
        if t.attrs.get("data-tile-for") == service:
            assert t.attrs.get("data-state") == c.attrs.get("data-state")


def test_5_all_tiles_agree_with_cards_after_store(web):
    store_key(web, "enrow")
    root = page(web)
    for t in tiles(root):
        sid = t.attrs.get("data-tile-for")
        if sid:
            assert t.attrs.get("data-state") == card(root, sid).attrs.get("data-state"), sid


def test_5_stored_secret_value_is_not_echoed_back(web):
    store_key(web, "enrow")
    assert "fakekey_enrow_0123456789abcdef" not in web.html("/settings")


# ---------------------------------------------------------------------------------------------------------------
# 6. Gmail card
# ---------------------------------------------------------------------------------------------------------------

def panels(c):
    return [n for n in c.walk() if n.has("data-gmail-panel")]


def panel(c, name):
    found = [p for p in panels(c) if p.attrs.get("data-gmail-panel") == name]
    assert found, f"no gmail panel {name}"
    return found[0]


def test_6_no_client_view(web):
    assert card(page(web), "gmail").attrs.get("data-gmail-view") == "no-client"


def test_6_no_client_only_default_visible_panel_is_no_client(web):
    c = card(page(web), "gmail")
    shown = [p.attrs["data-gmail-panel"] for p in panels(c) if p.visible_by_default()]
    assert shown == ["no-client"]


def test_6_no_client_panel_offers_choose(web):
    c = card(page(web), "gmail")
    assert [n for n in panel(c, "no-client").walk() if n.has("data-gmail-choose")]


def test_6_ready_view_with_valid_client(web):
    store_client(web)
    assert card(page(web), "gmail").attrs.get("data-gmail-view") == "ready"


def test_6_ready_panel_has_connect(web):
    store_client(web)
    c = card(page(web), "gmail")
    assert [n for n in panel(c, "ready").walk() if n.has("data-gmail-connect")]


def test_6_ready_only_default_visible_panel_is_ready(web):
    store_client(web)
    c = card(page(web), "gmail")
    shown = [p.attrs["data-gmail-panel"] for p in panels(c) if p.visible_by_default()]
    assert shown == ["ready"]


def test_6_connected_view_with_token(web):
    store_client(web)
    store_token(web)
    assert card(page(web), "gmail").attrs.get("data-gmail-view") == "connected"


def test_6_connected_panel_has_switch_and_remove(web):
    store_client(web)
    store_token(web)
    p = panel(card(page(web), "gmail"), "connected")
    assert [n for n in p.walk() if n.has("data-gmail-switch")]
    assert [n for n in p.walk() if n.has("data-gmail-remove")]


def test_6_connected_panel_has_no_connect_or_choose(web):
    store_client(web)
    store_token(web)
    p = panel(card(page(web), "gmail"), "connected")
    bad = [n for n in p.walk() if n.has("data-gmail-connect") or n.has("data-gmail-choose")]
    assert not bad


def test_6_connected_only_default_visible_panel_is_connected(web):
    store_client(web)
    store_token(web)
    c = card(page(web), "gmail")
    shown = [p.attrs["data-gmail-panel"] for p in panels(c) if p.visible_by_default()]
    assert shown == ["connected"]


# ---------------------------------------------------------------------------------------------------------------
# 7. Layout
# ---------------------------------------------------------------------------------------------------------------

def test_7_single_main_element(web):
    assert len(page(web).find_all("main")) == 1


@pytest.mark.parametrize("cls", ["container", "narrow", "two-col"])
def test_7_main_has_no_capping_class(web, cls):
    assert cls not in page(web).find("main").classes


def test_7_no_two_col_element(web):
    assert not [n for n in page(web).walk() if "two-col" in n.classes]


@pytest.mark.parametrize("cls", ["container", "narrow", "two-col"])
def test_7_body_has_no_capping_class(web, cls):
    assert cls not in page(web).find("body").classes


# ---------------------------------------------------------------------------------------------------------------
# 8. Replies
# ---------------------------------------------------------------------------------------------------------------

def test_8_add_reply_details_exists_and_is_closed(web):
    d = page(web, "/replies").find("details", id="addReply")
    assert d is not None
    assert not d.has("open")


def test_8_add_reply_form_is_inside_the_details(web):
    root = page(web, "/replies")
    d = root.find("details", id="addReply")
    action = web.url("reply_add")
    forms = [f for f in root.find_all("form") if f.attrs.get("action") == action]
    assert len(forms) == 1
    assert forms[0].inside(d)


def test_8_add_reply_action_is_unchanged(web):
    root = page(web, "/replies")
    d = root.find("details", id="addReply")
    forms = d.find_all("form")
    assert forms and forms[0].attrs.get("action") == web.url("reply_add")
    assert forms[0].attrs.get("method", "").lower() == "post"


@pytest.mark.parametrize("names", [("sender", "from_email", "from", "email"), ("subject",), ("body", "message", "text")])
def test_8_add_reply_field_names_unchanged(web, names):
    d = page(web, "/replies").find("details", id="addReply")
    have = {f.attrs.get("name") for f in fields_of(d)}
    assert have & set(names), f"none of {names} in {sorted(n for n in have if n)}"


@pytest.mark.parametrize("bad", ["btn-full", "square-button"])
def test_8_add_reply_submit_has_no_banned_class(web, bad):
    d = page(web, "/replies").find("details", id="addReply")
    subs = [b for b in d.find_all("button") if b.attrs.get("type", "submit").lower() == "submit"]
    assert subs
    for b in subs:
        assert bad not in b.classes


@pytest.mark.parametrize("bad", ["btn-full", "square-button"])
def test_8_poll_control_has_no_banned_class(web, bad):
    root = page(web, "/replies")
    polls = [n for n in root.walk() if n.tag in ("button", "a", "input") and "poll gmail now" in (n.all_text() + " " + n.attrs.get("value", "")).lower()]
    assert polls, "no Poll Gmail now control"
    for p in polls:
        assert bad not in p.classes


# ---------------------------------------------------------------------------------------------------------------
# 9. Security
# ---------------------------------------------------------------------------------------------------------------

PAGES = ["/settings", "/replies"]


@pytest.mark.parametrize("path", PAGES)
def test_9_test_mode_banner_text_present(web, path):
    assert "TEST MODE" in page(web, path).find("body").all_text()


@pytest.mark.parametrize("path", PAGES)
def test_9_test_mode_banner_is_first_in_body(web, path):
    b = page(web, path).find("body")
    first = None
    for n in b.walk():
        if "skip-link" in n.classes:
            continue
        if n.text and "".join(n.text).strip():
            first = n
            break
    assert first is not None and "TEST MODE" in first.all_text()


@pytest.mark.parametrize("path", PAGES)
def test_9_no_inline_script(web, path):
    inline = [s for s in page(web, path).find_all("script") if not s.has("src")]
    assert not inline


@pytest.mark.parametrize("path", PAGES)
def test_9_csp_forbids_inline_scripts(web, path):
    csp = web.get(path).headers.get("Content-Security-Policy", "")
    directives = {d.strip().split()[0]: d.strip().split()[1:] for d in csp.split(";") if d.strip()}
    script = directives.get("script-src", directives.get("default-src"))
    assert script is not None
    assert "'unsafe-inline'" not in script
