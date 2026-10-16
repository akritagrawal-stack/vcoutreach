"""
Gmail-native MIME builder for outreach emails.

Investor emails sent as text/plain only render with hard line breaks on the
Gmail iPhone app (each wrapped line becomes its own paragraph on screen).
This module builds proper multipart/alternative messages (text/plain +
text/html) so every client reflows paragraphs to its own viewport, plus a
lint gate that blocks bad bodies (hard wraps, dashes, filler phrases,
forbidden claims, broken URLs) before they can be staged or sent.

Public API:
    lint_body(body, *, kind="follow_up") -> list[str]
    apply_deck_mode(body, mode, deck_url) -> str
    render_html(body, *, deck_url=None, open_pixel_url=None) -> str
    paragraph_preview_html(body, *, deck_url=None, open_pixel_url=None) -> str
    build_message(*, sender, to, subject, body, ...) -> EmailMessage

CLI:
    python -m app.outreach_mime preview <body.txt> <out.html>
"""
from __future__ import annotations

import html
import re
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from vcoutreach.web.workspace_facts import forbidden_phrases

# ---------------------------------------------------------------------------
# Lint gate
# ---------------------------------------------------------------------------

_EM_DASH = '\u2014'
_EN_DASH = chr(0x2013)

_FILLER_PHRASES: list[str] = [
    "not bad",
    "not unlike",
    "no small",
    "not insignificant",
    "hardly",
    "not only",
    "isn't just",
    "is not just",
    "can't help but",
    "no shortage",
    "i hope this finds you well",
    "just circling back",
    "bumping this",
    "excited to share",
    "quick question",
    "leverage",
    "delve",
    "it's worth noting",
]


_URL_RE = re.compile(r'https?://[^\s<>]+')


def _split_paragraphs(body: str) -> list[str]:
    """Split body into paragraphs on one or more blank lines."""
    normalized = body.strip('\n')
    if not normalized:
        return []
    return re.split(r'\n[ \t]*\n+', normalized)


def _word_count_before_signature(paragraphs: list[str]) -> int:
    """Count words in every paragraph up to (not including) the signature block."""
    count = 0
    for para in paragraphs:
        first_line = para.split('\n', 1)[0].strip()
        if first_line == 'Best,':
            break
        count += len(para.split())
    return count


def _lint_hard_wraps(paragraphs: list[str]) -> list[str]:
    errors = []
    for i, para in enumerate(paragraphs):
        lines = para.split('\n')
        is_greeting = i == 0
        is_signature = lines[0].strip() == 'Best,'
        if not is_greeting and not is_signature and len(lines) > 1:
            start = lines[0].strip()[:40]
            errors.append(f'hard wrap: paragraph must be a single line, starts "{start}"')
    return errors


def _lint_urls(body: str) -> list[str]:
    errors = []
    for m in _URL_RE.finditer(body):
        url = m.group(0)
        start = m.start()
        if not url.startswith('https://'):
            errors.append(f'URL must use https: "{url}"')
        if url[-1] in '.,)':
            errors.append(f'URL has trailing punctuation glued on: "{url}"')
        prev_char = body[start - 1] if start > 0 else ''
        bracketed = prev_char == '<' and body[m.end():m.end() + 1] == '>'
        if prev_char and not prev_char.isspace() and not bracketed:
            errors.append(f'URL not separated from preceding text: "{url}"')
    return errors


def _lint_signature(body: str) -> list[str]:
    from vcoutreach.web.config import get_workspace
    sender = get_workspace().read_json('sender.json', {}) or {}
    signature = sender.get('signature') or sender.get('name', '')
    return ['Body must end with the saved sender signature.'] if signature and not body.rstrip().endswith(signature) else []


