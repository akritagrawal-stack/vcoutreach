"""SPEC 22.17: the sender dash check (20.4) descends into nested values and names the dotted path."""
from __future__ import annotations

import json
import re

import pytest

from vcoutreach.sender import load_sender
from vcoutreach.types import SenderProfileError

EM = chr(0x2014)
EN = chr(0x2013)
TAIL = r" contains an em dash or en dash"


def sender_file(kit, **fields):
    data = {"name": "Sam Rivera"}
    data.update(fields)
    return kit.write_raw("nested-sender.json", json.dumps(data))  # json.dumps writes the dash as a \u escape


def refused(kit, **fields):
    path = sender_file(kit, **fields)
    with pytest.raises(SenderProfileError) as info:
        load_sender(path)
    message = str(info.value)
    assert path.name in message
    return message


# ----- a nested object value --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("dash", [EM, EN], ids=["em-dash", "en-dash"])
def test_a_dash_in_an_approved_claims_value_names_the_dotted_path(kit, dash):
    message = refused(kit, approved_claims={"metric": f"grew twofold {dash} fast"})
    assert re.search(r"field 'approved_claims\.metric'" + TAIL, message), message


def test_only_the_offending_claim_is_named_when_others_are_clean(kit):
    message = refused(kit, approved_claims={"partners": "a handful of design partners", "metric": f"grew {EM} fast"})
    assert "approved_claims.metric" in message and "approved_claims.partners" not in message


@pytest.mark.parametrize("dash", [EM, EN], ids=["em-dash", "en-dash"])
def test_a_dash_two_levels_down_names_the_whole_path(kit, dash):
    message = refused(kit, extras={"a": {"b": {"c": f"x {dash} y"}}})
    assert re.search(r"field 'extras\.a\.b\.c'" + TAIL, message), message


def test_a_dash_in_a_list_inside_a_nested_object_is_found(kit):
    message = refused(kit, extras={"notes": ["a clean line", f"a dirty {EM} line"]})
    assert "extras.notes" in message and TAIL.strip() in message


def test_a_dash_in_an_object_inside_a_list_is_found(kit):
    message = refused(kit, extras={"people": [{"name": "Ada"}, {"name": f"Bob {EN} Smith"}]})
    assert "extras" in message and TAIL.strip() in message


# ----- an unknown top level key is still a string anywhere in the file --------------------------------------------------------------

def test_a_dash_in_an_unknown_top_level_key_is_found_too(kit):
    message = refused(kit, favourite_colour=f"teal {EM} green")
    assert re.search(r"field 'favourite_colour'" + TAIL, message), message


# ----- the existing top level behaviour is unchanged --------------------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", [("ask_line", f"Call me {EM} any time"), ("title", f"Founder {EN} CEO"),
                                         ("product_lines", [f"We build things {EM} well"])],
                         ids=["ask_line", "title", "product_lines"])
def test_top_level_fields_are_still_named_without_a_dot(kit, field, value):
    message = refused(kit, **{field: value})
    assert re.search(rf"field '{field}'" + TAIL, message), message


# ----- clean nested values still load -------------------------------------------------------------------------------------------------------

def test_clean_nested_values_load_and_keep_their_hyphens(kit):
    path = sender_file(kit, approved_claims={"partners": "a well-known handful of design-partners"},
                       extras={"n": 5, "ok": True, "nothing": None, "list": [1, 2, 3], "deep": {"x": "fine - really"}})
    sender = load_sender(path)
    assert dict(sender.approved_claims) == {"partners": "a well-known handful of design-partners"}


def test_several_nested_dashes_report_at_least_one_of_them(kit):
    message = refused(kit, approved_claims={"a": f"x {EM} y", "b": f"x {EN} y"})
    assert "approved_claims.a" in message or "approved_claims.b" in message
