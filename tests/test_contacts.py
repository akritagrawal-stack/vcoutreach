"""contacts.load_contacts (SPEC section 5), fed with messy real-world files."""
from __future__ import annotations

import importlib.resources as resources
import itertools
import re
from pathlib import Path

import pytest

from vcoutreach.contacts import load_contacts
from vcoutreach.types import ContactsError, Lane


LETTERS = "abcdefghijklmnopqrstuvwxyz"


def row(kit, n, **over):
    """Data row n (0-based). Names, ids and emails use letters only so that error messages
    containing them cannot be confused with row numbers."""
    base = dict(id=f"c{LETTERS[n]}", name=f"Person {LETTERS[n].upper()}",
                email=f"person{LETTERS[n]}@example.com", org="Org")
    base.update(over)
    return kit.csv_row(**base)


def five_rows(kit, overrides=None):
    """Five data rows; ``overrides`` maps a 0-based row index to a dict of column overrides."""
    overrides = overrides or {}
    return [row(kit, i, **overrides.get(i, {})) for i in range(5)]


# ----- happy paths and column handling -----------------------------------------

def test_loads_a_simple_file_in_file_order(kit):
    path = kit.write_csv(five_rows(kit))
    contacts = load_contacts(path)
    assert [c.name for c in contacts] == [f"Person {ch.upper()}" for ch in "abcde"]
    assert all(c.lane is Lane.FIRST_TOUCH for c in contacts)


def test_every_documented_column_is_mapped(kit):
    path = kit.write_csv([kit.csv_row(
        id="ada-1", name="Ada Lovelace", email="ada@example.com", org="Northwind Labs",
        role="Partner", facts="fact one | fact two", source_urls="https://a.example.com | https://b.example.com",
        lane="follow_up", thread_subject="a note for Ada", in_reply_to="<m1@example.com>",
        references="<m0@example.com> <m1@example.com>",
    )])
    (c,) = load_contacts(path)
    assert (c.id, c.name, c.email, c.org, c.role) == (
        "ada-1", "Ada Lovelace", "ada@example.com", "Northwind Labs", "Partner")
    assert c.facts == ("fact one", "fact two")
    assert c.source_urls == ("https://a.example.com", "https://b.example.com")
    assert c.lane is Lane.FOLLOW_UP
    assert c.thread_subject == "a note for Ada"
    assert c.in_reply_to == "<m1@example.com>"
    assert c.references == "<m0@example.com> <m1@example.com>"


def test_column_order_is_free(kit):
    header = list(reversed(kit.CSV_HEADER))
    path = kit.write_csv([kit.csv_row(id="z", name="Zed Zero", email="zed@example.com", org="ZCo")],
                         header=header)
    (c,) = load_contacts(path)
    assert (c.id, c.name, c.email, c.org) == ("z", "Zed Zero", "zed@example.com", "ZCo")


def test_extra_unknown_columns_are_ignored(kit):
    header = kit.CSV_HEADER + ["linkedin", "notes"]
    path = kit.write_csv(
        [dict(kit.csv_row(id="a", name="A B", email="a@example.com"), linkedin="x", notes="call him")],
        header=header,
    )
    (c,) = load_contacts(path)
    assert c.email == "a@example.com"


def test_header_only_file_has_no_contacts_either_as_an_empty_list_or_a_clear_error(kit):
    path = kit.write_csv([])
    try:
        contacts = load_contacts(path)
    except ContactsError as exc:
        assert re.search(r"no contacts found in \S*contacts\.csv", str(exc)), str(exc)
        return
    assert contacts == []


def test_utf8_bom_does_not_corrupt_the_first_column(kit):
    path = kit.write_csv([kit.csv_row(id="bom", name="Zo\u00eb Test", email="zoe@example.com")], bom=True)
    (c,) = load_contacts(path)
    assert c.id == "bom"
    assert c.name == "Zo\u00eb Test"


def test_crlf_line_endings_leave_no_carriage_returns(kit):
    path = kit.write_csv(five_rows(kit), crlf=True)
    contacts = load_contacts(path)
    assert len(contacts) == 5
    for c in contacts:
        for value in (c.id, c.name, c.email, c.org, c.role, c.lane.value, c.thread_subject):
            assert "\r" not in value
        assert all("\r" not in f for f in c.facts)


def test_bom_and_crlf_together_like_an_excel_export(kit):
    path = kit.write_csv(five_rows(kit), bom=True, crlf=True)
    assert [c.id for c in load_contacts(path)] == [f"c{ch}" for ch in "abcde"]


