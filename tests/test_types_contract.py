"""The frozen contract in vcoutreach/types.py (SPEC section 4)."""
from __future__ import annotations

import dataclasses
import json

import pytest

import vcoutreach
from vcoutreach.types import (
    Contact,
    ContactsError,
    Draft,
    Hook,
    InsufficientFacts,
    Lane,
    LintIssue,
    OutreachError,
    PersonalizationProvider,
    PersonalizationRequest,
    ProviderAuthError,
    ProviderConfigError,
    ProviderCreditError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTransportError,
    SenderProfile,
    SenderProfileError,
)


def field_names(cls):
    return [f.name for f in dataclasses.fields(cls)]


def field_defaults(cls):
    out = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            out[f.name] = f.default_factory()  # type: ignore[misc]
    return out


def test_package_version_is_0_1_0():
    assert vcoutreach.__version__ == "0.1.0"


# ----- Lane -------------------------------------------------------------------

def test_lane_has_exactly_the_two_documented_members():
    assert {m.name: m.value for m in Lane} == {
        "FIRST_TOUCH": "first_touch",
        "FOLLOW_UP": "follow_up",
    }


def test_lane_is_a_str_enum_that_round_trips_by_value():
    assert isinstance(Lane.FIRST_TOUCH, str)
    assert Lane.FIRST_TOUCH == "first_touch"
    assert Lane("follow_up") is Lane.FOLLOW_UP


def test_lane_serialises_to_plain_json_string():
    assert json.dumps({"lane": Lane.FOLLOW_UP}) == '{"lane": "follow_up"}'


# ----- Contact ----------------------------------------------------------------

def test_contact_field_names_and_order_match_the_spec():
    assert field_names(Contact) == [
        "id", "name", "email", "org", "role", "facts", "source_urls",
        "lane", "thread_subject", "in_reply_to", "references", "preferred_name",
    ]


def test_contact_defaults_when_only_required_fields_given():
    c = Contact(id="a", name="Ada", email="ada@example.com")
    assert c.org == "" and c.role == ""
    assert c.facts == () and c.source_urls == ()
    assert c.lane is Lane.FIRST_TOUCH
    assert c.thread_subject == "" and c.in_reply_to == "" and c.references == ""
    assert c.preferred_name == ""


def test_contact_is_frozen():
    c = Contact(id="a", name="Ada", email="ada@example.com")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.name = "Someone Else"  # type: ignore[misc]


def test_contact_with_tuple_fields_is_hashable_and_equal_by_value():
    a = Contact(id="a", name="Ada", email="ada@example.com", facts=("x",))
    b = Contact(id="a", name="Ada", email="ada@example.com", facts=("x",))
    assert a == b
    assert len({a, b}) == 1


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Ada Lovelace", "Ada"),
        ("  Ada   Lovelace  ", "Ada"),
        ("Madonna", "Madonna"),
        ("", ""),
        ("   ", ""),
        ("\tGrace\nHopper", "Grace"),
        ("Zo\u00eb \u00c5kesson", "Zo\u00eb"),
        ("Dr. Jane Roe", "Jane"),
        ("Mary-Jane Watson", "Mary-Jane"),
        ("\u674e\u96f7", "\u674e\u96f7"),
        ("Jean Luc Picard", "Jean"),
    ],
)
def test_first_name_edge_cases(name, expected):
    assert Contact(id="x", name=name, email="x@example.com").first_name == expected


def test_first_name_is_a_property_not_a_dataclass_field():
    assert isinstance(Contact.first_name, property)
    assert "first_name" not in field_names(Contact)


def test_preferred_name_is_the_last_field_and_defaults_to_empty():
    assert field_names(Contact)[-1] == "preferred_name"
    assert field_defaults(Contact)["preferred_name"] == ""
    assert Contact("a", "Ada", "ada@example.com").preferred_name == ""


def test_the_older_positional_fields_keep_their_positions():
    c = Contact("a", "Ada", "ada@example.com", "Org", "Role", ("f",), ("u",), Lane.FOLLOW_UP, "subj", "<m@x>", "<r@x>")
    assert (c.org, c.role, c.facts, c.source_urls, c.lane) == ("Org", "Role", ("f",), ("u",), Lane.FOLLOW_UP)
    assert (c.thread_subject, c.in_reply_to, c.references, c.preferred_name) == ("subj", "<m@x>", "<r@x>", "")


def test_preferred_name_takes_part_in_equality_and_hashing():
    a = Contact(id="a", name="Ada", email="ada@example.com", preferred_name="Ad")
    b = Contact(id="a", name="Ada", email="ada@example.com", preferred_name="Ad")
    c = Contact(id="a", name="Ada", email="ada@example.com")
    assert a == b and a != c
    assert len({a, b, c}) == 2


E_ACUTE = chr(0xE9)
E_ACUTE_UP = chr(0xC9)


