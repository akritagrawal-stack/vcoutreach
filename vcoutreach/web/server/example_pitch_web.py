"""Template helpers for the example pitch. Registered once in create_app.

Jinja globals added by ``register(app)``:

* ``example_pitch_block(kind=None)``: the data dict that ``_example_pitch.html`` renders. ``kind`` is
  'fundraising' or 'job_search'; ``None`` uses the workspace campaign kind (fundraising until chosen).
* ``campaign_kind_copy(kind=None)``: labels and copy per campaign kind (workspace_settings.CAMPAIGN_KINDS).
* ``draft_carries_example(body, subject='')``: True while a draft still holds the shipped example pitch.
* ``example_draft_slugs()``: set of draft slugs (contact ids) whose body still carries the example.
* ``example_warning_text``: the one-line warning shown on the send plan and the draft editor.
"""
from __future__ import annotations

import logging

from vcoutreach.web.server import example_pitch


def example_draft_slugs() -> set[str]:
    """Slugs of saved drafts that still carry the example pitch. Reads draft files only, never the network."""
    from vcoutreach.types import OutreachError
    from vcoutreach.web import drafts_service
    found: set[str] = set()
    try:
        paths = drafts_service._draft_paths()
    except Exception:
        return found
    for path in paths:
        try:
            draft = drafts_service.read_draft(path)
        except OutreachError:
            logging.getLogger(__name__).warning("Skipping unreadable draft file %s", path.name)
            continue
        if example_pitch.draft_carries_example(draft.body, draft.subject):
            found.add(str(draft.contact_id))
    return found


def example_pitch_block(kind=None) -> dict:
    """The example data for ``kind``, or for the workspace's campaign kind when ``kind`` is None."""
    if kind is None:
        try:
            from vcoutreach.web.server import workspace_settings
            kind = workspace_settings.effective_campaign_kind()
        except Exception:
            kind = example_pitch.DEFAULT_KIND
    return example_pitch.example_pitch(kind)


def campaign_kind_copy(kind=None) -> dict:
    from vcoutreach.web.server import workspace_settings
    return workspace_settings.campaign_kind_copy(kind)


def register(app) -> None:
    app.jinja_env.globals.update(
        example_pitch_block=example_pitch_block,
        campaign_kind_copy=campaign_kind_copy,
        draft_carries_example=example_pitch.draft_carries_example,
        example_draft_slugs=example_draft_slugs,
        example_warning_text=example_pitch.EXAMPLE_WARNING,
    )