def test_quoted_commas_and_quotes_survive(kit):
    path = kit.write_csv([kit.csv_row(
        id="q", name="Smith, Jane", email="jane@example.com", org="Acme, Inc.",
        facts='Said "ship early, ship often" | Runs a fund, small',
    )])
    (c,) = load_contacts(path)
    assert c.name == "Smith, Jane"
    assert c.org == "Acme, Inc."
    assert c.facts == ('Said "ship early, ship often"', "Runs a fund, small")


def test_blank_lines_and_comma_only_rows_are_skipped(kit):
    good = kit.csv_text([kit.csv_row(id="a", name="A One", email="a@example.com")]).splitlines()
    more = kit.csv_text([kit.csv_row(id="b", name="B Two", email="b@example.com")]).splitlines()[1:]
    lines = good + ["", ",,,,,,,,,,", ""] + more + [""]
    path = kit.write_raw("blanks.csv", "\n".join(lines) + "\n")
    assert [c.id for c in load_contacts(path)] == ["a", "b"]


def test_unicode_names_and_facts_round_trip(kit):
    path = kit.write_csv([kit.csv_row(
        id="u", name="\u00c5sa N\u00f8rgaard", email="asa@example.com",
        facts="grew a caf\u00e9 chain | writes in \u65e5\u672c\u8a9e",
    )])
    (c,) = load_contacts(path)
    assert c.name == "\u00c5sa N\u00f8rgaard"
    assert c.facts == ("grew a caf\u00e9 chain", "writes in \u65e5\u672c\u8a9e")


def test_whitespace_around_cells_is_stripped(kit):
    path = kit.write_csv([kit.csv_row(id=" a ", name="  Ada Lovelace ", email="  ada@example.com ", org=" Org ")])
    (c,) = load_contacts(path)
    assert (c.id, c.name, c.email, c.org) == ("a", "Ada Lovelace", "ada@example.com", "Org")


# ----- field counts (SPEC 19.6) ------------------------------------------------------------------

GOOD_LINE = "ga,Gina A,gina@example.com,Org,,,,,,,"  # 11 fields, like the default header


def raw_csv(kit, *lines, header=None, name="raw.csv", newline="\n"):
    header = list(header) if header is not None else list(kit.CSV_HEADER)
    return kit.write_raw(name, newline.join([",".join(header), *lines]) + newline)


def fields_message(row, got, want):
    return f"row {row} has {got} fields but the header has {want} (is there an unquoted comma?)"


def test_the_good_line_helper_really_has_as_many_fields_as_the_header(kit):
    assert GOOD_LINE.count(",") + 1 == len(kit.CSV_HEADER)
    (c,) = load_contacts(raw_csv(kit, GOOD_LINE))
    assert c.email == "gina@example.com"


def test_a_row_with_too_many_fields_reports_the_exact_message(kit):
    path = raw_csv(kit, GOOD_LINE, "gb,Gina B,ginab@example.com,Acme, Inc.,,,,,,,")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert fields_message(3, 12, 11) in str(info.value)


def test_a_row_with_too_few_fields_reports_the_exact_message(kit):
    path = raw_csv(kit, GOOD_LINE, "gc,Gina C,ginac@example.com")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert fields_message(3, 3, 11) in str(info.value)


def test_a_row_that_stops_before_the_email_is_reported_by_its_field_count(kit):
    path = raw_csv(kit, GOOD_LINE, GOOD_LINE.replace("ga,Gina A,gina", "gd,Gina D,ginad"), "lonely,Only A Name")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert fields_message(4, 2, 11) in str(info.value)


def test_the_header_width_in_the_message_counts_unknown_columns_too(kit):
    header = kit.CSV_HEADER + ["linkedin", "notes"]
    path = raw_csv(kit, "ga,Gina A,gina@example.com,Org,,,,,,,", header=header)
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert fields_message(2, 11, 13) in str(info.value)


def test_every_field_count_problem_is_reported_in_one_error(kit):
    path = raw_csv(kit, GOOD_LINE, "gb,Gina B,ginab@example.com,Acme, Inc.,,,,,,,", GOOD_LINE.replace("ga,Gina A,gina", "ge,Gina E,ginae"),
                   "gc,Gina C,ginac@example.com")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    text = str(info.value)
    assert fields_message(3, 12, 11) in text and fields_message(5, 3, 11) in text
    assert "row 2 " not in text and "row 4 " not in text, "the well formed rows must not be reported"


def test_field_counts_ignore_the_bom_and_crlf_line_endings(kit):
    lines = [",".join(kit.CSV_HEADER), "gb,Gina B,ginab@example.com,Acme, Inc.,,,,,,,"]
    path = kit.write_raw("excel.csv", b"\xef\xbb\xbf" + "\r\n".join(lines).encode("utf-8") + b"\r\n")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert fields_message(2, 12, 11) in str(info.value)