def lint_body(body: str, *, kind: str = "follow_up") -> list[str]:
    """
    Lint an outreach email body. Returns human-readable error strings, empty
    list means pass. Entries prefixed "warn:" are warn-level (word count out
    of range); callers can filter those out.

    `kind` is accepted for future per-kind rule tweaks (e.g. "follow_up" vs
    "warm_reply") and does not change behavior today.
    """
    del kind  # not used yet, kept for API stability
    errors: list[str] = []

    first_line = body.lstrip("\n").split("\n", 1)[0].strip()
    if not re.fullmatch(r"(Hi|Hello|Hey) [^\s,][^,]*,", first_line):
        errors.append(f'greeting missing: first line must be "Hi <Name>," (got "{first_line[:40]}")')


    if _EM_DASH in body:
        errors.append('em dash found (U+2014); rewrite without it')
    if _EN_DASH in body:
        errors.append('en dash found (U+2013); rewrite without it')

    paragraphs = _split_paragraphs(body)
    errors.extend(_lint_hard_wraps(paragraphs))

    lower_body = body.lower()
    for phrase in _FILLER_PHRASES:
        if phrase in lower_body:
            errors.append(f'filler phrase found: "{phrase}"')
    if '!' in body:
        errors.append('exclamation mark found: "!"')

    for phrase in forbidden_phrases():
        if phrase.lower() in lower_body:
            errors.append(f'forbidden phrase found: "{phrase}"')

    if '  ' in body:
        errors.append('double space found in body')
    for line_no, line in enumerate(body.split('\n'), start=1):
        if line != line.rstrip():
            errors.append(f'trailing whitespace on line {line_no}: "{line.rstrip()}"')
    if '\t' in body:
        errors.append('tab character found in body')

    errors.extend(_lint_urls(body))
    errors.extend(_lint_signature(body))

    word_count = _word_count_before_signature(paragraphs)
    if word_count < 50 or word_count > 120:
        errors.append(f'warn: word count before signature is {word_count}, expected 50 to 120')

    return errors


# ---------------------------------------------------------------------------
# Deck mode
# ---------------------------------------------------------------------------

_DECK_SENTENCES: list[str] = [
    "I attached our updated deck.",
    "I attached the updated deck.",
    "The updated deck is attached.",
    "Our updated deck is attached below.",
    "Our updated deck is attached.",
    "Our current deck is attached.",
]

_DECK_MODES = ("attach", "link", "attach_and_link")


_ATTACH_SENTENCE_RE = re.compile(
    r"[^.?!\n]*\bdeck\b[^.?!\n]*\battach[^.?!\n]*\.|[^.?!\n]*\battach[^.?!\n]*\bdeck\b[^.?!\n]*\."
)


def apply_deck_mode(body: str, mode: str, deck_url: str | None) -> str:
    """
    Rewrite the deck sentence in body according to mode.

    "attach"          -- body unchanged.
    "link"            -- a known deck sentence becomes "Here is our updated deck: <{deck_url}>."
    "attach_and_link" -- the existing deck sentence is kept word for word and
                         "You can also view it online: <{deck_url}>." is inserted after it.

    The deck URL is wrapped in angle brackets (RFC 3986 appendix C) so the
    sentence period never becomes part of the signed link in plain-text clients.

    Raises ValueError if mode needs a URL and none is given, if mode is
    unknown, or if no deck sentence is found in body.
    """
    if mode not in _DECK_MODES:
        raise ValueError(f'unknown deck mode: "{mode}"')
    if mode == "attach":
        return body
    if not deck_url:
        raise ValueError(f'deck_url is required for mode "{mode}"')

    if mode == "link":
        replacement = f"Here is our updated deck: <{deck_url}>."
        for sentence in _DECK_SENTENCES:
            if sentence in body:
                return body.replace(sentence, replacement, 1)
        raise ValueError("no known deck sentence found in body")

    match = _ATTACH_SENTENCE_RE.search(body)
    if not match:
        raise ValueError("no deck sentence found in body")
    insert = f" You can also view it online: <{deck_url}>."
    return body[:match.end()] + insert + body[match.end():]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def _linkify(text: str, deck_url: str | None) -> str:
    """Escape text and autolink https URLs, handling the special deck-link phrase."""
    if deck_url:
        href = html.escape(deck_url, quote=True)
        phrases = (
            (f'Here is our updated deck: <{deck_url}>.', f'Here is <a href="{href}">our updated deck</a>.'),
            (f'You can also view it online: <{deck_url}>.', f'You can also <a href="{href}">view it online</a>.'),
        )
        for plain_phrase, html_phrase in phrases:
            idx = text.find(plain_phrase)
            if idx != -1:
                before = text[:idx]
                after = text[idx + len(plain_phrase):]
                return _linkify(before, deck_url) + html_phrase + _linkify(after, deck_url)

    pieces = []
    last = 0
    for m in _URL_RE.finditer(text):
        pieces.append(html.escape(text[last:m.start()]))
        url = m.group(0)
        href = html.escape(url, quote=True)
        if deck_url and url == deck_url:
            anchor_text = 'our updated deck'
        else:
            anchor_text = html.escape(url)
        pieces.append(f'<a href="{href}">{anchor_text}</a>')
        last = m.end()
    pieces.append(html.escape(text[last:]))
    return ''.join(pieces)


