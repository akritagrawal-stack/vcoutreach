"""config.load_settings and Settings (SPEC section 11 and section 17)."""
from __future__ import annotations

import dataclasses
import importlib.resources as resources
import os
from pathlib import Path

import pytest

from vcoutreach.config import Settings, load_settings
from vcoutreach.types import OutreachError

NO_DOTENV = Path("/nonexistent-dir-for-vcoutreach-tests/.env")

KEYS = {
    "PUBLIK_API_KEY": "pk_fake_AAAAAAAAAAAAAAAAAAAA1111",
    "OPENAI_API_KEY": "ok_fake_BBBBBBBBBBBBBBBBBBBB2222",
    "ANTHROPIC_API_KEY": "ak_fake_CCCCCCCCCCCCCCCCCCCC3333",
}


def load(env=None, dotenv=NO_DOTENV):
    return load_settings(env=env if env is not None else {}, dotenv_path=dotenv)


def write_env(kit, text, name=".env"):
    path = kit.workdir / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


# ----- defaults -----------------------------------------------------------------------

def test_defaults_with_nothing_configured():
    s = load()
    assert s.send_mode == "test"
    assert s.daily_send_cap == 20 and isinstance(s.daily_send_cap, int)
    assert s.publik_api_base_url == "https://publikhq.com/api/v1"
    assert s.publik_model == "publik-balanced"
    assert s.publik_timeout_seconds == 30.0 and isinstance(s.publik_timeout_seconds, float)
    assert s.publik_max_retries == 3 and isinstance(s.publik_max_retries, int)
    assert s.openai_base_url == "https://api.openai.com/v1"
    assert not s.publik_api_key and not s.openai_api_key and not s.anthropic_api_key
    assert not s.test_recipient_email
    assert s.sender_email == ""


def test_default_paths_are_path_objects_under_home():
    s = load()
    assert isinstance(s.workdir, Path) and isinstance(s.gmail_token_path, Path)
    assert isinstance(s.gmail_client_secrets_path, Path)
    assert s.workdir == Path(".")
    home = Path(os.environ["HOME"])
    token = Path(os.path.expanduser(str(s.gmail_token_path)))
    secrets = Path(os.path.expanduser(str(s.gmail_client_secrets_path)))
    assert token == home / ".config" / "vcoutreach" / "token.json"
    assert secrets == home / ".config" / "vcoutreach" / "client_secret.json"


def test_settings_is_a_frozen_dataclass():
    s = load()
    assert dataclasses.is_dataclass(s)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.send_mode = "live"  # type: ignore[misc]


def test_every_documented_field_exists():
    names = {f.name for f in dataclasses.fields(Settings)}
    documented = {
        "send_mode", "test_recipient_email", "daily_send_cap", "publik_api_key",
        "publik_api_base_url", "publik_model", "publik_timeout_seconds", "publik_max_retries",
        "openai_api_key", "openai_base_url", "openai_model", "anthropic_api_key",
        "anthropic_model", "workdir", "gmail_token_path", "gmail_client_secrets_path",
        "sender_email",
    }
    assert documented <= names


# ----- environment mapping -----------------------------------------------------------------

def test_values_are_read_with_the_documented_types():
    s = load({
        "SEND_MODE": "live", "TEST_RECIPIENT_EMAIL": "me@mail.test", "DAILY_SEND_CAP": "7",
        "SENDER_EMAIL": "sam@example.com", "PUBLIK_API_KEY": "k1",
        "PUBLIK_API_BASE_URL": "http://gw.test/api/v1", "PUBLIK_MODEL": "publik-smart",
        "PUBLIK_TIMEOUT_SECONDS": "12.5", "PUBLIK_MAX_RETRIES": "5",
        "OPENAI_API_KEY": "k2", "OPENAI_BASE_URL": "http://llm.test/v1", "OPENAI_MODEL": "gpt-x",
        "ANTHROPIC_API_KEY": "k3", "ANTHROPIC_MODEL": "claude-x",
        "WORKDIR": "/tmp/work-area", "GMAIL_TOKEN_PATH": "/tmp/t.json",
        "GMAIL_CLIENT_SECRETS_PATH": "/tmp/c.json",
    })
    assert s.send_mode == "live" and s.test_recipient_email == "me@mail.test"
    assert s.daily_send_cap == 7 and s.sender_email == "sam@example.com"
    assert s.publik_api_key == "k1" and s.publik_api_base_url == "http://gw.test/api/v1"
    assert s.publik_model == "publik-smart"
    assert s.publik_timeout_seconds == 12.5 and s.publik_max_retries == 5
    assert (s.openai_api_key, s.openai_base_url, s.openai_model) == ("k2", "http://llm.test/v1", "gpt-x")
    assert (s.anthropic_api_key, s.anthropic_model) == ("k3", "claude-x")
    assert s.workdir == Path("/tmp/work-area")
    assert s.gmail_token_path == Path("/tmp/t.json")
    assert s.gmail_client_secrets_path == Path("/tmp/c.json")