@pytest.mark.parametrize(
    "name,expected",
    [
        # leading titles, with or without the period, any case
        ("Dr. Jane Roe", "Jane"), ("Dr Jane Roe", "Jane"), ("DR. JANE ROE", "Jane"), ("dr. jane roe", "Jane"),
        ("Mr. John Smith", "John"), ("Mr John Smith", "John"), ("MR JOHN SMITH", "John"),
        ("Mrs. Alice Brown", "Alice"), ("Ms Alice Brown", "Alice"), ("Mx. Alex Kim", "Alex"),
        ("Prof. Alan Turing", "Alan"), ("Prof Alan Turing", "Alan"), ("Professor Alan Turing", "Alan"),
        ("professor alan turing", "Alan"), ("Sir Isaac Newton", "Isaac"), ("Madam Marie Curie", "Marie"),
        # words that merely start like a title are names
        ("Drake Smith", "Drake"), ("Missy Elliott", "Missy"), ("Sirius Black", "Sirius"), ("Mister Rogers", "Mister"),
        ("Jane Dr", "Jane"),
        # "Last, First"
        ("Smith, Alice", "Alice"), ("Smith,Alice", "Alice"), ("SMITH, ALICE", "Alice"), ("Smith, Alice B.", "Alice"),
        ("Roe, Dr. Jane", "Jane"), ("Smith,", "Smith"), ("Hopper, Grace Murray", "Grace"),
        # case cleaning only for all upper or all lower tokens
        ("ALICE Smith", "Alice"), ("alice smith", "Alice"), ("ADA", "Ada"), ("grace", "Grace"),
        ("mARY Smith", "mARY"), ("McDonald Ronald", "McDonald"), ("iPhone Guy", "iPhone"),
        (E_ACUTE_UP + "MILE Zola", E_ACUTE_UP + "mile"), (E_ACUTE + "mile zola", E_ACUTE_UP + "mile"),
        ("d'Artagnan Dumas", "d'Artagnan"), ("O'Brien Pat", "O'Brien"), ("Mary-Jane Watson", "Mary-Jane"),
        # trailing punctuation
        ("Alice. Smith", "Alice"), ("Alice; Smith", "Alice"), ("Alice: Smith", "Alice"), ("Alice.. Smith", "Alice"),
        ("Alice,", "Alice"), ("J. Smith", "J"),
        # whitespace
        ("  Dr.   Jane   Roe  ", "Jane"), ("\tDr.\nJane Roe", "Jane"),
        # nothing left
        ("", ""), ("   ", ""), ("Dr.", ""), ("Dr. ", ""), ("Mr", ""), ("Prof.", ""), ("Sir", ""),
        ("...", ""), (".,;:", ""), ("Dr. ...", ""),
    ],
)
def test_first_name_is_a_cleaned_token(name, expected):
    assert Contact(id="x", name=name, email="x@example.com").first_name == expected


@pytest.mark.parametrize(
    "name,preferred,expected",
    [
        ("Dr. Alan Turing", "Al", "Al"),
        ("Turing, Alan", "Al", "Al"),
        ("Alan Turing", "  Al  ", "Al"),
        ("Alan Turing", "alice", "alice"),
        ("Alan Turing", "Al.", "Al."),
        ("", "Al", "Al"),
        ("Alan Turing", "   ", "Alan"),
        ("Dr. Alan Turing", "", "Alan"),
    ],
)
def test_preferred_name_wins_after_stripping_and_is_never_re_cased(name, preferred, expected):
    c = Contact(id="x", name=name, email="x@example.com", preferred_name=preferred)
    assert c.first_name == expected


# ----- SenderProfile ----------------------------------------------------------

def test_sender_profile_starts_with_the_documented_fields_in_order():
    # A trailing optional field (for subject_template) would be tolerated, nothing else.
    assert field_names(SenderProfile)[:10] == [
        "name", "title", "org", "website", "signature", "product_lines",
        "traction_lines", "ask_line", "approved_claims", "forbidden_phrases",
    ]


def test_sender_profile_defaults():
    s = SenderProfile(name="Sam")
    assert s.title == "" and s.org == "" and s.website == "" and s.signature == ""
    assert s.product_lines == () and s.traction_lines == ()
    assert s.ask_line == ""
    assert dict(s.approved_claims) == {}
    assert s.forbidden_phrases == ()


def test_sender_profile_default_claims_are_not_shared_between_instances():
    a = SenderProfile(name="A")
    b = SenderProfile(name="B")
    assert a.approved_claims is not b.approved_claims


def test_sender_profile_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        SenderProfile(name="Sam").name = "Other"  # type: ignore[misc]


# ----- PersonalizationRequest, Hook, LintIssue, Draft ---------------------------

def test_personalization_request_fields_and_default_instructions(kit):
    r = PersonalizationRequest(contact=kit.contact(), sender=kit.sender())
    assert field_names(PersonalizationRequest) == ["contact", "sender", "instructions"]
    assert r.instructions == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.instructions = "x"  # type: ignore[misc]


def test_hook_fields_and_defaults():
    assert field_names(Hook) == ["text", "provider", "model", "meta"]
    h = Hook(text="hi", provider="template")
    assert h.model == "" and dict(h.meta) == {}
    assert Hook(text="a", provider="p").meta is not Hook(text="b", provider="p").meta
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.text = "other"  # type: ignore[misc]