def test_a_comma_inside_quotes_is_not_a_field_count_problem(kit):
    path = raw_csv(kit, 'gq,Gina Q,ginaq@example.com,"Acme, Inc.",,,,,,,')
    (c,) = load_contacts(path)
    assert c.org == "Acme, Inc."


# ----- facts and source_urls separators -------------------------------------------

@pytest.mark.parametrize(
    "cell,expected",
    [
        ("alpha | beta", ("alpha", "beta")),
        ("alpha | beta | gamma", ("alpha", "beta", "gamma")),
        ("  alpha   |   beta  |  gamma ", ("alpha", "beta", "gamma")),
        ("only one", ("only one",)),
        ("", ()),
        ("   ", ()),
    ],
)
def test_facts_are_split_on_space_pipe_space_and_trimmed(kit, cell, expected):
    path = kit.write_csv([kit.csv_row(id="f", name="F G", email="f@example.com", facts=cell)])
    (c,) = load_contacts(path)
    assert c.facts == expected


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("alpha|beta", ("alpha|beta",)),
        ("alpha |beta", ("alpha |beta",)),
        ("alpha| beta", ("alpha| beta",)),
        ("you grew revenue 5|6 percent", ("you grew revenue 5|6 percent",)),
        ("your talk on A|B testing | your essay on pricing", ("your talk on A|B testing", "your essay on pricing")),
        ("one|two|three", ("one|two|three",)),
    ],
)
def test_a_bare_pipe_does_not_split_a_fact(kit, cell, expected):
    path = kit.write_csv([kit.csv_row(id="f", name="F G", email="f@example.com", facts=cell)])
    (c,) = load_contacts(path)
    assert c.facts == expected


def test_a_newline_inside_a_quoted_fact_becomes_a_space(kit):
    cell = "your essay on\nsmall teams | your talk on\nmaking software readable"
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="n", name="N M", email="n@example.com", facts=cell)]))
    assert c.facts == ("your essay on small teams", "your talk on making software readable")


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_no_newline_character_survives_inside_a_fact(kit, newline):
    cell = f"your essay{newline}on small teams"
    path = kit.write_csv([kit.csv_row(id="n", name="N M", email="n@example.com", facts=cell)], crlf=newline == "\r\n")
    (c,) = load_contacts(path)
    (fact,) = c.facts
    assert "\n" not in fact and "\r" not in fact
    assert fact.split() == ["your", "essay", "on", "small", "teams"]


def test_a_fact_wrapped_over_several_lines_in_a_spreadsheet_cell_reads_as_one_line(kit):
    cell = "\nyour essay\non how small\nteams ship weekly\n"
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="n", name="N M", email="n@example.com", facts=cell)]))
    assert len(c.facts) == 1
    assert c.facts[0].split() == "your essay on how small teams ship weekly".split()
    assert c.facts[0] == c.facts[0].strip()


def test_source_urls_use_the_same_pipe_separator(kit):
    path = kit.write_csv([kit.csv_row(
        id="s", name="S T", email="s@example.com",
        source_urls="https://one.example.com/a?x=1 | https://two.example.com",
    )])
    (c,) = load_contacts(path)
    assert c.source_urls == ("https://one.example.com/a?x=1", "https://two.example.com")


# ----- email validation --------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    ["not-an-email", "missing-tld@host", "@example.com", "two words@example.com",
     "a@@example.com", "", "plain.example.com", "trailing@dot."],
)
def test_invalid_email_raises_contacts_error_naming_the_row(kit, bad):
    path = kit.write_csv(five_rows(kit, {4: {"email": bad}}))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    kit.assert_names_rows(str(info.value), [5], 5, path)


@pytest.mark.parametrize(
    "good",
    ["a@example.com", "first.last+tag@sub.team.test", "UPPER@EXAMPLE.COM", "o-brien@my-co.test"],
)
def test_ordinary_email_shapes_are_accepted(kit, good):
    path = kit.write_csv([kit.csv_row(id="e", name="E F", email=good)])
    (c,) = load_contacts(path)
    assert c.email == good.lower()


def test_missing_email_column_is_a_contacts_error(kit):
    header = [h for h in kit.CSV_HEADER if h != "email"]
    path = kit.write_csv([kit.csv_row(id="a", name="A B")], header=header)
    with pytest.raises(ContactsError):
        load_contacts(path)


# ----- duplicates ------------------------------------------------------------------------

