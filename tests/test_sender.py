"""sender.load_sender (SPEC section 5 and section 17 sample data contract)."""
from __future__ import annotations

import importlib.resources as resources
import json
import re
from pathlib import Path

import pytest

from vcoutreach.sender import load_sender
from vcoutreach.types import SenderProfile, SenderProfileError


def write(kit, data, name="sender.json"):
    text = data if isinstance(data, str) else json.dumps(data)
    return kit.write_raw(name, text)


def test_full_profile_maps_every_key_and_arrays_become_tuples(kit):
    path = kit.write_sender()
    s = load_sender(path)
    assert isinstance(s, SenderProfile)
    assert s.name == "Sam Rivera"
    assert s.title == "Founder"
    assert s.org == "Lumenlist"
    assert s.website == "https://lumenlist.example.com"
    assert s.signature == "Sam Rivera\nFounder, Lumenlist\nhttps://lumenlist.example.com"
    assert isinstance(s.product_lines, tuple) and len(s.product_lines) == 1
    assert isinstance(s.traction_lines, tuple) and len(s.traction_lines) == 1
    assert s.ask_line.startswith("If a short call")
    assert dict(s.approved_claims) == {"design_partners": "a handful of design partners"}
    assert s.forbidden_phrases == ("synergy", "game changer")


def test_name_only_profile_gets_empty_defaults(kit):
    s = load_sender(write(kit, {"name": "Sam"}))
    assert s.name == "Sam"
    assert s.title == "" and s.org == "" and s.signature == "" and s.ask_line == ""
    assert s.product_lines == () and s.traction_lines == () and s.forbidden_phrases == ()
    assert dict(s.approved_claims) == {}


@pytest.mark.parametrize("payload", [{}, {"title": "Founder"}, {"name": ""}, {"name": "   "}, {"name": None}])
def test_missing_or_blank_name_raises_sender_profile_error(kit, payload):
    with pytest.raises(SenderProfileError):
        load_sender(write(kit, payload))


@pytest.mark.parametrize(
    "text",
    ['{"name": "Sam",}', '{"name": "Sam"', "", "not json at all", "{'name': 'Sam'}"],
)
def test_malformed_json_raises_sender_profile_error(kit, text):
    with pytest.raises(SenderProfileError):
        load_sender(write(kit, text))


@pytest.mark.parametrize("text", ["[]", '"Sam"', "42", "null"])
def test_top_level_must_be_an_object(kit, text):
    with pytest.raises(SenderProfileError):
        load_sender(write(kit, text))


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Sam", "product_lines": "one long string instead of a list"},
        {"name": "Sam", "traction_lines": 5},
        {"name": "Sam", "forbidden_phrases": "synergy"},
        {"name": "Sam", "approved_claims": ["a", "b"]},
        {"name": "Sam", "product_lines": [1, 2]},
    ],
)
def test_wrong_value_types_raise_sender_profile_error(kit, payload):
    with pytest.raises(SenderProfileError):
        load_sender(write(kit, payload))


def test_unknown_keys_are_ignored(kit):
    s = load_sender(write(kit, {"name": "Sam", "_comment": "hello", "favourite_colour": "teal"}))
    assert s.name == "Sam"


def test_optional_subject_template_does_not_break_loading(kit):
    s = load_sender(write(kit, {"name": "Sam", "subject_template": "hello {first_name} at {org}"}))
    assert s.name == "Sam"


def test_utf8_bom_file_loads(kit):
    path = kit.workdir / "bom.json"
    path.write_bytes(("\ufeff" + json.dumps({"name": "Zo\u00eb Sender"})).encode("utf-8"))
    assert load_sender(path).name == "Zo\u00eb Sender"


def test_unicode_and_multiline_signature_survive(kit):
    sig = "Zo\u00eb Sender\nFounder, Caf\u00e9 Co\nhttps://cafe.example.com"
    s = load_sender(write(kit, {"name": "Zo\u00eb Sender", "signature": sig}))
    assert s.signature == sig


def test_missing_file_is_a_clean_error(kit):
    with pytest.raises((SenderProfileError, FileNotFoundError)):
        load_sender(kit.workdir / "absent.json")


# ----- no em dash or en dash in any string value (SPEC 20.4) ----------------------------------------------

EM = chr(0x2014)
EN = chr(0x2013)
DASH_MESSAGE = r"sender file .+: field '{field}' contains an em dash or en dash"


def dashed(kit, **fields):
    data = {"name": "Sam Rivera"}
    data.update(fields)
    return kit.write_raw("dashed-sender.json", json.dumps(data, ensure_ascii=False))


