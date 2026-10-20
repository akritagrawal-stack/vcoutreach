"""Outreach MIME tests authored only from /tmp/codexlane/contract.txt."""
from __future__ import annotations

import email.policy
import re
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

import pytest

from vcoutreach.web import outreach_mime as mime


BODY = (
    "Hi Priya,\n\n"
    "I enjoyed your essay about patient seed investing at Cedar Grove Ventures.\n\n"
    "We are building Lumenlist with independent research teams. "
    "Would you have time for a short conversation next week?\n\n"
    "Best,\nSam Rivera\nFounder, Lumenlist"
)
DECK_URL = "https://decks.lumenlist.example/seed.pdf?token=review%2Bcedar&expires=1792537200"


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.stack = []
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "div":
            node = {"attrs": dict(attrs), "text": "", "breaks": 0, "children": []}
            if self.stack:
                self.stack[-1]["children"].append(node)
            self.nodes.append(node)
            self.stack.append(node)
        elif tag == "br" and self.stack:
            self.stack[-1]["breaks"] += 1

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack:
            self.stack[-1]["text"] += data


@pytest.mark.parametrize("kind", ["follow_up", "warm_reply", "first_touch", "custom_review"])
@pytest.mark.parametrize("body", [BODY, "", "\n \n", "Hi Zoë,\n\nCafé research in 日本語."])
def test_lint_kind_does_not_change_findings(body, kind):
    """Contract lines 4 to 9: findings are strings and kind is behavior-neutral."""
    baseline = mime.lint_body(body)
    assert isinstance(baseline, list)
    assert all(isinstance(item, str) and item.strip() for item in baseline)
    assert mime.lint_body(body, kind=kind) == baseline


@pytest.mark.parametrize("body", [BODY, "", "Hi Zoë,\n\nCafé & 日本語."])
def test_attach_mode_preserves_body_without_a_deck_url(body):
    """Contract line 13: attach leaves the body unchanged."""
    assert mime.apply_deck_mode(body, "attach", None) == body


@pytest.mark.parametrize("mode", ["link", "attach_and_link"])
def test_link_modes_reject_a_missing_url(mode):
    """Contract lines 21 to 22: URL-dependent modes reject no URL."""
    with pytest.raises(ValueError):
        mime.apply_deck_mode(BODY, mode, None)


@pytest.mark.parametrize("mode", ["", "preview", "LINK", "attch"])
def test_unknown_deck_mode_is_rejected(mode):
    """Contract lines 21 to 22: an unknown mode raises ValueError."""
    with_deck = BODY + "\n\nOur updated deck is attached."
    assert DECK_URL in mime.apply_deck_mode(with_deck, "link", DECK_URL)
    with pytest.raises(ValueError, match="(?i)mode"):
        mime.apply_deck_mode(with_deck, mode, DECK_URL)


@pytest.mark.parametrize("token", ["{{first_name}}", "{first_name}", "[Company]", "[TBD]"])
def test_unfilled_placeholder_is_a_blocking_lint_error(token):
    findings = mime.lint_body(BODY.replace("Priya", token))
    assert any(not f.lower().startswith("warn:") and re.search("placeholder|unfilled|template", f, re.I)
               for f in findings), findings


@pytest.mark.parametrize("phrase", ["I hope this email finds you well", "Hope this email finds you well",
                                    "I hope this message finds you well"])
def test_canned_well_wishes_are_flagged(phrase):
    findings = mime.lint_body(BODY.replace("I enjoyed your essay about patient seed investing at Cedar Grove Ventures.", phrase))
    assert any(re.search(r"hope|finds.*well|clich|banned|generic", finding, re.I)
               for finding in findings), findings