def test_duplicate_email_is_case_insensitive_and_lists_both_rows(kit):
    rows = five_rows(kit, {4: {"email": "PERSONB@Example.COM"}})
    path = kit.write_csv(rows)  # row 2 holds personb@example.com
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    kit.assert_names_rows(str(info.value), [2, 5], 5, path)


def test_duplicate_email_error_mentions_the_address(kit):
    rows = five_rows(kit, {3: {"email": "personb@example.com"}})
    path = kit.write_csv(rows)
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert "personb@example.com" in str(info.value).lower()


# ----- lanes and threading --------------------------------------------------------------------

def test_blank_lane_defaults_to_first_touch(kit):
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="a", name="A B", email="a@example.com", lane="")]))
    assert c.lane is Lane.FIRST_TOUCH


def test_follow_up_without_in_reply_to_is_an_error_naming_the_row(kit):
    rows = five_rows(kit, {4: {"lane": "follow_up", "thread_subject": "a note for Person"}})
    path = kit.write_csv(rows)
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    kit.assert_names_rows(str(info.value), [5], 5, path)


def test_follow_up_with_in_reply_to_loads(kit):
    rows = five_rows(kit, {1: {
        "lane": "follow_up", "thread_subject": "a note for Person",
        "in_reply_to": "<orig@example.com>",
    }})
    contacts = load_contacts(kit.write_csv(rows))
    assert contacts[1].lane is Lane.FOLLOW_UP
    assert contacts[1].in_reply_to == "<orig@example.com>"
    assert contacts[0].lane is Lane.FIRST_TOUCH


def test_unknown_lane_value_is_a_contacts_error(kit):
    path = kit.write_csv(five_rows(kit, {2: {"lane": "second_touch"}}))
    with pytest.raises(ContactsError):
        load_contacts(path)


# ----- ids ---------------------------------------------------------------------------------------

def test_explicit_id_is_kept(kit):
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="my-id", name="A B", email="a@example.com")]))
    assert c.id == "my-id"


def test_blank_id_defaults_to_a_filesystem_safe_slug_of_the_email(kit):
    (c,) = load_contacts(kit.write_csv([kit.csv_row(name="A B", email="Ada.Lovelace+x@Sub.Example.com")]))
    assert c.id
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", c.id), c.id
    assert "@" not in c.id and "/" not in c.id and " " not in c.id
    assert "ada" in c.id and "example" in c.id


def test_slug_is_deterministic_and_distinguishes_different_emails(kit):
    rows = [
        kit.csv_row(name="A B", email="ada@example.com"),
        kit.csv_row(name="A C", email="ada@example.org"),
        kit.csv_row(name="A D", email="bob@example.com"),
    ]
    first = [c.id for c in load_contacts(kit.write_csv(rows))]
    second = [c.id for c in load_contacts(kit.write_csv(rows, name="again.csv"))]
    assert first == second
    assert len(set(first)) == 3


@pytest.mark.parametrize("email", ["../../etc@example.com", "a/b@example.com", "x\\y@example.com", ".hidden@example.com"])
def test_slug_never_contains_path_separators_even_for_hostile_local_parts(kit, email):
    try:
        (c,) = load_contacts(kit.write_csv([kit.csv_row(name="A B", email=email)]))
    except ContactsError:
        return  # rejecting the address outright is also safe
    assert "/" not in c.id and "\\" not in c.id
    assert ".." not in c.id
    assert not c.id.startswith(".")


# ----- the first_name column and lower-cased emails (SPEC 19.4) -------------------------------------

def test_the_optional_first_name_column_becomes_preferred_name(kit):
    header = kit.CSV_HEADER + ["first_name"]
    rows = [
        dict(kit.csv_row(id="a", name="Dr. Alan Turing", email="alan@example.com"), first_name=" Al "),
        dict(kit.csv_row(id="b", name="Dr. Grace Hopper", email="grace@example.com"), first_name=""),
    ]
    first, second = load_contacts(kit.write_csv(rows, header=header))
    assert first.preferred_name == "Al" and first.first_name == "Al"
    assert second.preferred_name == "" and second.first_name == "Grace"


def test_the_first_name_column_can_sit_anywhere_in_the_header(kit):
    header = ["first_name"] + kit.CSV_HEADER
    rows = [dict(kit.csv_row(id="a", name="Alexander Hamilton", email="alex@example.com"), first_name="Alex")]
    (c,) = load_contacts(kit.write_csv(rows, header=header))
    assert c.preferred_name == "Alex" and c.name == "Alexander Hamilton"


