"""Gaps found by the second mutation run: config.Settings and load_settings (SPEC sections 11 and 17)."""
from __future__ import annotations

from pathlib import Path

import pytest

from vcoutreach.config import Settings, load_settings
from vcoutreach.types import OutreachError

NO_DOTENV = Path("/nonexistent-dir-for-vcoutreach-tests/.env")


def load(env):
    return load_settings(env=env, dotenv_path=NO_DOTENV)


# ----- the default of the dataclass itself is the safe one, not only what load_settings makes of an empty environment ----------------

def test_a_settings_object_built_with_no_arguments_is_in_test_mode():
    assert Settings().send_mode == "test"


def test_a_settings_object_built_with_no_arguments_agrees_with_an_empty_environment():
    bare, loaded = Settings(), load({})
    for name in ("send_mode", "daily_send_cap", "publik_model", "publik_max_retries", "publik_timeout_seconds", "openai_base_url"):
        assert getattr(bare, name) == getattr(loaded, name), name


def test_a_settings_object_built_with_no_arguments_has_no_keys():
    bare = Settings()
    assert not bare.publik_api_key and not bare.openai_api_key and not bare.anthropic_api_key
    assert not bare.test_recipient_email


# ----- numbers that make no sense are refused at load time ("a clean error", not a silent odd limit) -----------------------------------

@pytest.mark.parametrize("name,value", [("DAILY_SEND_CAP", "-5"), ("DAILY_SEND_CAP", "-1"),
                                        ("PUBLIK_MAX_RETRIES", "-1"), ("PUBLIK_MAX_RETRIES", "-3")])
def test_a_negative_count_is_refused(name, value):
    with pytest.raises(OutreachError):
        load({name: value})


@pytest.mark.parametrize("value", ["0", "0.0", "-1", "-0.5", "inf", "-inf"], ids=["zero", "zero-float", "negative", "negative-float", "inf", "minus-inf"])
def test_a_timeout_that_is_not_greater_than_zero_is_refused(value):
    with pytest.raises(OutreachError):
        load({"PUBLIK_TIMEOUT_SECONDS": value})


def test_zero_is_a_valid_cap_and_a_valid_retry_count_and_a_tiny_timeout_is_fine():
    s = load({"DAILY_SEND_CAP": "0", "PUBLIK_MAX_RETRIES": "0", "PUBLIK_TIMEOUT_SECONDS": "0.5"})
    assert (s.daily_send_cap, s.publik_max_retries, s.publik_timeout_seconds) == (0, 0, 0.5)