@pytest.mark.parametrize("renderer", [mime.render_html, mime.paragraph_preview_html])
def test_html_escapes_body_and_quoted_url_attributes(renderer):
    text = 'R&D <img src=x onerror="alert(1)"> > "quoted" & final'
    malicious_url = 'https://example.com/deck?x=" onmouseover="alert(1)'
    html = renderer("Hi Priya,\n\n" + malicious_url + "\n\n" + text)
    parsed = Paragraphs()
    parsed.feed(html)
    assert not any(tag in ("img", "script") for tag, attrs in parsed.tags)
    assert all(not key.lower().startswith("on") for tag, attrs in parsed.tags for key in attrs)
    assert "R&amp;D" in html and "&lt;img" in html and "&gt;" in html
    assert re.search(r"&quot;quoted&quot;|&#(?:34|x22);quoted&#(?:34|x22);", html)
    assert any(text in node["text"] for node in parsed.nodes)


@pytest.mark.parametrize("mode", ["link", "attach_and_link"])
@pytest.mark.parametrize("body", ["", "Hi Priya,\n\nCan we meet on Tuesday?\n\nSam Rivera"])
def test_link_rewrite_requires_a_deck_sentence(body, mode):
    """Contract lines 21 to 22: no deck sentence raises ValueError."""
    with pytest.raises(ValueError):
        mime.apply_deck_mode(body, mode, DECK_URL)


@pytest.mark.parametrize("renderer", [mime.render_html, mime.paragraph_preview_html])
def test_gmail_paragraph_layout_and_signature_line_breaks(renderer):
    """Contract lines 24 to 29: paragraphs, spacer divs, and signature breaks."""
    parsed = Paragraphs()
    parsed.feed(renderer(BODY))
    roots = [node for node in parsed.nodes if node["attrs"].get("dir") == "ltr"]
    assert len(roots) == 1
    children = roots[0]["children"]
    paragraphs = [node for node in children if node["text"].strip()]
    spacers = [node for node in children if not node["text"].strip() and node["breaks"]]
    assert len(paragraphs) == 4
    assert len(spacers) == 3
    assert all("\n" not in node["text"] and "\r" not in node["text"] for node in paragraphs)
    assert paragraphs[-1]["breaks"] == 2
    assert not {"pre", "table"}.intersection(tag for tag, attrs in parsed.tags)
    assert all("width" not in attrs and "width" not in attrs.get("style", "").lower()
               for tag, attrs in parsed.tags)


def test_preview_is_only_the_embeddable_ltr_div():
    """Contract line 29: no document wrapper is included in a CRM preview."""
    preview = mime.paragraph_preview_html("Hi Zoë,\n\nCafé research in 日本語.\n\nSam Rivera")
    assert preview.strip().startswith('<div dir="ltr">')
    assert preview.strip().endswith("</div>")
    assert "<html" not in preview.lower() and "<body" not in preview.lower()
    assert "Café" in preview and "日本語" in preview


@pytest.mark.parametrize("renderer", [mime.render_html, mime.paragraph_preview_html])
def test_malformed_greeting_uses_safe_fallback_in_sent_html_and_preview(renderer):
    parsed = Paragraphs()
    parsed.feed(renderer(BODY.replace("Hi Priya,", 'Hi "click"),,')))
    paragraphs = [node["text"] for node in parsed.nodes if node["text"].strip()]
    assert paragraphs[0] == "Hi there,"
    assert "I enjoyed your essay about patient seed investing at Cedar Grove Ventures." in paragraphs


def build(**overrides):
    kwargs = dict(sender="Sam Rivera <sam@lumenlist.example>",
                  to="Priya Raman <priya@cedargrove.example>",
                  subject="A seed research conversation", body=BODY)
    kwargs.update(overrides)
    return mime.build_message(**kwargs)


@pytest.fixture
def clean_lint(monkeypatch):
    # Other tests exercise lint. These isolate the documented MIME construction.
    monkeypatch.setattr(mime, "lint_body", lambda body, **kwargs: [])