def test_a_file_without_a_first_name_column_still_loads_and_cleans_the_name(kit):
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="a", name="Prof. Ada Lovelace", email="ada@example.com")]))
    assert c.preferred_name == ""
    assert c.first_name == "Ada"


def test_a_last_comma_first_name_in_a_csv_greets_by_the_first_name(kit):
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="a", name="Hopper, Grace", email="grace@example.com")]))
    assert c.name == "Hopper, Grace" and c.first_name == "Grace"


@pytest.mark.parametrize("raw", ["Ada.Lovelace@Example.COM", "UPPER@EXAMPLE.COM", "mixed.Case+Tag@Sub.Example.org"])
def test_emails_are_lower_cased_at_load_time(kit, raw):
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="e", name="E F", email=raw)]))
    assert c.email == raw.lower()


def test_the_default_id_of_a_mixed_case_email_matches_the_lower_case_one(kit):
    (upper,) = load_contacts(kit.write_csv([kit.csv_row(name="A B", email="Ada.Lovelace@Example.COM")], name="upper.csv"))
    (lower,) = load_contacts(kit.write_csv([kit.csv_row(name="A B", email="ada.lovelace@example.com")], name="lower.csv"))
    assert upper.id == lower.id and upper.email == lower.email


def test_only_the_email_is_lower_cased_not_the_message_ids(kit):
    row = kit.csv_row(id="g", name="G H", email="G@Example.com", lane="follow_up", thread_subject="Intro For Grace",
                      in_reply_to="<Orig-ABC@Example.com>", references="<Root-1@Example.com> <Orig-ABC@Example.com>")
    (c,) = load_contacts(kit.write_csv([row]))
    assert c.email == "g@example.com"
    assert c.in_reply_to == "<Orig-ABC@Example.com>"
    assert c.references == "<Root-1@Example.com> <Orig-ABC@Example.com>"
    assert c.thread_subject == "Intro For Grace"


# ----- follow_up rows need a real Message-ID and a subject (SPEC 19.6) -------------------------------

def follow_up_row(kit, **over):
    base = {"lane": "follow_up", "thread_subject": "a note for Person", "in_reply_to": "<orig@example.com>"}
    base.update(over)
    return base


@pytest.mark.parametrize(
    "bad",
    ["orig@example.com", "<orig-example.com>", "orig@example.com>", "<orig@example.com", "<>", "", "   ", "see my last email"],
    ids=["no-brackets", "no-at-sign", "no-open-bracket", "no-close-bracket", "empty-brackets", "empty", "blank", "prose"],
)
def test_a_follow_up_in_reply_to_must_look_like_a_message_id(kit, bad):
    path = kit.write_csv(five_rows(kit, {2: follow_up_row(kit, in_reply_to=bad)}))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    kit.assert_names_rows(str(info.value), [3], 5, path)


@pytest.mark.parametrize(
    "good",
    ["<orig@example.com>", "<a.b+c@sub.team.test>", "<20260101.abc123@mail.team.test>",
     "<CAF=abc+def@mail.team.test>", " <orig@example.com> ", "<sample-thread-001@example.com>"],
)
def test_a_well_formed_message_id_is_accepted_for_a_follow_up(kit, good):
    contacts = load_contacts(kit.write_csv(five_rows(kit, {2: follow_up_row(kit, in_reply_to=good)})))
    assert contacts[2].lane is Lane.FOLLOW_UP and contacts[2].in_reply_to == good.strip()


@pytest.mark.parametrize("bad", ["", "   "], ids=["empty", "blank"])
def test_a_follow_up_without_a_thread_subject_is_an_error_naming_the_row(kit, bad):
    path = kit.write_csv(five_rows(kit, {1: follow_up_row(kit, thread_subject=bad)}))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    kit.assert_names_rows(str(info.value), [2], 5, path)


def test_a_first_touch_row_does_not_need_threading_fields(kit):
    contacts = load_contacts(kit.write_csv(five_rows(kit)))
    assert all(c.in_reply_to == "" and c.thread_subject == "" for c in contacts)


# ----- every problem in one pass (SPEC 19.6) --------------------------------------------------------------

def rows_named(message):
    return sorted({int(n) for n in re.findall(r"\brow\s+(\d+)", message, re.IGNORECASE)})


def mentions(line, number):
    return re.search(rf"(?<!\d){number}(?!\d)", line) is not None


def two_letter_ids(n):
    return ["".join(pair) for pair in itertools.islice(itertools.product(LETTERS, repeat=2), n)]


def all_bad_rows(kit, n):
    return [kit.csv_row(id=i, name=f"Person {i.upper()}", email=f"not-an-email-{i}", org="Org") for i in two_letter_ids(n)]