def test_integer_timeout_is_still_a_float():
    s = load({"PUBLIK_TIMEOUT_SECONDS": "10"})
    assert s.publik_timeout_seconds == 10.0 and isinstance(s.publik_timeout_seconds, float)


def test_env_none_reads_the_real_process_environment(monkeypatch):
    monkeypatch.setenv("PUBLIK_API_KEY", "from-os-environ")
    s = load_settings(dotenv_path=NO_DOTENV)
    assert s.publik_api_key == "from-os-environ"


def test_empty_mapping_does_not_fall_back_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("PUBLIK_API_KEY", "from-os-environ")
    s = load_settings(env={}, dotenv_path=NO_DOTENV)
    assert not s.publik_api_key


@pytest.mark.parametrize("value", ["test", "live"])
def test_valid_send_modes_are_accepted(value):
    assert load({"SEND_MODE": value}).send_mode == value


@pytest.mark.parametrize("value", ["production", "true", "1", "yes", "dry-run", "prod"])
def test_invalid_send_mode_raises_outreach_error(value):
    with pytest.raises(OutreachError):
        load({"SEND_MODE": value})


@pytest.mark.parametrize("name,value", [
    ("DAILY_SEND_CAP", "many"), ("PUBLIK_MAX_RETRIES", "x"), ("PUBLIK_TIMEOUT_SECONDS", "soon"),
])
def test_malformed_numbers_fail_with_a_clean_error(name, value):
    with pytest.raises((OutreachError, ValueError)):
        load({name: value})


def test_blank_values_mean_unset_so_defaults_apply():
    s = load({"PUBLIK_MODEL": "", "PUBLIK_API_BASE_URL": "", "SEND_MODE": "", "DAILY_SEND_CAP": ""})
    assert s.publik_model == "publik-balanced"
    assert s.publik_api_base_url == "https://publikhq.com/api/v1"
    assert s.send_mode == "test"
    assert s.daily_send_cap == 20


# ----- .env handling -----------------------------------------------------------------------------

def test_dotenv_fills_values_the_environment_lacks(kit):
    path = write_env(kit, "PUBLIK_API_KEY=from-dotenv\nSEND_MODE=test\n")
    assert load({}, path).publik_api_key == "from-dotenv"


def test_real_environment_wins_over_dotenv(kit):
    path = write_env(kit, "PUBLIK_API_KEY=from-dotenv\nSEND_MODE=test\nDAILY_SEND_CAP=3\n")
    s = load({"PUBLIK_API_KEY": "from-env", "SEND_MODE": "live"}, path)
    assert s.publik_api_key == "from-env"
    assert s.send_mode == "live"
    assert s.daily_send_cap == 3  # untouched by env, still read from .env


@pytest.mark.parametrize(
    "line,expected",
    [
        ("PUBLIK_API_KEY=abc123", "abc123"),
        ('PUBLIK_API_KEY="abc 123"', "abc 123"),
        ("PUBLIK_API_KEY='abc 123'", "abc 123"),
        ("PUBLIK_API_KEY=abc123   ", "abc123"),
        ("PUBLIK_API_KEY=", ""),
        ('PUBLIK_API_KEY=""', ""),
        ('PUBLIK_API_KEY="has # hash"', "has # hash"),
        ("PUBLIK_API_KEY=a=b=c", "a=b=c"),
    ],
)
def test_dotenv_value_forms(kit, line, expected):
    path = write_env(kit, line + "\n")
    assert load({}, path).publik_api_key == expected


def test_dotenv_comments_and_blank_lines_are_ignored(kit):
    path = write_env(kit, "# a comment\n\n   \n# PUBLIK_API_KEY=commented-out\nOPENAI_API_KEY=real\n")
    s = load({}, path)
    assert not s.publik_api_key
    assert s.openai_api_key == "real"


