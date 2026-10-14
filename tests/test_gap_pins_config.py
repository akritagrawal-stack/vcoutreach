"""SPEC section 22.7: inline comments in a .env file."""
from __future__ import annotations

import pytest

from vcoutreach.config import load_settings


def load(kit, text):
    path = kit.write_raw(".env", text)
    return load_settings(env={}, dotenv_path=path)


# ----- unquoted values: a '#' after whitespace starts a comment, a '#' glued to the value does not ----------------------------

@pytest.mark.parametrize("line,expected", [
    ("PUBLIK_API_KEY=abc123 # my key", "abc123"),
    ("PUBLIK_API_KEY=abc123    # my key", "abc123"),
    ("PUBLIK_API_KEY=abc123\t# my key", "abc123"),
    ("PUBLIK_API_KEY=abc123 #no space after the hash", "abc123"),
    ("PUBLIK_API_KEY=abc123 ## double", "abc123"),
    ("PUBLIK_API_KEY=abc123   ", "abc123"),
])
def test_an_unquoted_value_stops_at_whitespace_then_hash_and_is_trimmed(kit, line, expected):
    assert load(kit, line + "\n").publik_api_key == expected


@pytest.mark.parametrize("line,expected", [
    ("PUBLIK_API_BASE_URL=https://gw.test/api#frag", "https://gw.test/api#frag"),
    ("PUBLIK_API_BASE_URL=https://gw.test/api#frag # real comment", "https://gw.test/api#frag"),
    ("OPENAI_MODEL=m#1", "m#1"),
    ("OPENAI_MODEL=m#1 # real comment", "m#1"),
    ("OPENAI_MODEL=#leading-hash", "#leading-hash"),
])
def test_a_hash_that_is_not_preceded_by_whitespace_is_part_of_the_value(kit, line, expected):
    s = load(kit, line + "\n")
    got = s.publik_api_base_url if line.startswith("PUBLIK_API_BASE_URL") else s.openai_model
    assert got == expected


def test_a_value_that_is_only_a_comment_is_empty(kit):
    s = load(kit, "PUBLIK_API_KEY= # nothing here\nOPENAI_MODEL=m1\n")
    assert not s.publik_api_key
    assert s.openai_model == "m1", "the next line is untouched"


# ----- quoted values: whitespace then '#' after the closing quote starts a comment; inside the quotes it is text ------------------

@pytest.mark.parametrize("line,expected", [
    ('PUBLIK_API_KEY="a b"  # comment', "a b"),
    ('PUBLIK_API_KEY="a b" # comment', "a b"),
    ("PUBLIK_API_KEY='a b' # comment", "a b"),
    ('PUBLIK_API_KEY="a b"\t# comment', "a b"),
    ('PUBLIK_API_KEY="a # b"', "a # b"),
    ('PUBLIK_API_KEY="a # b" # comment', "a # b"),
    ("PUBLIK_API_KEY='a # b'", "a # b"),
    ('PUBLIK_API_KEY="abc"', "abc"),
])
def test_a_quoted_value_ends_at_the_closing_quote_and_a_comment_may_follow(kit, line, expected):
    assert load(kit, line + "\n").publik_api_key == expected


def test_comment_lines_and_inline_comments_can_share_a_file(kit):
    text = (
        "# the key\n"
        "PUBLIK_API_KEY=abc123 # inline\n"
        "\n"
        "   # indented comment\n"
        'OPENAI_MODEL="my model" # quoted\n'
        "PUBLIK_API_BASE_URL=https://gw.test/x#y\n"
    )
    s = load(kit, text)
    assert (s.publik_api_key, s.openai_model, s.publik_api_base_url) == ("abc123", "my model", "https://gw.test/x#y")