def test_lint_issue_default_severity_is_error():
    assert field_names(LintIssue) == ["code", "message", "severity"]
    assert LintIssue(code="x", message="y").severity == "error"


def test_draft_fields_order_and_defaults():
    assert field_names(Draft) == [
        "contact_id", "to", "subject", "body", "status", "hook", "issues",
        "lane", "in_reply_to", "references",
    ]
    d = Draft(contact_id="a", to="a@example.com", subject="s", body="b", status="ok")
    assert d.hook is None
    assert d.issues == ()
    assert d.lane is Lane.FIRST_TOUCH
    assert d.in_reply_to == "" and d.references == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.status = "blocked"  # type: ignore[misc]


# ----- exceptions ---------------------------------------------------------------

@pytest.mark.parametrize(
    "child,parent",
    [
        (ContactsError, OutreachError),
        (SenderProfileError, OutreachError),
        (ProviderError, OutreachError),
        (ProviderConfigError, ProviderError),
        (ProviderAuthError, ProviderError),
        (ProviderCreditError, ProviderError),
        (ProviderRateLimitError, ProviderError),
        (ProviderTransportError, ProviderError),
        (ProviderResponseError, ProviderError),
        (InsufficientFacts, ProviderError),
    ],
)
def test_exception_hierarchy(child, parent):
    assert issubclass(child, parent)


def test_outreach_error_is_an_ordinary_exception():
    assert issubclass(OutreachError, Exception)
    assert not issubclass(OutreachError, (KeyboardInterrupt, SystemExit))


def test_sibling_provider_errors_are_distinct_types():
    assert not issubclass(ProviderAuthError, ProviderCreditError)
    assert not issubclass(ProviderRateLimitError, ProviderTransportError)
    assert not issubclass(InsufficientFacts, ProviderResponseError)
    assert not issubclass(ContactsError, SenderProfileError)


def test_provider_credit_error_carries_top_up_url_and_message():
    err = ProviderCreditError("Out of credit", "https://example.com/top-up")
    assert str(err) == "Out of credit"
    assert err.top_up_url == "https://example.com/top-up"


def test_provider_credit_error_top_up_url_defaults_to_empty_string():
    err = ProviderCreditError("Out of credit")
    assert err.top_up_url == ""


def test_provider_credit_error_accepts_top_up_url_keyword_and_is_catchable_as_provider_error():
    with pytest.raises(ProviderError) as info:
        raise ProviderCreditError("m", top_up_url="https://example.com/u")
    assert info.value.top_up_url == "https://example.com/u"


def test_personalization_provider_protocol_describes_name_and_personalize():
    assert hasattr(PersonalizationProvider, "personalize")
    assert "name" in getattr(PersonalizationProvider, "__annotations__", {})


# ----- SPEC 21.4c: a suffix after the comma is not a first name ---------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("Alice Brown, PhD", "Alice"), ("Alice Brown, phd", "Alice"), ("Alice Brown, PHD", "Alice"),
        ("Alice Brown, Ph.D.", "Alice"), ("Alice Brown, Ph.D", "Alice"), ("ALICE BROWN, PHD", "Alice"),
        ("Alice Brown, MD", "Alice"), ("Alice Brown, M.D.", "Alice"), ("Alice Brown, MBA", "Alice"),
        ("John Smith, Jr.", "John"), ("John Smith, Jr", "John"), ("John Smith, Sr.", "John"),
        ("John Smith, II", "John"), ("John Smith, III", "John"), ("John Smith, IV", "John"),
        ("Ann Lee, CPA", "Ann"), ("Ann Lee, CFA", "Ann"), ("Ann Lee, Esq.", "Ann"), ("Ann Lee, J.D.", "Ann"),
        ("Ann Lee, JD", "Ann"), ("Ann Lee, DDS", "Ann"), ("Ann Lee, D.D.S.", "Ann"),
        ("Dr. Alice Brown, MD", "Alice"), ("Alice Brown,PhD", "Alice"), ("Alice Brown,  PhD", "Alice"),
        ("Alice Brown, PhD, MBA", "Alice"),
        ("Smith, MD", "Smith"), ("Brown, Jr", "Brown"),
        # still "Last, First" when what follows the comma is not a suffix
        ("Smith, Taylor", "Taylor"), ("Smith, Taylor Jr.", "Taylor"), ("Wong, Mia", "Mia"), ("Lee, Ivy", "Ivy"),
        ("Park, Drew", "Drew"), ("Jones, Iva", "Iva"), ("Roe, Dr. Jane", "Jane"), ("Smith, Alice", "Alice"),
    ],
)
def test_the_comma_rule_knows_suffixes_from_first_names(name, expected):
    assert Contact(id="x", name=name, email="x@example.com").first_name == expected


def test_the_known_imperfect_smith_jr_john_case_still_gives_a_usable_greeting():
    first = Contact(id="x", name="Smith, Jr., John", email="x@example.com").first_name
    assert isinstance(first, str) and first.strip() != ""


def test_a_preferred_name_still_wins_over_a_suffix_name():
    c = Contact(id="x", name="Alice Brown, PhD", email="x@example.com", preferred_name="Al")
    assert c.first_name == "Al"
