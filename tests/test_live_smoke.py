"""One opt-in smoke test against the real Publik API. It costs real money, so it is skipped
unless BOTH of these are set in the environment that launches pytest:

    PUBLIK_LIVE_SMOKE=1
    PUBLIK_API_KEY=<your key>

It makes exactly one tiny call (the cheapest model, no retries) and never prints the key.
"""
from __future__ import annotations

import os

import pytest

# Captured at import time on purpose: the suite's autouse fixtures scrub the environment
# before each test body runs, and the key must never be passed around as a fixture value
# (pytest would show it in failure reports).
_KEY = os.environ.get("PUBLIK_API_KEY", "")
_ENABLED = os.environ.get("PUBLIK_LIVE_SMOKE") == "1" and bool(_KEY)

pytestmark = pytest.mark.live


@pytest.mark.skipif(not _ENABLED, reason="set PUBLIK_LIVE_SMOKE=1 and PUBLIK_API_KEY to run (costs real money)")
def test_one_real_publik_call_returns_a_non_empty_hook(kit):
    from vcoutreach.providers import PublikProvider
    from vcoutreach.types import Hook

    settings = kit.settings(PUBLIK_API_KEY=_KEY, PUBLIK_MODEL="publik-fast", PUBLIK_MAX_RETRIES="1")
    request = kit.request(
        contact=kit.contact(facts=("your essay on how small teams ship weekly",)),
    )
    hook = PublikProvider(settings).personalize(request)

    assert isinstance(hook, Hook)
    assert hook.provider == "publik"
    assert hook.text.strip(), "the live call returned an empty hook"
    leaked = _KEY in repr(hook) or _KEY in hook.text
    assert not leaked, "the API key appeared in the hook (value deliberately not shown)"
