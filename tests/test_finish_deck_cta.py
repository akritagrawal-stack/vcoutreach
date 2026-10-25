"""Deck upload, the "Let Publik do it for you" call to action and the bundled example deck.

Spec only. The tests drive the public HTTP contract through the Flask test client on a throwaway workspace.
No network (the autouse network guard records any attempt), obviously fake key values.
No dash characters appear in this file.

Contracts under test:
* POST /crm/deck/upload (multipart field "deck"), POST /crm/deck/remove, POST /crm/deck/example.
* The campaign page /crm/drafts: the prominent Publik button, the upload control, the missing file wording.
* POST /crm/setup/publik with and without a Publik API key.
* The bundled example deck PDF.

Mutation check (by reasoning, nothing was edited): see the block at the end of this file.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures + autouse env isolation)
from web_helpers import ADA, REPO_ROOT, body, page_text, seed_contacts
from audit_fix_helpers import dom, flash_texts

PDF_A = b"%PDF-1.4\n% deck A\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
PDF_B = b"%PDF-1.4\n% deck B, different bytes\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
MAX_BYTES = 25 * 1024 * 1024
EXAMPLE_DECK = REPO_ROOT / "vcoutreach" / "data" / "examples" / "Publik_Pitch_Deck_Example.pdf"
FAKE_PUBLIK_KEY = "pk_live_ZZ-FAKE-deck-cta-key-0a1b2c3d4e5f6071"

UPLOAD = "/crm/deck/upload"
REMOVE = "/crm/deck/remove"
EXAMPLE = "/crm/deck/example"
CAMPAIGN = "/crm/drafts"


# ---------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------

def upload(web, content=PDF_A, filename="deck.pdf", *, next_url=None, csrf=True, field="deck", follow=False):
    data = {}
    if content is not None:
        data[field] = (io.BytesIO(content), filename)
    if next_url is not None:
        data["next"] = next_url
    if csrf is True:
        data["csrf_token"] = web.csrf
    elif isinstance(csrf, str):
        data["csrf_token"] = csrf
    return web.client.post(UPLOAD, data=data, base_url="http://127.0.0.1:8765",
                           content_type="multipart/form-data", follow_redirects=follow)


def saved_settings(web) -> dict:
    path = web.ws / "data" / "workspace-settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def saved_deck(web):
    value = saved_settings(web).get("pitch_deck_path")
    return Path(value) if value else None


def attachment_files(web):
    root = web.ws / "attachments"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def location_path(response) -> str:
    return urlsplit(response.headers.get("Location", "")).path


def is_redirect(response) -> bool:
    return response.status_code in (301, 302, 303, 307, 308)


def snapshot(root: Path) -> dict:
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def assert_nothing_saved(web):
    assert saved_deck(web) is None, saved_settings(web)
    assert attachment_files(web) == []


def assert_friendly(response, web):
    """A rejected upload is a normal page or redirect with a visible message, never a crash or traceback."""
    assert response.status_code < 500, response.status_code
    if is_redirect(response):
        page = web.client.get(urlsplit(response.headers["Location"]).path, base_url="http://127.0.0.1:8765")
        text = body(page)
        shown = flash_texts(text)
        assert shown and all(t.strip() for t in shown), "a refused upload must explain itself with a flash"
    else:
        text = body(response)
        assert page_text(text).strip(), "empty error page"
    assert "Traceback" not in text
    assert "Internal Server Error" not in text


# ---------------------------------------------------------------------------------------------------------------
# POST /crm/deck/upload: the happy path
# ---------------------------------------------------------------------------------------------------------------

def test_valid_pdf_is_saved_under_workspace_attachments_and_becomes_the_pitch_deck(make_web):
    web = make_web()
    response = upload(web, PDF_A, "my-deck.pdf")
    assert is_redirect(response), response.status_code
    deck = saved_deck(web)
    assert deck is not None, "pitch_deck_path must be saved in the workspace settings"
    assert deck.is_file()
    assert deck.read_bytes() == PDF_A
    assert deck.resolve().parent == (web.ws / "attachments").resolve()


def test_default_redirect_after_a_good_upload_is_the_campaign_page(make_web):
    web = make_web()
    response = upload(web, PDF_A)
    assert location_path(response) == CAMPAIGN


@pytest.mark.parametrize("target", ["/crm/drafts", "/crm/drafts/ada", "/crm/drafts/some-slug-1", "/settings"])
def test_allowed_next_targets_are_honoured(make_web, target):
    web = make_web()
    response = upload(web, PDF_A, next_url=target)
    assert is_redirect(response)
    assert location_path(response) == target


@pytest.mark.parametrize("target", [
    "https://evil.example.com/crm/drafts",
    "//evil.example.com/crm/drafts",
    "/\\evil.example.com",
    "/crm/analytics",
    "/crm",
    "/replies",
    "/crm/drafts/../../etc/passwd",
    "javascript:alert(1)",
    "crm/drafts",
    "",
])
def test_every_other_next_target_falls_back_to_the_campaign_page(make_web, target):
    web = make_web()
    response = upload(web, PDF_A, next_url=target)
    assert is_redirect(response)
    parts = urlsplit(response.headers["Location"])
    assert parts.netloc in ("", "127.0.0.1:8765"), response.headers["Location"]
    assert parts.path == CAMPAIGN, response.headers["Location"]


# ---------------------------------------------------------------------------------------------------------------
# POST /crm/deck/upload: refusals
# ---------------------------------------------------------------------------------------------------------------

def test_a_text_file_renamed_to_pdf_is_refused_with_a_friendly_message(make_web):
    web = make_web()
    response = upload(web, b"this is not a pdf at all", "deck.pdf")
    assert_friendly(response, web)
    assert_nothing_saved(web)


def test_a_non_pdf_extension_is_refused(make_web):
    web = make_web()
    response = upload(web, b"hello, plain notes", "notes.txt")
    assert_friendly(response, web)
    assert_nothing_saved(web)


def test_pdf_magic_in_the_wrong_place_is_not_a_pdf(make_web):
    web = make_web()
    response = upload(web, b"junk before\n%PDF-1.4\n", "deck.pdf")
    assert_friendly(response, web)
    assert_nothing_saved(web)


def test_an_empty_file_is_refused(make_web):
    web = make_web()
    response = upload(web, b"", "deck.pdf")
    assert_friendly(response, web)
    assert_nothing_saved(web)


def test_a_post_with_no_file_at_all_is_refused(make_web):
    web = make_web()
    response = upload(web, None)
    assert_friendly(response, web)
    assert_nothing_saved(web)


def test_a_file_under_the_wrong_field_name_is_treated_as_no_file(make_web):
    web = make_web()
    response = upload(web, PDF_A, "deck.pdf", field="attachment")
    assert_friendly(response, web)
    assert_nothing_saved(web)


def test_a_pdf_over_25_mb_is_refused_and_nothing_is_saved(make_web):
    web = make_web()
    big = b"%PDF-1.4\n" + b"0" * (MAX_BYTES + 1024)
    response = upload(web, big, "huge.pdf")
    assert response.status_code < 500, response.status_code
    assert_nothing_saved(web)
    if is_redirect(response) or response.status_code == 200:
        assert_friendly(response, web)
    else:
        assert response.status_code in (400, 413)
        assert "Traceback" not in body(response)


def test_a_pdf_just_under_the_limit_is_accepted(make_web):
    web = make_web()
    near = b"%PDF-1.4\n" + b"0" * (MAX_BYTES - 4096)
    response = upload(web, near, "big-but-fine.pdf")
    assert is_redirect(response), response.status_code
    assert saved_deck(web) is not None


def test_a_refused_upload_keeps_the_previous_deck(make_web):
    web = make_web()
    upload(web, PDF_A, "first.pdf")
    before = saved_deck(web)
    assert before is not None
    assert_friendly(upload(web, b"not a pdf", "second.pdf"), web)
    assert saved_deck(web) == before
    assert before.read_bytes() == PDF_A


def test_missing_csrf_is_rejected_like_other_post_routes(make_web):
    web = make_web()
    response = upload(web, PDF_A, csrf=False)
    assert response.status_code == 400
    assert_nothing_saved(web)


def test_a_wrong_csrf_token_is_rejected(make_web):
    web = make_web()
    response = upload(web, PDF_A, csrf="not-a-real-token")
    assert response.status_code == 400
    assert_nothing_saved(web)


def test_the_upload_route_does_not_accept_get(make_web):
    web = make_web()
    assert web.get(UPLOAD).status_code in (404, 405)


# ---------------------------------------------------------------------------------------------------------------
# POST /crm/deck/upload: filenames and collisions
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "../../x.pdf",
    "../x.pdf",
    "..\\..\\x.pdf",
    "/etc/x.pdf",
    "sub/dir/x.pdf",
    "....//....//x.pdf",
])
def test_path_traversal_in_the_filename_never_escapes_attachments(make_web, tmp_path, name):
    web = make_web()
    before = set(snapshot(tmp_path))
    response = upload(web, PDF_A, name)
    assert response.status_code < 500
    attachments = (web.ws / "attachments").resolve()
    for path_text, data in snapshot(tmp_path).items():
        if path_text in before:
            continue
        if data.startswith(b"%PDF-"):
            assert Path(path_text).resolve().is_relative_to(attachments), f"PDF written outside attachments: {path_text}"
    deck = saved_deck(web)
    if deck is not None:
        assert deck.resolve().is_relative_to(attachments), deck
    assert not (tmp_path / "x.pdf").exists()
    assert not (web.ws / "x.pdf").exists()
    assert not Path("/etc/x.pdf").exists()


def test_same_name_with_different_bytes_does_not_clobber_the_existing_file(make_web):
    web = make_web()
    upload(web, PDF_A, "deck.pdf")
    first = saved_deck(web)
    assert first is not None
    upload(web, PDF_B, "deck.pdf")
    contents = [p.read_bytes() for p in attachment_files(web)]
    assert PDF_A in contents, "the first deck's bytes were overwritten"
    assert PDF_B in contents, "the second deck was not stored"
    assert first.read_bytes() == PDF_A
    second = saved_deck(web)
    assert second is not None and second.read_bytes() == PDF_B
    assert second != first


def test_uploading_the_same_bytes_twice_keeps_a_working_deck(make_web):
    web = make_web()
    upload(web, PDF_A, "deck.pdf")
    upload(web, PDF_A, "deck.pdf")
    deck = saved_deck(web)
    assert deck is not None and deck.read_bytes() == PDF_A


# ---------------------------------------------------------------------------------------------------------------
# remove and example
# ---------------------------------------------------------------------------------------------------------------

def test_remove_clears_the_pitch_deck_setting(make_web):
    web = make_web()
    upload(web, PDF_A)
    assert saved_deck(web) is not None
    response = web.post(REMOVE, {})
    assert response.status_code < 500
    assert not saved_deck(web)


def test_remove_without_a_deck_is_harmless(make_web):
    web = make_web()
    response = web.post(REMOVE, {}, follow=True)
    assert response.status_code == 200
    assert "Traceback" not in body(response)
    assert not saved_deck(web)


def test_remove_needs_csrf(make_web):
    web = make_web()
    upload(web, PDF_A)
    response = web.post(REMOVE, {}, csrf=False)
    assert response.status_code == 400
    assert saved_deck(web) is not None


def test_example_sets_the_bundled_example_deck(make_web):
    web = make_web()
    response = web.post(EXAMPLE, {})
    assert response.status_code < 500
    deck = saved_deck(web)
    assert deck is not None, "the example deck must become the pitch deck"
    assert deck.name.endswith("Publik_Pitch_Deck_Example.pdf") or "Example" in deck.name, deck
    assert deck.is_file()
    assert deck.read_bytes().startswith(b"%PDF-")


def test_example_needs_csrf(make_web):
    web = make_web()
    assert web.post(EXAMPLE, {}, csrf=False).status_code == 400
    assert saved_deck(web) is None


def test_example_then_remove_leaves_no_deck(make_web):
    web = make_web()
    web.post(EXAMPLE, {})
    web.post(REMOVE, {})
    assert not saved_deck(web)


# ---------------------------------------------------------------------------------------------------------------
# The campaign page
# ---------------------------------------------------------------------------------------------------------------

def norm(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'")


def test_campaign_page_has_the_prominent_let_publik_do_it_button(make_web):
    web = make_web()
    page = dom(web, CAMPAIGN)
    controls = [el for el in page.select("button, a, input[type=submit]")
                if "let publik do it for you" in norm(el.get_text(" ", strip=True) or el.get("value", "")).lower()]
    assert controls, "no 'Let Publik do it for you' control on the campaign page"
    control = controls[0]
    if control.name == "button" or control.name == "input":
        form = control.find_parent("form")
        assert form is not None and "/crm/setup/publik" in (form.get("action") or "")
        assert (form.get("method") or "get").lower() == "post"
    else:
        assert control.get("href"), "the call to action must go somewhere"


def test_campaign_page_has_a_deck_upload_control(make_web):
    web = make_web()
    page = dom(web, CAMPAIGN)
    file_inputs = page.select('input[type="file"][name="deck"]')
    assert file_inputs, "no file input named 'deck' on the campaign page"
    form = file_inputs[0].find_parent("form")
    assert form is not None
    assert "/crm/deck/upload" in (form.get("action") or "")
    assert (form.get("method") or "").lower() == "post"
    assert "multipart/form-data" in (form.get("enctype") or "").lower()
    assert form.select_one('input[name="csrf_token"]') is not None


def test_the_upload_form_works_end_to_end_from_the_page(make_web):
    web = make_web()
    page = dom(web, CAMPAIGN)
    form = page.select_one('input[type="file"][name="deck"]').find_parent("form")
    data = {i.get("name"): i.get("value", "") for i in form.select("input[name]")
            if (i.get("type") or "text") not in ("file", "submit", "button")}
    data["deck"] = (io.BytesIO(PDF_A), "from-the-page.pdf")
    response = web.client.post(form.get("action"), data=data, base_url="http://127.0.0.1:8765",
                               content_type="multipart/form-data")
    assert is_redirect(response)
    deck = saved_deck(web)
    assert deck is not None and deck.read_bytes() == PDF_A


def test_campaign_page_never_shows_the_old_attachments_folder_wording(make_web, tmp_path):
    web = make_web()
    phrase = "not inside the workspace attachments folder"
    assert phrase not in page_text(body(web.get(CAMPAIGN))).lower()
    outside = tmp_path / "elsewhere" / "outside.pdf"
    outside.parent.mkdir()
    outside.write_bytes(PDF_A)
    settings = web.ws / "data" / "workspace-settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"pitch_deck_path": str(outside)}))
    assert phrase not in norm(page_text(body(web.get(CAMPAIGN)))).lower()
    web2 = make_web()
    upload(web2, PDF_A)
    assert phrase not in page_text(body(web2.get(CAMPAIGN))).lower()


def test_missing_deck_file_says_we_cant_find_your_deck_file(make_web):
    web = make_web()
    upload(web, PDF_A, "will-vanish.pdf")
    deck = saved_deck(web)
    assert deck is not None
    deck.unlink()
    text = norm(page_text(body(web.get(CAMPAIGN))))
    assert "We can't find your deck file. Attach it again." in text
    assert "not inside the workspace attachments folder" not in text.lower()


def test_a_present_deck_does_not_show_the_missing_file_message(make_web):
    web = make_web()
    upload(web, PDF_A)
    text = norm(page_text(body(web.get(CAMPAIGN))))
    assert "We can't find your deck file" not in text


def test_no_deck_set_does_not_show_the_missing_file_message(make_web):
    web = make_web()
    text = norm(page_text(body(web.get(CAMPAIGN))))
    assert "We can't find your deck file" not in text


# ---------------------------------------------------------------------------------------------------------------
# POST /crm/setup/publik
# ---------------------------------------------------------------------------------------------------------------

def test_publik_setup_without_a_key_makes_no_network_call_and_points_to_ai_setup(make_web, monkeypatch, network_guard):
    from vcoutreach.providers import http as _http

    calls = []

    def spy(self, method, url, headers, payload, timeout):
        calls.append(url)
        raise AssertionError("no transport call is allowed without a Publik API key")

    monkeypatch.setattr(_http.UrllibTransport, "request", spy)
    web = make_web()
    seed_contacts(web, [ADA])
    response = web.post("/crm/setup/publik", {})
    assert is_redirect(response), response.status_code
    assert location_path(response) == "/setup/ai"
    page = web.get("/setup/ai")
    assert page.status_code == 200
    followed = web.post("/crm/setup/publik", {}, follow=True)
    assert followed.status_code == 200
    shown = " ".join(flash_texts(body(followed))).lower()
    assert "publik" in shown and "api key" in shown, shown
    assert calls == []
    assert network_guard.attempts == []
    assert web.transport.calls == []


def test_publik_setup_without_a_key_needs_csrf(make_web):
    web = make_web()
    assert web.post("/crm/setup/publik", {}, csrf=False).status_code == 400


def test_publik_setup_with_a_key_uses_the_transport_saves_a_result_and_sends_no_email(make_web, monkeypatch):
    from vcoutreach.providers import http as _http

    calls = []
    reply = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "Looks good. Nothing to change."}}]}

    def fake(self, method, url, headers, payload, timeout):
        calls.append((method, url))
        return _http.HttpResponse(200, {"content-type": "application/json"}, json.dumps(reply).encode())

    monkeypatch.setattr(_http.UrllibTransport, "request", fake)
    web = make_web()
    seed_contacts(web, [ADA])
    saved = web.post("/api/model-provider/key", {"provider": "publik", "api_key": FAKE_PUBLIK_KEY})
    assert saved.status_code < 500
    before = snapshot(web.ws)
    response = web.post("/crm/setup/publik", {}, follow=True)
    assert response.status_code == 200
    assert "Traceback" not in body(response)
    assert FAKE_PUBLIK_KEY not in body(response)
    shown = " ".join(flash_texts(body(response))).lower()
    assert "add your publik api key" not in shown
    assert calls, "with a key saved the setup must go through the (faked) provider transport"
    assert web.transport.calls == [], "setting up with Publik must never send an email"
    after = snapshot(web.ws)
    assert after != before, "a result must be saved in the workspace"


# ---------------------------------------------------------------------------------------------------------------
# The bundled example deck PDF
# ---------------------------------------------------------------------------------------------------------------

def pdf_page_count() -> int:
    if shutil.which("pdfinfo"):
        out = subprocess.run(["pdfinfo", str(EXAMPLE_DECK)], capture_output=True, text=True, timeout=60).stdout
        match = re.search(r"^Pages:\s+(\d+)", out, re.M)
        if match:
            return int(match.group(1))
    try:
        import pypdf  # type: ignore
        return len(pypdf.PdfReader(str(EXAMPLE_DECK)).pages)
    except ImportError:
        pytest.skip("neither pdfinfo nor pypdf is available")


def pdf_text() -> str:
    if shutil.which("pdftotext"):
        return subprocess.run(["pdftotext", "-layout", str(EXAMPLE_DECK), "-"], capture_output=True, text=True,
                              timeout=120).stdout
    try:
        import pypdf  # type: ignore
        return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(str(EXAMPLE_DECK)).pages)
    except ImportError:
        pytest.skip("neither pdftotext nor pypdf is available")


def test_example_deck_is_a_real_pdf_with_nine_pages():
    assert EXAMPLE_DECK.is_file(), EXAMPLE_DECK
    assert EXAMPLE_DECK.read_bytes().startswith(b"%PDF-")
    assert pdf_page_count() == 9


def _old_deck_md5s():
    from vcoutreach.web.server.workspace_settings import KNOWN_OLD_EXAMPLE_DECK_MD5
    return set(KNOWN_OLD_EXAMPLE_DECK_MD5)


def test_example_deck_is_the_new_image_only_deck_not_the_old_one():
    """The example deck is image-only on purpose (redaction is burned into the pixels), so no text is
    extractable. Check page count and that it is not the known older bundled deck."""
    import hashlib
    assert pdf_page_count() == 9
    digest = hashlib.md5(EXAMPLE_DECK.read_bytes()).hexdigest()
    assert digest not in _old_deck_md5s(), "bundled example deck is still the old known deck"


def test_example_deck_pixels_show_placeholders_not_real_numbers(tmp_path):
    """OCR the TAM page (5) and the raise page (8); skipped when OCR tooling is absent."""
    if not shutil.which("pdftoppm"):
        pytest.skip("pdftoppm is not on PATH, cannot render deck pages for OCR")
    if not shutil.which("tesseract"):
        try:
            import pytesseract  # type: ignore  # noqa: F401
        except ImportError:
            pytest.skip("neither tesseract nor pytesseract is available, OCR check not run")
        if not shutil.which("tesseract"):
            pytest.skip("pytesseract needs the tesseract binary, which is not on PATH")

    def ocr(page: int) -> str:
        prefix = tmp_path / f"p{page}"
        subprocess.run(["pdftoppm", "-r", "150", "-f", str(page), "-l", str(page), "-png",
                        str(EXAMPLE_DECK), str(prefix)], check=True, timeout=120)
        png = sorted(tmp_path.glob(f"p{page}*.png"))[0]
        out = subprocess.run(["tesseract", str(png), "-"], capture_output=True, text=True, timeout=120)
        return " ".join(out.stdout.split())

    page5, page8 = ocr(5), ocr(8)
    assert "TAM" in page5 and "SAM" in page5, page5
    assert "Raising" in page8, page8
    combined = page5 + " " + page8
    assert re.search(r"\b[XY]\b", combined), "placeholders X or Y must be visible in the deck pixels"
    assert "25M+" not in combined


def test_example_deck_has_no_em_or_en_dash():
    text = pdf_text()
    assert chr(0x2014) not in text
    assert chr(0x2013) not in text


# ---------------------------------------------------------------------------------------------------------------
# MUTATION CHECK (by reasoning, no source was edited)
#
# If the POST /crm/deck/upload route were deleted, every request to it would be a 404 (or 405 if only GET
# siblings matched). These tests would then FAIL:
#   * test_valid_pdf_is_saved_under_workspace_attachments_and_becomes_the_pitch_deck (needs a redirect and a saved path)
#   * test_default_redirect_after_a_good_upload_is_the_campaign_page
#   * test_allowed_next_targets_are_honoured (4 params)
#   * test_every_other_next_target_falls_back_to_the_campaign_page (10 params; is_redirect fails)
#   * test_a_pdf_just_under_the_limit_is_accepted
#   * test_a_text_file_renamed_to_pdf..., test_a_non_pdf_extension_is_refused, test_pdf_magic_in_the_wrong_place...,
#     test_an_empty_file_is_refused, test_a_post_with_no_file_at_all..., test_a_file_under_the_wrong_field_name...
#     (assert_friendly needs a flash, a 404 page has none)
#   * test_a_pdf_over_25_mb_is_refused_and_nothing_is_saved (status 404 is not in the allowed 400/413 set)
#   * test_a_refused_upload_keeps_the_previous_deck (first upload never saves, so `before` is None)
#   * test_missing_csrf_is_rejected..., test_a_wrong_csrf_token_is_rejected (404 is not 400)
#   * test_the_upload_route_does_not_accept_get passes either way (404 is allowed), so it would NOT catch it
#   * test_same_name_with_different_bytes..., test_uploading_the_same_bytes_twice..., test_remove_clears...
#     (setup upload never saves), test_remove_needs_csrf, test_missing_deck_file_says_we_cant_find_your_deck_file
#   * test_the_upload_form_works_end_to_end_from_the_page (form action 404s; also the file input is gone)
#   * test_campaign_page_has_a_deck_upload_control (if the form is removed along with the route)
# Tests that would still PASS: path traversal (nothing is written, which is vacuously safe, only the saved-path
# check stays quiet), the example/remove routes that do not need an upload, the Publik setup tests, the PDF
# content tests. The traversal tests are therefore guarded only by their sibling happy path tests, which is
# acceptable: a missing route cannot write outside attachments.
# ---------------------------------------------------------------------------------------------------------------