def test_bad_emails_in_several_rows_are_all_reported_at_once(kit):
    path = kit.write_csv(five_rows(kit, {0: {"email": "nope"}, 2: {"email": "also nope"}, 4: {"email": "@example.com"}}))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    named = rows_named(str(info.value))
    assert named == [2, 4, 6], str(info.value)


def test_different_kinds_of_problems_are_reported_together(kit):
    rows = five_rows(kit, {
        1: {"email": "not-an-email"},
        3: {"lane": "follow_up", "thread_subject": "a note for Person"},
        4: {"email": "PERSONA@example.com"},
    })
    path = kit.write_csv(rows)
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    message = str(info.value)
    lines = [ln for ln in message.splitlines() if ln.strip()]
    named = rows_named(message)
    assert 3 in named, "bad email on file row 3"
    assert 5 in named, "follow_up without in_reply_to on file row 5"
    assert any(mentions(ln, 2) and mentions(ln, 6) for ln in lines), "the duplicate email names both of its rows"
    assert 4 not in named, "file row 4 is fine"
    assert len({ln for ln in lines if any(mentions(ln, n) for n in (2, 3, 5, 6))}) >= 3, "one line per problem"


def test_the_report_has_one_line_per_problem(kit):
    path = kit.write_csv(five_rows(kit, {0: {"email": "nope"}, 2: {"email": "also nope"}, 4: {"email": "@example.com"}}))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    problem_lines = [ln for ln in str(info.value).splitlines() if re.search(r"\brow\s+\d+", ln, re.IGNORECASE)]
    assert len(problem_lines) == 3
    for ln in problem_lines:
        assert len(re.findall(r"\brow\s+\d+", ln, re.IGNORECASE)) == 1


def test_follow_up_problems_in_several_rows_are_reported_together(kit):
    rows = five_rows(kit, {
        0: follow_up_row(kit, in_reply_to="orig@example.com"),
        1: follow_up_row(kit, thread_subject=""),
        2: follow_up_row(kit, in_reply_to="<no-at-sign>"),
        3: follow_up_row(kit),
    })
    with pytest.raises(ContactsError) as info:
        load_contacts(kit.write_csv(rows))
    assert rows_named(str(info.value)) == [2, 3, 4]


def test_a_field_count_problem_and_a_bad_email_are_reported_together(kit):
    path = raw_csv(kit, GOOD_LINE, "gb,Gina B,not-an-email,Org,,,,,,,", "gc,Gina C,ginac@example.com", name="mixed.csv")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    message = str(info.value)
    assert fields_message(4, 3, 11) in message
    assert 3 in rows_named(message)


@pytest.mark.parametrize("count,expected_rows", [
    (25, list(range(2, 27))),
    (26, list(range(2, 27))),
    (30, list(range(2, 27))),
], ids=["exactly-25", "26-capped", "30-capped"])
def test_at_most_25_problems_are_reported(kit, count, expected_rows):
    path = kit.write_csv(all_bad_rows(kit, count))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert rows_named(str(info.value)) == expected_rows


def test_a_file_with_fewer_problems_than_the_cap_reports_them_all(kit):
    path = kit.write_csv(all_bad_rows(kit, 7))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert rows_named(str(info.value)) == list(range(2, 9))


def test_one_bad_row_among_many_good_ones_names_only_that_row(kit):
    rows = [kit.csv_row(id=i, name=f"Person {i.upper()}", email=f"person{i}@example.com", org="Org") for i in two_letter_ids(12)]
    rows[9]["email"] = "broken"
    with pytest.raises(ContactsError) as info:
        load_contacts(kit.write_csv(rows))
    assert rows_named(str(info.value)) == [11]


# ----- files that are not CSV at all (SPEC 20.7) -----------------------------------------------------

JSON_HINT = "looks like JSON, expected a CSV file with a header row"
SINGLE_COLUMN_HINT = ("has a single column; is it separated by semicolons or tabs? "
                      "Save it as a comma-separated UTF-8 CSV")


@pytest.mark.parametrize(
    "text",
    ['{"contacts": [{"name": "Ada", "email": "ada@example.com"}]}', '[{"name": "Ada", "email": "ada@example.com"}]',
     '  \n\t {\n  "contacts": []\n}\n', "\n\n[\n]\n", '{"id": "a"}'],
    ids=["object", "array", "indented-object", "blank-lines-then-array", "tiny-object"],
)
def test_a_json_file_is_reported_as_json_not_as_a_broken_csv(kit, text):
    path = kit.write_raw("contacts.json", text)
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    message = str(info.value)
    assert re.search(r"\S*contacts\.json " + re.escape(JSON_HINT), message), message
    assert "single column" not in message