def test_dotenv_with_windows_line_endings(kit):
    path = write_env(kit, "PUBLIK_API_KEY=abc123\r\nSEND_MODE=test\r\nTEST_RECIPIENT_EMAIL=me@mail.test\r\n")
    s = load({}, path)
    assert s.publik_api_key == "abc123"
    assert s.test_recipient_email == "me@mail.test"
    assert "\r" not in s.send_mode


def test_dotenv_with_utf8_bom_still_reads_the_first_line(kit):
    path = write_env(kit, b"\xef\xbb\xbfPUBLIK_API_KEY=abc123\n")
    assert load({}, path).publik_api_key == "abc123"


def test_dotenv_url_value_keeps_its_query_string(kit):
    path = write_env(kit, "PUBLIK_API_BASE_URL=https://gw.example.com/api/v1?region=eu&x=1\n")
    assert load({}, path).publik_api_base_url == "https://gw.example.com/api/v1?region=eu&x=1"


def test_missing_dotenv_file_is_not_an_error():
    assert load({}, Path("/definitely/not/here/.env")).send_mode == "test"


def test_dotenv_in_the_working_directory_is_found_by_default(kit):
    write_env(kit, "PUBLIK_API_KEY=cwd-dotenv\n")
    s = load_settings(env={})
    assert s.publik_api_key == "cwd-dotenv"


# ----- `export KEY=VALUE` and the warn callback (SPEC 19.7) -------------------------------------------

@pytest.mark.parametrize(
    "line,expected",
    [
        ("export PUBLIK_API_KEY=abc123", "abc123"),
        ('export PUBLIK_API_KEY="abc 123"', "abc 123"),
        ("export PUBLIK_API_KEY='abc 123'", "abc 123"),
        ("export  PUBLIK_API_KEY=abc123", "abc123"),
        ("export PUBLIK_API_KEY=a=b=c", "a=b=c"),
        ("export PUBLIK_API_KEY=", ""),
    ],
)
def test_a_leading_export_is_accepted(kit, line, expected):
    warnings = []
    path = write_env(kit, line + "\n")
    s = load_settings(env={}, dotenv_path=path, warn=warnings.append)
    assert s.publik_api_key == expected
    assert warnings == [], "an export line is a valid line"


def test_export_lines_mix_with_plain_lines_and_the_environment_still_wins(kit):
    path = write_env(kit, "export PUBLIK_API_KEY=from-dotenv\nOPENAI_API_KEY=plain\nexport SEND_MODE=test\n")
    s = load_settings(env={"PUBLIK_API_KEY": "from-env"}, dotenv_path=path)
    assert s.publik_api_key == "from-env"
    assert s.openai_api_key == "plain"
    assert s.send_mode == "test"


def test_warn_is_called_once_per_malformed_line_with_its_line_number(kit):
    path = write_env(kit, "# comment\n\nPUBLIK_API_KEY=good\nthis is not valid\n   \nplainwords\nOPENAI_API_KEY=also-good\n")
    warnings = []
    s = load_settings(env={}, dotenv_path=path, warn=warnings.append)
    assert warnings == [".env line 4 ignored: not KEY=VALUE", ".env line 6 ignored: not KEY=VALUE"]
    assert s.publik_api_key == "good" and s.openai_api_key == "also-good", "good lines around a bad one still load"


def test_warn_never_receives_the_value_of_a_malformed_line(kit):
    secret = "zq9-very-secret-value-4711"
    path = write_env(kit, f"PUBLIK_API_KEY {secret}\nsecret {secret} here\n")
    warnings = []
    load_settings(env={}, dotenv_path=path, warn=warnings.append)
    assert len(warnings) == 2
    for message in warnings:
        assert secret not in message and "PUBLIK_API_KEY" not in message and "zq9" not in message
        assert message.endswith("ignored: not KEY=VALUE")


def test_warn_line_numbers_are_physical_lines_even_with_windows_line_endings(kit):
    path = write_env(kit, "PUBLIK_API_KEY=good\r\n\r\nbroken line\r\n")
    warnings = []
    load_settings(env={}, dotenv_path=path, warn=warnings.append)
    assert warnings == [".env line 3 ignored: not KEY=VALUE"]


def test_warn_is_silent_for_comments_blank_lines_and_valid_lines(kit):
    path = write_env(kit, "# a comment\n\n   \nPUBLIK_API_KEY=abc\nOPENAI_API_KEY=\nexport SEND_MODE=test\n")
    warnings = []
    load_settings(env={}, dotenv_path=path, warn=warnings.append)
    assert warnings == []