def _render_line(line: str, deck_url: str | None) -> str:
    return _linkify(line, deck_url)


def _render_inner(body: str, deck_url: str | None, open_pixel_url: str | None) -> str:
    paragraphs = _split_paragraphs(body)
    divs = []
    for i, para in enumerate(paragraphs):
        if i > 0:
            divs.append('<div><br></div>')
        lines = para.split('\n')
        rendered = [_render_line(line, deck_url) for line in lines]
        divs.append('<div>' + '<br>'.join(rendered) + '</div>')

    content = ''.join(divs)
    if open_pixel_url:
        pixel_src = html.escape(open_pixel_url, quote=True)
        content += f'<img src="{pixel_src}" width="1" height="1" alt="" style="display:block;border:0">'
    return f'<div dir="ltr">{content}</div>'


def render_html(body: str, *, deck_url: str | None = None, open_pixel_url: str | None = None) -> str:
    """
    Render body as a Gmail-native HTML document: outer <div dir="ltr">, one
    <div> per paragraph, a <div><br></div> spacer between paragraphs, and
    greeting/signature lines joined with <br>. No CSS widths, no <pre>, no
    tables, no newline characters inside any paragraph div.
    """
    inner = _render_inner(body, deck_url, open_pixel_url)
    return f'<html><body>{inner}</body></html>'


def paragraph_preview_html(
    body: str,
    *,
    deck_url: str | None = None,
    open_pixel_url: str | None = None,
) -> str:
    """Return only the inner <div dir="ltr">...</div>, for embedding in the CRM review page."""
    return _render_inner(body, deck_url, open_pixel_url)


# ---------------------------------------------------------------------------
# Message assembly
# ---------------------------------------------------------------------------

def build_message(
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[tuple[str, bytes, str]] = (),
    deck_mode: str = "attach",
    deck_url: str | None = None,
    open_pixel_url: str | None = None,
    message_id_domain: str = "outreach-signal.local",
    allow_warnings: bool = True,
) -> EmailMessage:
    """
    Build a Gmail-native multipart/alternative (+ multipart/mixed if there are
    attachments) EmailMessage. Runs apply_deck_mode then lint_body; raises
    ValueError listing errors. Warn-level ("warn:") errors only raise when
    allow_warnings=False. Serialize the result with
    msg.as_bytes(policy=email.policy.SMTP).
    """
    transformed = apply_deck_mode(body, deck_mode, deck_url)

    errors = lint_body(transformed)
    blocking = errors if not allow_warnings else [e for e in errors if not e.startswith('warn:')]
    if blocking:
        raise ValueError('lint_body failed: ' + '; '.join(blocking))

    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = to
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = make_msgid(domain=message_id_domain)
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
    if references:
        msg['References'] = references

    msg.set_content(transformed)
    html_body = render_html(transformed, deck_url=deck_url, open_pixel_url=open_pixel_url)
    msg.add_alternative(html_body, subtype='html')

    for filename, data, mime_type in attachments:
        maintype, subtype = mime_type.split('/', 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    return msg


# ---------------------------------------------------------------------------
# CLI: python -m app.outreach_mime preview <body.txt> <out.html>
# ---------------------------------------------------------------------------

def _cli_preview(body_path: str, out_path: str) -> None:
    body = Path(body_path).read_text(encoding='utf-8')
    doc = render_html(body)
    Path(out_path).write_text(doc, encoding='utf-8')
    print(f'wrote {out_path}')


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 3 and argv[0] == 'preview':
        _cli_preview(argv[1], argv[2])
        return 0
    print('usage: python -m app.outreach_mime preview <body.txt> <out.html>')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