def test_a_csv_with_an_ordinary_header_is_not_mistaken_for_json(kit):
    (c,) = load_contacts(kit.write_csv([kit.csv_row(id="j", name="Json Fan", email="json@example.com", org='{"a": 1}')]))
    assert c.org == '{"a": 1}'


@pytest.mark.parametrize("delimiter", [";", "\t"], ids=["semicolon", "tab"])
def test_a_semicolon_or_tab_separated_file_gets_the_single_column_hint(kit, delimiter):
    header = delimiter.join(["id", "name", "email", "org"])
    row = delimiter.join(["a", "Ada Lovelace", "ada@example.com", "Org"])
    path = kit.write_raw("european.csv", header + "\n" + row + "\n")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    message = str(info.value)
    assert re.search(r"\S*european\.csv " + re.escape(SINGLE_COLUMN_HINT), message), message


def test_an_excel_semicolon_export_with_bom_and_crlf_gets_the_same_hint(kit):
    text = "id;name;email\r\na;Ada Lovelace;ada@example.com\r\n"
    path = kit.write_raw("excel-eu.csv", b"\xef\xbb\xbf" + text.encode("utf-8"))
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert SINGLE_COLUMN_HINT in str(info.value)


def test_a_one_column_file_without_a_delimiter_is_an_error_but_not_the_delimiter_hint(kit):
    path = kit.write_raw("one.csv", "email\nada@example.com\n")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert "single column" not in str(info.value)


def test_semicolons_and_tabs_inside_cells_of_a_normal_csv_are_fine(kit):
    row = kit.csv_row(id="s", name="Semi Colon", email="semi@example.com", org="Acme; Inc", role="Head\tof Growth")
    (c,) = load_contacts(kit.write_csv([row]))  # loading must not fail with the delimiter hint
    assert c.org == "Acme; Inc"
    assert "Head" in c.role and "Growth" in c.role, "whitespace inside a role may be normalized"


def test_a_zero_byte_file_says_it_is_empty_and_expects_a_header_row(kit):
    path = kit.write_raw("zero.csv", b"")
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    assert re.search(r"\S*zero\.csv is empty: expected a header row", str(info.value)), str(info.value)


def test_a_zero_byte_file_and_a_header_only_file_give_different_messages(kit):
    zero = kit.write_raw("zero.csv", b"")
    header_only = kit.write_csv([], name="header-only.csv")
    with pytest.raises(ContactsError) as zero_info:
        load_contacts(zero)
    try:
        load_contacts(header_only)
    except ContactsError as exc:
        assert str(exc) != str(zero_info.value)
        assert "is empty" not in str(exc)


# ----- file level failures -------------------------------------------------------------------------

def test_missing_file_is_a_clean_error(kit):
    with pytest.raises((ContactsError, FileNotFoundError)):
        load_contacts(kit.workdir / "nope.csv")


def test_latin1_encoded_file_either_loads_or_fails_cleanly(kit):
    data = kit.csv_text([kit.csv_row(id="l", name="Jos\u00e9 L", email="jose@example.com")]).encode("latin-1")
    path = kit.write_raw("latin1.csv", data)
    try:
        contacts = load_contacts(path)
    except ContactsError:
        return
    assert contacts[0].email == "jose@example.com"


# ----- the shipped sample file (SPEC section 17 sample data contract) -----------------------------

def sample_path():
    return resources.files("vcoutreach").joinpath("data", "examples", "contacts.sample.csv")


def test_sample_contacts_file_matches_the_published_contract():
    with resources.as_file(sample_path()) as p:
        contacts = load_contacts(Path(p))
    assert len(contacts) == 5
    lanes = [c.lane for c in contacts]
    assert lanes.count(Lane.FIRST_TOUCH) == 4 and lanes.count(Lane.FOLLOW_UP) == 1
    with_facts = [c for c in contacts if c.facts]
    assert len(with_facts) == 4
    assert sum(1 for c in contacts if c.lane is Lane.FIRST_TOUCH and not c.facts) == 1
    (follow_up,) = [c for c in contacts if c.lane is Lane.FOLLOW_UP]
    assert follow_up.thread_subject.strip()
    assert follow_up.in_reply_to == "<sample-thread-001@example.com>"
    assert follow_up.facts, "the follow_up sample row must have facts so it drafts"


