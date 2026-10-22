"""Small shared helpers for the audit-fix test files (test support only, no expectations live here)."""
from __future__ import annotations

import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from bs4 import BeautifulSoup

from web_helpers import (
    csv_row, body, contact_ref, page_text, read_draft_file, seed_contacts,
)

PAT = csv_row(id="pat", name="Pat Quinn", email="pat@example.com", org="Quill Co", role="Partner")
REX = csv_row(id="rex", name="Rex Hale", email="rex@example.com", org="Hale Co", role="Founder")
SUE = csv_row(id="sue", name="Sue Marsh", email="sue@example.com", org="Marsh Labs", role="Principal")
NO_FACT_ROWS = [PAT, REX, SUE]

TEST_INBOX_ADDR = "inbox@testmail.example.org"


def dom(web, path: str, **kw) -> BeautifulSoup:
    return BeautifulSoup(body(web.get(path, **kw)), "html.parser")


def soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "html.parser")


def flashes(markup: str) -> List[Tuple[str, List[str]]]:
    """Every flash as (visible text without the dismiss glyph, hrefs of links inside it)."""
    out = []
    for li in soup(markup).select("li.flash"):
        links = [a.get("href", "") for a in li.select("a[href]")]
        for btn in li.select("button"):
            btn.decompose()
        out.append((li.get_text(" ", strip=True), links))
    return out


def flash_texts(markup: str) -> List[str]:
    return [t for t, _ in flashes(markup)]


def write_drafts(web, provider: str = "template"):
    return web.post(web.url("drafts_write"), {"provider": provider}, follow=True)


def form_data(form) -> Dict[str, str]:
    """The fields a browser would submit for a form, with their pre-filled values."""
    data: Dict[str, str] = {}
    for el in form.select("input, textarea, select"):
        name = el.get("name")
        if not name:
            continue
        if el.name == "textarea":
            data[name] = el.get_text() or ""
        elif el.name == "select":
            chosen = el.select_one("option[selected]") or el.select_one("option")
            data[name] = chosen.get("value", chosen.get_text()) if chosen else ""
        else:
            kind = (el.get("type") or "text").lower()
            if kind in ("checkbox", "radio") and not el.has_attr("checked"):
                continue
            if kind in ("submit", "button", "file"):
                continue
            data[name] = el.get("value", "")
    return data


def find_form(root, *, action_contains: Optional[str] = None, textarea_like: Optional[str] = None):
    for form in root.select("form"):
        action = form.get("action") or ""
        if action_contains and action_contains not in action:
            continue
        if textarea_like and not any(textarea_like in (t.get("name") or "").lower() for t in form.select("textarea")):
            continue
        return form
    return None


def submit_form(web, form, overrides: Mapping[str, str], *, follow: bool = True, fallback_path: str = ""):
    data = form_data(form)
    data.update(overrides)
    action = form.get("action") or fallback_path
    return web.post(action, data, csrf=data.get("csrf_token", True) if data.get("csrf_token") else True, follow=follow)


def facts_of(web, email: str) -> List[str]:
    from web_helpers import export_rows

    for row in export_rows(web):
        if row["email"] == email.lower():
            raw = row.get("facts", "")
            return [p.strip() for p in raw.split(" | ") if p.strip()] if raw else []
    return []


def words(text: str) -> int:
    return len(re.findall(r"\b[\w'$+%-]+\b", text))


def has_dash(text: str) -> bool:
    return chr(0x2014) in text or chr(0x2013) in text
