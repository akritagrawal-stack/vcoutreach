"""The app owns attachments/Example_Pitch_Deck.pdf: a stale copy is refreshed from the bundled deck.

Spec only. Drives the public ensure_example_deck() and bundled_example_deck() inside a Flask app context
on a throwaway workspace. No network, no dash characters in this file.

Contracts under test:
* After ensure_example_deck(), attachments/Example_Pitch_Deck.pdf is byte identical to the bundled deck,
  even when it held other bytes and even when the data/.example_deck_seeded marker already exists.
* A user's own deck under a different file name is never touched.
* A copy that already equals the bundled deck stays unchanged.
* The pitch_deck_path setting pointing at that file is unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from web_helpers import *  # noqa: F401,F403  (fixtures + autouse env isolation)

from vcoutreach.web.server import workspace_settings as ws_settings

STALE = b"%PDF-1.4 stale unredacted deck"
USER_DECK = b"%PDF-1.4\n% my own deck, hands off\n%%EOF\n"
NAME = "Example_Pitch_Deck.pdf"


def _bundled_bytes() -> bytes:
    return Path(ws_settings.bundled_example_deck()).read_bytes()


def _example(web) -> Path:
    return web.ws / "attachments" / NAME


def _settings(web) -> dict:
    path = web.ws / "data" / "workspace-settings.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _run_ensure(web):
    with web.app.app_context():
        return ws_settings.ensure_example_deck()


def _plant(web, content: bytes, *, marker: bool) -> Path:
    target = _example(web)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    if marker:
        mark = web.ws / "data" / ".example_deck_seeded"
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text("seeded", encoding="utf-8")
    return target


def _point_setting_at(web, target: Path) -> None:
    path = web.ws / "data" / "workspace-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _settings(web)
    data["pitch_deck_path"] = str(target)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_bundled_deck_is_a_real_pdf():
    data = _bundled_bytes()
    assert data.startswith(b"%PDF") and len(data) > len(STALE)


@pytest.mark.parametrize("marker", [False, True], ids=["no-marker", "marker-present"])
def test_stale_bytes_are_replaced_by_the_bundled_deck(web, marker):
    target = _plant(web, STALE, marker=marker)
    assert target.read_bytes() == STALE
    _run_ensure(web)
    assert target.read_bytes() == _bundled_bytes()


def test_longer_stale_copy_with_marker_is_also_replaced(web):
    target = _plant(web, b"%PDF-1.4 another old build, longer than the stale one " * 20, marker=True)
    _run_ensure(web)
    assert target.read_bytes() == _bundled_bytes()


def test_already_current_copy_stays_unchanged(web):
    bundled = _bundled_bytes()
    target = _plant(web, bundled, marker=True)
    _run_ensure(web)
    assert target.read_bytes() == bundled
    _run_ensure(web)
    assert target.read_bytes() == bundled
    assert sorted(p.name for p in target.parent.iterdir()) == [NAME]


def test_users_own_deck_with_another_name_is_never_touched(web):
    target = _plant(web, STALE, marker=True)
    own = target.parent / "My_Own_Deck.pdf"
    own.write_bytes(USER_DECK)
    _run_ensure(web)
    assert own.read_bytes() == USER_DECK
    assert target.read_bytes() == _bundled_bytes()


def test_users_own_deck_survives_when_example_copy_is_current(web):
    own = web.ws / "attachments" / "Founder_Deck_v3.pdf"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_bytes(USER_DECK)
    _plant(web, _bundled_bytes(), marker=True)
    _run_ensure(web)
    assert own.read_bytes() == USER_DECK


def test_deck_path_setting_pointing_at_the_example_is_unchanged(web):
    target = _plant(web, STALE, marker=True)
    _point_setting_at(web, target)
    before = _settings(web).get("pitch_deck_path")
    assert before == str(target)
    _run_ensure(web)
    assert _settings(web).get("pitch_deck_path") == before
    assert target.read_bytes() == _bundled_bytes()


def test_deck_path_setting_pointing_at_a_users_own_deck_is_unchanged(web):
    own = web.ws / "attachments" / "Founder_Deck_v3.pdf"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_bytes(USER_DECK)
    _plant(web, STALE, marker=True)
    _point_setting_at(web, own)
    _run_ensure(web)
    assert _settings(web).get("pitch_deck_path") == str(own)
    assert own.read_bytes() == USER_DECK