def test_sample_contacts_use_only_reserved_example_domains_and_clean_facts():
    with resources.as_file(sample_path()) as p:
        contacts = load_contacts(Path(p))
    for c in contacts:
        assert c.email.lower().endswith(("@example.com", "@example.org")), c.email
        for fact in c.facts:
            assert "\u2014" not in fact and "\u2013" not in fact
            for digits in re.findall(r"\d+", fact):
                assert len(digits) <= 3 or re.fullmatch(r"(19|20)\d\d", digits), (fact, digits)
    assert len({c.email.lower() for c in contacts}) == 5
    assert len({c.id for c in contacts}) == 5



def test_sample_follow_up_row_satisfies_the_follow_up_rules():
    with resources.as_file(sample_path()) as p:
        contacts = load_contacts(Path(p))
    (follow_up,) = [c for c in contacts if c.lane is Lane.FOLLOW_UP]
    assert re.fullmatch(r"<[^<>@\s]+@[^<>\s]+>", follow_up.in_reply_to), follow_up.in_reply_to
    assert follow_up.thread_subject.strip()


def test_sample_greeting_names_are_clean_letters(kit):
    with resources.as_file(sample_path()) as p:
        contacts = load_contacts(Path(p))
    for c in contacts:
        assert re.fullmatch(r"[^\W\d_]{2,}(?:['-][^\W\d_]+)*", c.first_name), (c.name, c.first_name)


# ----- 21.1 ids are unique ignoring case (each draft is a file) -----------------------------------------------------------------------------

CASE_NOTE = "(ids that differ only by case count as duplicates, because each draft is a file)"


def raised(path):
    with pytest.raises(ContactsError) as info:
        load_contacts(path)
    return str(info.value)


def line_naming(message, *numbers):
    for ln in message.splitlines():
        if all(re.search(rf"(?<!\d){n}(?!\d)", ln) for n in numbers):
            return ln
    return None


def test_ids_that_differ_only_by_case_are_rejected_naming_both_rows(kit):
    path = kit.write_csv(five_rows(kit, {1: {"id": "Ann"}, 3: {"id": "ann"}}))
    message = raised(path)
    kit.assert_names_rows(message, [2, 4], 5, path)
    assert message.rstrip().endswith(CASE_NOTE), message


def test_the_case_note_sits_on_the_same_line_as_both_rows(kit):
    path = kit.write_csv(five_rows(kit, {0: {"id": "Ann"}, 4: {"id": "aNN"}}))
    message = raised(path)
    line = line_naming(message, 2, 6)
    assert line is not None and CASE_NOTE in line, message


@pytest.mark.parametrize("first,second", [("ann", "Ann"), ("ANN", "ann"), ("Ann-Lee", "ann-lee"), ("a", "A")],
                         ids=["lower-title", "upper-lower", "hyphenated", "single-letter"])
def test_any_pair_of_ids_that_match_ignoring_case_collides(kit, first, second):
    path = kit.write_csv(five_rows(kit, {0: {"id": first}, 2: {"id": second}}))
    message = raised(path)
    kit.assert_names_rows(message, [1, 3], 5, path)
    assert CASE_NOTE in message


def test_three_ids_that_match_ignoring_case_are_all_named(kit):
    path = kit.write_csv(five_rows(kit, {0: {"id": "ann"}, 2: {"id": "ANN"}, 4: {"id": "Ann"}}))
    message = raised(path)
    kit.assert_names_rows(message, [1, 3, 5], 5, path)


def test_an_exact_duplicate_id_is_rejected_naming_both_rows(kit):
    path = kit.write_csv(five_rows(kit, {1: {"id": "same"}, 3: {"id": "same"}}))
    message = raised(path)
    kit.assert_names_rows(message, [2, 4], 5, path)


def test_ids_that_merely_start_alike_are_not_duplicates(kit):
    path = kit.write_csv(five_rows(kit, {1: {"id": "ann"}, 3: {"id": "anna"}, 4: {"id": "ann-lee"}}))
    ids = [c.id for c in load_contacts(path)]
    assert ids == ["ca", "ann", "cc", "anna", "ann-lee"]


def test_a_case_collision_is_reported_together_with_other_row_problems(kit):
    path = kit.write_csv(five_rows(kit, {1: {"id": "Ann"}, 3: {"id": "ann"}, 4: {"email": "not-an-email"}}))
    message = raised(path)
    kit.assert_names_rows(message, [2, 4, 5], 5, path)
    assert CASE_NOTE in message


def test_the_same_id_in_different_case_in_two_files_is_not_a_collision(kit):
    one = kit.write_csv([row(kit, 0, id="Ann")], name="one.csv")
    two = kit.write_csv([row(kit, 1, id="ann")], name="two.csv")
    assert [c.id for c in load_contacts(one)] == ["Ann"]
    assert [c.id for c in load_contacts(two)] == ["ann"]