def test_message_is_a_roundtrippable_multipart_alternative(clean_lint):
    """Contract lines 31 to 35: plain text plus HTML, serialized with SMTP."""
    message = build(body=BODY.replace("Priya", "Zoë"), subject="Café research in 日本語")
    assert isinstance(message, EmailMessage)
    wire = message.as_bytes(policy=email.policy.SMTP)
    parsed = BytesParser(policy=email.policy.default).parsebytes(wire)
    assert parsed.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in parsed.iter_parts()] == ["text/plain", "text/html"]
    assert str(parsed["Subject"]) == "Café research in 日本語"
    assert "Hi Zoë," in parsed.get_body(preferencelist=("plain",)).get_content()
    assert "Hi Zoë," in parsed.get_body(preferencelist=("html",)).get_content()
    assert not parsed.defects


def test_attachment_wraps_alternative_and_preserves_pdf_bytes(clean_lint):
    """Contract lines 30 to 35: attachments require a mixed MIME envelope."""
    message = build(attachments=[("Lumenlist seed résumé.pdf", b"%PDF-1.7\nseed deck\n%%EOF", "application/pdf")])
    parsed = BytesParser(policy=email.policy.default).parsebytes(message.as_bytes(policy=email.policy.SMTP))
    assert parsed.get_content_type() == "multipart/mixed"
    assert next(parsed.iter_parts()).get_content_type() == "multipart/alternative"
    attachments = list(parsed.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_filename() == "Lumenlist seed résumé.pdf"
    assert attachments[0].get_payload(decode=True) == b"%PDF-1.7\nseed deck\n%%EOF"


@pytest.mark.parametrize("allow_warnings", [True, False])
def test_lint_errors_refuse_message_even_when_warnings_are_allowed(monkeypatch, allow_warnings):
    """Contract lines 32 to 34: actual errors always raise and are listed."""
    monkeypatch.setattr(mime, "lint_body", lambda body, **kwargs: ["Greeting is missing", "Signature is missing"])
    with pytest.raises(ValueError) as caught:
        build(allow_warnings=allow_warnings)
    assert "Greeting is missing" in str(caught.value)
    assert "Signature is missing" in str(caught.value)


def test_warning_is_allowed_by_default(monkeypatch):
    """Contract lines 33 to 34: warn findings alone do not block by default."""
    monkeypatch.setattr(mime, "lint_body", lambda body, **kwargs: ["warn: word count out of range"])
    assert build().get_content_type() == "multipart/alternative"


def test_warning_blocks_when_explicitly_disallowed(monkeypatch):
    """Contract lines 33 to 34: strict warning mode raises ValueError."""
    monkeypatch.setattr(mime, "lint_body", lambda body, **kwargs: ["warn: word count out of range"])
    with pytest.raises(ValueError, match="word count out of range"):
        build(allow_warnings=False)


def test_builder_applies_deck_rewrite_before_lint(monkeypatch):
    """Contract line 32: lint must see the rewritten email, not the old body."""
    calls = []

    def rewrite(body, mode, deck_url):
        calls.append("rewrite")
        return "Hi Priya,\n\nHere is our updated deck: <https://decks.lumenlist.example/seed.pdf>.\n\nSam Rivera"

    def lint(body, **kwargs):
        calls.append("lint")
        return [] if "Here is our updated deck:" in body else ["Deck rewrite has not run"]

    monkeypatch.setattr(mime, "apply_deck_mode", rewrite)
    monkeypatch.setattr(mime, "lint_body", lint)
    result = build(deck_mode="link", deck_url=DECK_URL)
    assert calls[:2] == ["rewrite", "lint"]
    assert "Here is our updated deck:" in result.get_body(preferencelist=("plain",)).get_content()


def test_deck_validation_error_is_not_swallowed(clean_lint):
    """Contract lines 21 to 22 and 32: build_message runs deck validation."""
    with pytest.raises(ValueError):
        build(deck_mode="link", deck_url=None)
