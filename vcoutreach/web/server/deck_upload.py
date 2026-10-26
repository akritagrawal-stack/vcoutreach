"""Deck (or resume) upload, removal and example-deck helpers for the Template and deck panel.

The upload route lives in app.py; everything that touches files or settings lives here so it can be
tested without a request. Files only ever land inside <workspace>/attachments. Nothing here sends mail
or calls a network service.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path

from vcoutreach.web.config import get_workspace

MAX_DECK_BYTES = 25 * 1024 * 1024
PDF_MAGIC = b'%PDF-'
PDF_EOF = b'%%EOF'
PDF_TYPE = b'/Type'
_EOF_WINDOW = 2048
_STEM_LIMIT = 80
_BACK_PATH = re.compile(r'^/(?:crm/drafts(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,239})?|settings)$')

NO_FILE = 'Choose a PDF file to upload.'
EMPTY_FILE = 'That file is empty. Choose a PDF file to upload.'
TOO_LARGE = 'That file is larger than 25 MB.'


class DeckUploadError(ValueError):
    """A user-facing reason the upload was refused. The message is safe to flash."""


def noun(job_search: bool) -> str:
    return 'Resume' if job_search else 'Deck'


def not_a_pdf(job_search: bool) -> str:
    return f'That file is not a PDF. Upload a PDF {noun(job_search).lower()}.'


def damaged_pdf(job_search: bool) -> str:
    return f'That file looks damaged or incomplete. Export the {noun(job_search).lower()} as a PDF again and upload it.'


def looks_complete(data: bytes) -> bool:
    """Cheap structural check, no parser: a real PDF ends with an %%EOF marker and declares at least one /Type object."""
    return PDF_EOF in data[-_EOF_WINDOW:] and PDF_TYPE in data


def back_path(value: object, default: str = '/crm/drafts') -> str:
    """A same-site return path from the posted ``next`` value: the Campaign page, a draft editor or Settings."""
    text = str(value or '').strip()
    return text if _BACK_PATH.fullmatch(text) else default


def safe_pdf_name(filename: object) -> str:
    """Reduce a client filename to a safe ``<stem>.pdf``: ASCII letters, digits, dot, dash, underscore."""
    base = str(filename or '').replace('\\', '/').rsplit('/', 1)[-1]
    stem = base.rsplit('.', 1)[0] if '.' in base else base
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', stem).strip('._-')[:_STEM_LIMIT].strip('._-')
    return (stem or 'deck') + '.pdf'


def label_from_name(saved_name: str) -> str:
    """Human label for the settings page, from the saved filename."""
    stem = saved_name.rsplit('.', 1)[0]
    return (re.sub(r'[_\-]+', ' ', stem).strip() or 'Pitch deck')[:160]


def _read_limited(storage) -> bytes:
    stream = getattr(storage, 'stream', None)
    if stream is None:
        raise DeckUploadError(NO_FILE)
    data = stream.read(MAX_DECK_BYTES + 1)
    if len(data) > MAX_DECK_BYTES:
        raise DeckUploadError(TOO_LARGE)
    return data


def _write_atomic(target: Path, data: bytes) -> None:
    temporary = target.with_name('.upload-' + secrets.token_hex(8) + '.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _set_deck(path: str, label: str) -> None:
    from vcoutreach.web.server.workspace_settings import _persist_settings, load_settings, normalize_settings
    settings = load_settings()
    settings['pitch_deck_path'] = path
    settings['pitch_deck_label'] = label
    # An uploaded file replaces any deck link, so the panel shows exactly one deck.
    settings['pitch_deck_url'] = ''
    _persist_settings(normalize_settings(settings))


def save_uploaded_deck(storage, job_search: bool = False) -> str:
    """Validate and store one uploaded PDF, make it the configured deck, and return the saved filename.

    Raises DeckUploadError with a flashable message when the file is missing, empty, over 25 MB, not a PDF or damaged (no %%EOF marker or no /Type object).
    """
    if storage is None or not getattr(storage, 'filename', ''):
        raise DeckUploadError(NO_FILE)
    data = _read_limited(storage)
    if not data:
        raise DeckUploadError(EMPTY_FILE)
    if not data.startswith(PDF_MAGIC):
        raise DeckUploadError(not_a_pdf(job_search))
    if not looks_complete(data):
        raise DeckUploadError(damaged_pdf(job_search))
    workspace = get_workspace()
    folder = workspace.path('attachments')
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_pdf_name(storage.filename)
    target = workspace.path('attachments', name)
    if target.exists() or target.is_symlink():
        try:
            same = target.is_file() and not target.is_symlink() and target.read_bytes() == data
        except OSError:
            same = False
        if not same:
            name = name[:-4] + '-' + secrets.token_hex(3) + '.pdf'
            target = workspace.path('attachments', name)
    if not target.exists():
        _write_atomic(target, data)
    _set_deck(str(target), label_from_name(name))
    return name


def remove_deck() -> bool:
    """Clear the configured deck path and link. Files on disk are left alone. Returns True if something was set."""
    from vcoutreach.web.server.workspace_settings import _persist_settings, load_settings, normalize_settings
    settings = load_settings()
    had = bool(settings.get('pitch_deck_path') or settings.get('pitch_deck_url'))
    settings['pitch_deck_path'] = ''
    settings['pitch_deck_url'] = ''
    _persist_settings(normalize_settings(settings))
    return had


def use_example_deck() -> str:
    """Copy the bundled example deck into attachments and make it the configured deck. Returns its filename."""
    from vcoutreach.web.server import example_pitch
    from vcoutreach.web.server.workspace_settings import EXAMPLE_DECK_NAME, bundled_example_deck
    source = bundled_example_deck()
    if not source.is_file():
        raise DeckUploadError('The example deck is not available in this build.')
    workspace = get_workspace()
    workspace.path('attachments').mkdir(parents=True, exist_ok=True)
    target = workspace.path('attachments', EXAMPLE_DECK_NAME)
    if target.is_symlink():
        raise DeckUploadError('The example deck could not be placed in your attachments folder.')
    if not target.is_file() or target.read_bytes() != source.read_bytes():
        # Overwriting is safe here: a file with this name that differs from the bundled copy is a stale seed.
        shutil.copyfile(source, target)
    _set_deck(str(target), example_pitch.EXAMPLE_DECK_LABEL)
    return EXAMPLE_DECK_NAME