@pytest.mark.parametrize("dash", [EM, EN], ids=["em-dash", "en-dash"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "Sam {d} Rivera"),
        ("title", "Founder {d} CEO"),
        ("org", "Lumen {d} list"),
        ("website", "https://lumenlist.example.com/a{d}b"),
        ("signature", "Sam Rivera\nFounder {d} Lumenlist"),
        ("ask_line", "If a short call makes sense {d} here is my link."),
        ("subject_template", "an idea {d} for {{first_name}}"),
        ("product_lines", ["We build a tool {d} for founders."]),
        ("traction_lines", ["Last month {d} we onboarded a handful of design partners."]),
    ],
    ids=["name", "title", "org", "website", "signature", "ask_line", "subject_template", "product_lines", "traction_lines"],
)
def test_a_dash_in_a_sender_value_is_rejected_naming_the_field(kit, field, value, dash):
    if isinstance(value, list):
        value = [v.format(d=dash) for v in value]
    else:
        value = value.format(d=dash)
    path = dashed(kit, **{field: value})
    with pytest.raises(SenderProfileError) as info:
        load_sender(path)
    message = str(info.value)
    assert re.fullmatch(DASH_MESSAGE.format(field=field), message), message
    assert path.name in message


def test_a_dash_in_the_second_list_item_is_found_too(kit):
    path = dashed(kit, product_lines=["A clean first line.", "A second line " + EM + " with a dash."])
    with pytest.raises(SenderProfileError) as info:
        load_sender(path)
    assert "field 'product_lines'" in str(info.value)


def test_a_dash_written_as_a_json_escape_is_caught_as_well(kit):
    path = kit.write_raw("escaped.json", '{"name": "Sam", "ask_line": "Call me \\u2014 any time"}')
    with pytest.raises(SenderProfileError) as info:
        load_sender(path)
    assert "field 'ask_line'" in str(info.value)


@pytest.mark.parametrize("field,value", [("forbidden_phrases", ["game " + EM + " changer"]),
                                         ("approved_claims", {"partners": "a handful " + EN + " maybe"})],
                         ids=["forbidden_phrases", "approved_claims"])
def test_dashes_in_the_phrase_and_claim_fields_are_either_rejected_by_name_or_loaded(kit, field, value):
    path = dashed(kit, **{field: value})
    try:
        load_sender(path)
    except SenderProfileError as exc:
        # SPEC 22.17: a value inside an object is named by its dotted path, for example approved_claims.partners
        assert re.search(rf"field '{field}(\.[\w-]+)?'", str(exc)), str(exc)


def test_two_bad_fields_report_at_least_one_of_them(kit):
    path = dashed(kit, title="Founder " + EM + " CEO", ask_line="Call " + EN + " me")
    with pytest.raises(SenderProfileError) as info:
        load_sender(path)
    message = str(info.value)
    assert "field 'title'" in message or "field 'ask_line'" in message


def test_ordinary_hyphens_accented_letters_and_smart_quotes_are_fine(kit):
    path = dashed(kit, title="Co-founder", org="Caf" + chr(0xE9) + " State-of-the-Art Ltd",
                  ask_line="Let" + chr(0x2019) + "s talk - any time.", product_lines=["A tool for follow-ups, not mass mail."])
    s = load_sender(path)
    assert s.title == "Co-founder" and s.product_lines == ("A tool for follow-ups, not mass mail.",)


def test_numbers_and_non_string_values_are_not_inspected_for_dashes(kit):
    path = kit.write_raw("odd-types.json", '{"name": "Sam", "unknown_number": 5, "unknown_flag": true}')
    assert load_sender(path).name == "Sam"


# ----- shipped sample (SPEC section 17) -----------------------------------------------------

def sample_sender():
    ref = resources.files("vcoutreach").joinpath("data", "examples", "sender.sample.json")
    with resources.as_file(ref) as p:
        return load_sender(Path(p))


def test_sample_sender_is_sam_rivera_of_lumenlist():
    s = sample_sender()
    assert (s.name, s.title, s.org) == ("Sam Rivera", "Founder", "Lumenlist")


def test_sample_sender_has_the_documented_lines():
    s = sample_sender()
    assert len(s.product_lines) == 1 and s.product_lines[0].strip()
    assert len(s.traction_lines) == 1 and s.traction_lines[0].strip()
    assert s.ask_line.strip()
    assert s.signature.strip()
    assert len(s.forbidden_phrases) == 2 and all(p.strip() for p in s.forbidden_phrases)


def test_sample_traction_number_is_backed_by_an_approved_claim():
    s = sample_sender()
    numbers = re.findall(r"\d+", s.traction_lines[0])
    assert numbers, "the sample traction line must contain a number"
    claims = " ".join(s.approved_claims.values())
    for n in numbers:
        assert n in claims, f"{n} appears in the traction line but not in approved_claims"


def test_sample_sender_text_has_no_unicode_dashes():
    s = sample_sender()
    blob = json.dumps(
        [s.name, s.title, s.org, s.signature, s.product_lines, s.traction_lines, s.ask_line,
         dict(s.approved_claims), s.forbidden_phrases],
        ensure_ascii=False,
    )
    assert "\u2014" not in blob and "\u2013" not in blob