def test_warn_is_not_called_when_there_is_no_dotenv_file():
    warnings = []
    load_settings(env={}, dotenv_path=Path("/definitely/not/here/.env"), warn=warnings.append)
    assert warnings == []


def test_a_malformed_line_without_a_warn_callback_is_skipped_quietly(kit):
    path = write_env(kit, "garbage line\nPUBLIK_API_KEY=ok\n")
    assert load_settings(env={}, dotenv_path=path).publik_api_key == "ok"
    assert load_settings(env={}, dotenv_path=path, warn=None).publik_api_key == "ok"


def test_warn_also_works_for_the_default_dotenv_in_the_working_directory(kit):
    write_env(kit, "oops not an assignment\nPUBLIK_API_KEY=cwd\n")
    warnings = []
    s = load_settings(env={}, warn=warnings.append)
    assert s.publik_api_key == "cwd"
    assert warnings == [".env line 1 ignored: not KEY=VALUE"]


def test_invalid_send_mode_in_dotenv_also_raises(kit):
    path = write_env(kit, "SEND_MODE=production\n")
    with pytest.raises(OutreachError):
        load({}, path)


def test_shipped_env_example_loads_unchanged_with_safe_defaults():
    ref = resources.files("vcoutreach").joinpath("data", "examples", "env.example")
    with resources.as_file(ref) as p:
        s = load({}, Path(p))
    assert s.send_mode == "test"
    assert not s.publik_api_key and not s.openai_api_key and not s.anthropic_api_key
    assert s.daily_send_cap >= 1
    assert s.publik_model in {"publik-fast", "publik-balanced", "publik-smart", "publik-default"}


# ----- masking ---------------------------------------------------------------------------------------

def secret_settings():
    return load(dict(KEYS, SEND_MODE="test", TEST_RECIPIENT_EMAIL="me@mail.test"))


def test_repr_and_str_never_contain_any_key():
    s = secret_settings()
    for rendered in (repr(s), str(s), f"{s}", f"{s!r}", "%s" % s, repr([s]), format(s)):
        for key in KEYS.values():
            assert key not in rendered


def test_repr_masks_keys_as_three_stars_without_leaking_the_tail():
    rendered = repr(secret_settings())
    assert "***" in rendered
    for key in KEYS.values():
        assert key[-4:] not in rendered, "showing the last 4 characters of a key is not allowed"
        assert key[:8] not in rendered


# ----- SPEC 21.6: no default model for the bring-your-own providers -------------------------------------------------------

def test_the_openai_and_anthropic_models_have_no_default():
    s = load()
    assert not s.openai_model and not s.anthropic_model


def test_setting_a_key_does_not_invent_a_model():
    s = load({"OPENAI_API_KEY": "k2", "ANTHROPIC_API_KEY": "k3"})
    assert not s.openai_model and not s.anthropic_model


def test_a_blank_model_line_in_the_environment_or_dotenv_stays_empty(kit):
    assert not load({"OPENAI_MODEL": "", "ANTHROPIC_MODEL": ""}).openai_model
    path = write_env(kit, "OPENAI_API_KEY=k2\nOPENAI_MODEL=\nANTHROPIC_API_KEY=k3\nANTHROPIC_MODEL=\n")
    s = load({}, dotenv=path)
    assert not s.openai_model and not s.anthropic_model


def test_a_model_given_in_dotenv_is_read(kit):
    path = write_env(kit, "OPENAI_MODEL=my-chat-model\nANTHROPIC_MODEL=my-claude-model\n")
    s = load({}, dotenv=path)
    assert (s.openai_model, s.anthropic_model) == ("my-chat-model", "my-claude-model")


def test_the_publik_model_keeps_its_tier_default():
    assert load().publik_model == "publik-balanced"


def test_no_module_keeps_a_stale_default_model_constant():
    import importlib
    import re

    pattern = re.compile(r"(?i)^(gpt-|claude-|o[0-9]-)")
    offenders = []
    for name in ("vcoutreach.config", "vcoutreach.providers", "vcoutreach.providers.openai_compat",
                 "vcoutreach.providers.anthropic_provider"):
        module = importlib.import_module(name)
        for attr, value in vars(module).items():
            if isinstance(value, str) and pattern.match(value):
                offenders.append(f"{name}.{attr} = {value!r}")
    assert offenders == [], offenders
