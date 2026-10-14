"""lint.lint_draft (SPEC section 8, with the send-time rules of section 17).

Every lint code gets a positive case (it fires) and a negative case (a near miss stays quiet).
"""
from __future__ import annotations

import copy
import itertools

import pytest

from vcoutreach.lint import lint_draft
from vcoutreach.types import LintIssue

EM = "\u2014"
EN = "\u2013"
SUBJECT = "a note for Ada"

NEUTRAL_WORDS = [
    "steady", "careful", "small", "teams", "ship", "weekly", "because", "focus",
    "keeps", "scope", "honest", "and", "clear", "notes",
]


def words(n):
    return " ".join(itertools.islice(itertools.cycle(NEUTRAL_WORDS), n))


def codes(issues):
    return [i.code for i in issues]


def only(issues, code):
    return [i for i in issues if i.code == code]


def run(kit, *, subject=SUBJECT, body=None, sender=None, contact="default", hook=None):
    sender = sender or kit.sender()
    contact = kit.contact() if contact == "default" else contact
    body = kit.clean_body() if body is None else body
    hook = kit.HOOK if hook is None else hook
    return lint_draft(subject, body, sender, contact, hook)


def body_with_hook_paragraph(kit, paragraph):
    return kit.clean_body(hook=paragraph)


# ----- baseline -------------------------------------------------------------------------------------

def test_clean_draft_has_no_issues(kit):
    assert run(kit) == []


def test_result_is_a_plain_list_of_lint_issues(kit):
    issues = run(kit, body=kit.clean_body() + "!")
    assert isinstance(issues, list)
    assert issues and all(isinstance(i, LintIssue) for i in issues)
    assert all(i.severity in ("error", "warning") and i.message for i in issues)


# ----- em_dash ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("dash", [EM, EN], ids=["em", "en"])
def test_dash_in_body_is_flagged(kit, dash):
    body = body_with_hook_paragraph(kit, f"I came across your essay {dash} on small teams, and I found it interesting.")
    issues = run(kit, body=body)
    assert "em_dash" in codes(issues)
    assert all(i.severity == "error" for i in only(issues, "em_dash"))


@pytest.mark.parametrize("dash", [EM, EN], ids=["em", "en"])
def test_dash_in_subject_is_flagged(kit, dash):
    assert "em_dash" in codes(run(kit, subject=f"a note {dash} for Ada"))


def test_hyphens_and_ranges_with_ascii_hyphen_are_fine(kit):
    body = body_with_hook_paragraph(kit, "I came across your well-known essay on early-stage teams from 2020-2021, and I found it interesting.")
    assert "em_dash" not in codes(run(kit, body=body, subject="a note for Mary-Jane"))


# ----- hard_wrap ---------------------------------------------------------------------------------------

def test_single_newline_inside_the_hook_paragraph_is_a_hard_wrap(kit):
    body = body_with_hook_paragraph(kit, "I came across your essay on how small teams\nship weekly, and I found it interesting.")
    assert "hard_wrap" in codes(run(kit, body=body))


def test_single_newline_inside_the_ask_paragraph_is_a_hard_wrap(kit):
    body = kit.clean_body().replace("here is my calendar link:", "here is my\ncalendar link:")
    assert body != kit.clean_body()
    assert "hard_wrap" in codes(run(kit, body=body))


def test_single_newline_inside_the_greeting_is_a_hard_wrap(kit):
    body = kit.clean_body().replace("Hi Ada,\n\n", "Hi Ada,\nthere\n\n", 1)
    assert "hard_wrap" in codes(run(kit, body=body))


def test_multi_line_signature_is_exempt_from_hard_wrap(kit):
    sig = "Sam Rivera\nFounder, Lumenlist\nhttps://lumenlist.example.com\nSan Example, EX\n+0 000 000 0000"
    sender = kit.sender(signature=sig)
    body = kit.clean_body(sender=kit.sender_dict(signature=sig))
    assert "hard_wrap" not in codes(run(kit, body=body, sender=sender))


def test_trailing_newline_after_the_signature_is_not_a_hard_wrap(kit):
    assert "hard_wrap" not in codes(run(kit, body=kit.clean_body() + "\n"))


def test_paragraph_breaks_are_not_hard_wraps(kit):
    assert "hard_wrap" not in codes(run(kit))


# ----- filler ------------------------------------------------------------------------------------------------

FILLERS = [
    "I hope this finds you well", "just circling back", "bumping this", "excited to share",
    "quick question", "touch base", "leverage", "delve",
]


@pytest.mark.parametrize("phrase", FILLERS)
def test_each_filler_phrase_is_flagged(kit, phrase):
    body = body_with_hook_paragraph(kit, f"I came across your essay, and I found it interesting. {phrase} on that.")
    issues = run(kit, body=body)
    assert "filler" in codes(issues)
    assert all(i.severity == "error" for i in only(issues, "filler"))


@pytest.mark.parametrize("phrase", FILLERS)
def test_filler_matching_ignores_case(kit, phrase):
    body = body_with_hook_paragraph(kit, f"I came across your essay, and I found it interesting. {phrase.upper()} on that.")
    assert "filler" in codes(run(kit, body=body))


def test_ordinary_words_near_the_filler_list_do_not_trigger_it(kit):
    body = body_with_hook_paragraph(kit, "I came across your essay on how a lever, a base and a quick draft fit together, and I found it interesting.")
    assert "filler" not in codes(run(kit, body=body))


# ----- exclamation -----------------------------------------------------------------------------------------------

def test_exclamation_mark_in_body_is_flagged(kit):
    body = body_with_hook_paragraph(kit, "I came across your essay, and I found it interesting!")
    assert "exclamation" in codes(run(kit, body=body))


def test_question_marks_and_periods_are_not_exclamations(kit):
    body = body_with_hook_paragraph(kit, "Did you write that essay on small teams? I found it interesting.")
    assert "exclamation" not in codes(run(kit, body=body))


# ----- forbidden_phrase ---------------------------------------------------------------------------------------------

def test_sender_forbidden_phrase_in_body_is_flagged(kit):
    body = body_with_hook_paragraph(kit, "I came across your essay, and I see real synergy with small teams.")
    issues = run(kit, body=body)
    assert "forbidden_phrase" in codes(issues)
    assert any("synergy" in i.message for i in only(issues, "forbidden_phrase"))


def test_second_forbidden_phrase_is_checked_too(kit):
    body = body_with_hook_paragraph(kit, "I came across your essay, a real game changer for small teams.")
    assert "forbidden_phrase" in codes(run(kit, body=body))


def test_clean_body_with_forbidden_phrases_configured_is_quiet(kit):
    assert "forbidden_phrase" not in codes(run(kit, sender=kit.sender(forbidden_phrases=["rocket ship", "disrupt"])))


def test_no_forbidden_phrases_configured_never_flags(kit):
    body = body_with_hook_paragraph(kit, "I see real synergy and a game changer here.")
    assert "forbidden_phrase" not in codes(run(kit, body=body, sender=kit.sender(forbidden_phrases=[])))


def test_empty_string_entry_in_forbidden_phrases_does_not_flag_everything(kit):
    assert "forbidden_phrase" not in codes(run(kit, sender=kit.sender(forbidden_phrases=["", "  "])))


# ----- false_parallel (hook text only) -------------------------------------------------------------------------------

PARALLEL_HOOKS = [
    "Your writing on funding for women-founded startups, which is close to what we are doing at Lumenlist, caught my eye.",
    "Your platform is exactly what we are building, and I admire it.",
    "Your approach is precisely what we do every day.",
    "Your tooling is just like what we ship at Lumenlist.",
    "Your essays are exactly what we need to read.",
    "This is the same as what we do, and I respect it.",
    "Your focus on weekly shipping is exactly what we're doing.",
    "Your roadmap maps directly to what we think about.",
    "Your roadmap maps directly to how we think about it.",
]


@pytest.mark.parametrize("hook", PARALLEL_HOOKS)
def test_hook_claiming_the_recipient_work_is_what_the_sender_does_is_flagged(kit, hook):
    issues = run(kit, hook=hook)
    assert "false_parallel" in codes(issues)
    assert all(i.severity == "error" for i in only(issues, "false_parallel"))


@pytest.mark.parametrize("hook", [PARALLEL_HOOKS[1].upper(), "Same as what we do, honestly."])
def test_false_parallel_is_case_insensitive(kit, hook):
    assert "false_parallel" in codes(run(kit, hook=hook))


@pytest.mark.parametrize(
    "hook",
    [
        "Your talk on seed funding for women founders was the sharpest I read this year.",
        "I wonder what we could learn from your approach to weekly shipping.",
        "This is exactly what you described in your essay, and I agree.",
        "The same as what they do at Northwind, but with more patience.",
        "I respect how you map directly to what founders ask for.",
    ],
)
def test_near_miss_hooks_are_not_false_parallels(kit, hook):
    assert "false_parallel" not in codes(run(kit, hook=hook))


def test_false_parallel_is_judged_on_the_hook_not_the_body(kit):
    body = body_with_hook_paragraph(kit, "Your platform is exactly what we are building, and I admire it.")
    assert "false_parallel" not in codes(run(kit, body=body, hook=kit.HOOK))


# ----- unapproved_number (hook text only) -----------------------------------------------------------------------------

def test_number_missing_from_facts_and_claims_is_flagged(kit):
    contact = kit.contact(facts=("you backed 12 startups",))
    issues = run(kit, contact=contact, hook="I saw your fund has backed 40 startups, which I respect.")
    assert "unapproved_number" in codes(issues)
    assert all(i.severity == "error" for i in only(issues, "unapproved_number"))


def test_number_present_in_a_fact_is_allowed(kit):
    contact = kit.contact(facts=("you backed 12 startups in 2021",))
    issues = run(kit, contact=contact, hook="I saw you backed 12 startups in 2021, which I respect.")
    assert "unapproved_number" not in codes(issues)


def test_number_present_in_an_approved_claim_is_allowed(kit):
    sender = kit.sender(approved_claims={"reach": "about 10M organic views"})
    issues = run(kit, sender=sender, contact=kit.contact(facts=("your essay",)),
                 hook="I respect anyone who earns 10M readers.")
    assert "unapproved_number" not in codes(issues)


def test_hook_without_any_digits_never_triggers_it(kit):
    assert "unapproved_number" not in codes(run(kit))


def test_a_year_not_in_the_facts_is_flagged(kit):
    contact = kit.contact(facts=("your essay on small teams",))
    assert "unapproved_number" in codes(run(kit, contact=contact, hook="Your 2019 essay on small teams stayed with me."))


# ----- word_count (warning) ------------------------------------------------------------------------------------------------

def test_body_in_range_has_no_word_count_issue(kit):
    assert "word_count" not in codes(run(kit))


def test_short_body_gets_a_word_count_warning_not_an_error(kit):
    body = "Hi Ada,\n\nI liked your essay.\n\n" + kit.sender_dict()["signature"]
    issues = only(run(kit, body=body), "word_count")
    assert len(issues) == 1 and issues[0].severity == "warning"


def test_long_body_gets_a_word_count_warning(kit):
    issues = only(run(kit, body=body_with_hook_paragraph(kit, words(170))), "word_count")
    assert len(issues) == 1 and issues[0].severity == "warning"


def test_signature_words_do_not_rescue_a_short_body(kit):
    long_signature = "Sam Rivera\n" + "\n".join(words(10) for _ in range(8))
    body = "\n\n".join(["Hi Ada,", kit.HOOK, kit.sender_dict()["ask_line"], long_signature])
    issues = only(run(kit, body=body), "word_count")
    assert len(issues) == 1 and issues[0].severity == "warning"
    # SPEC 21.4a: nine lines of prose are not a signature, so the paragraph is checked like any other
    assert "hard_wrap" in codes(run(kit, body=body))


def test_signature_words_do_not_push_a_good_body_over_the_limit(kit):
    signature = "Sam Rivera\n" + "\n".join(words(10) for _ in range(2))
    body = "\n\n".join(["Hi Ada,", words(100), kit.sender_dict()["ask_line"], signature])
    assert "word_count" not in codes(run(kit, body=body))


# ----- empty_hook ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["   ", "\n\t  \n"])
def test_blank_hook_text_is_flagged(kit, blank):
    issues = run(kit, hook=blank)
    assert "empty_hook" in codes(issues)
    assert all(i.severity == "error" for i in only(issues, "empty_hook"))


def test_real_hook_text_is_not_empty(kit):
    assert "empty_hook" not in codes(run(kit))


# ----- send-time re-lint (SPEC section 17) --------------------------------------------------------------------------------------

def test_contact_none_skips_the_hook_only_checks(kit):
    issues = lint_draft(SUBJECT, kit.clean_body(), kit.sender(), None,
                        "Your platform is exactly what we are building, with 40 customers.")
    assert not {"false_parallel", "unapproved_number", "empty_hook"} & set(codes(issues))


def test_empty_hook_text_skips_the_hook_only_checks(kit):
    issues = lint_draft(SUBJECT, kit.clean_body(), kit.sender(), kit.contact(), "")
    assert not {"false_parallel", "unapproved_number", "empty_hook"} & set(codes(issues))
    assert issues == []


def test_send_time_relint_still_catches_body_level_problems(kit):
    body = kit.clean_body().replace("weekly,", f"weekly {EM}")
    issues = lint_draft(SUBJECT, body, kit.sender(), None)
    assert "em_dash" in codes(issues)


def test_send_time_relint_catches_exclamation_filler_forbidden_and_hard_wrap(kit):
    broken = {
        "exclamation": kit.clean_body(hook="I came across your essay, and I found it interesting!"),
        "filler": kit.clean_body(hook="I came across your essay. Just circling back on it."),
        "forbidden_phrase": kit.clean_body(hook="I came across your essay about synergy."),
        "hard_wrap": kit.clean_body(hook="I came across your essay on how\nsmall teams ship."),
    }
    for expected, body in broken.items():
        assert expected in codes(lint_draft(SUBJECT, body, kit.sender(), None)), expected


# ----- purity -----------------------------------------------------------------------------------------------------------------------

def test_lint_does_not_mutate_its_inputs(kit):
    sender = kit.sender()
    contact = kit.contact()
    claims_before = dict(sender.approved_claims)
    sender_before = copy.deepcopy(sender)
    contact_before = copy.deepcopy(contact)
    body = kit.clean_body(hook=f"I came across your essay {EM} on small teams, and I found it interesting!")
    body_before = str(body)
    issues = lint_draft(SUBJECT, body, sender, contact, kit.HOOK)
    assert issues
    assert body == body_before and EM in body
    assert sender == sender_before and contact == contact_before
    assert dict(sender.approved_claims) == claims_before


def test_lint_is_idempotent_and_returns_a_fresh_list_each_time(kit):
    body = kit.clean_body(hook="I came across your essay, and I found it interesting!")
    first = run(kit, body=body)
    second = run(kit, body=body)
    assert first == second
    assert first is not second
    first.clear()
    assert run(kit, body=body) == second


def test_several_problems_are_all_reported_together(kit):
    body = kit.clean_body(hook=f"Just circling back {EM} your essay was great! It has real synergy.")
    found = set(codes(run(kit, body=body)))
    assert {"em_dash", "filler", "exclamation", "forbidden_phrase"} <= found


# ----- odd_greeting (SPEC 19.4) ---------------------------------------------------------------------------------

SLASH = chr(0x2F)
E_DIAERESIS = chr(0xEB)
A_RING_UP = chr(0xC5)
CJK_NAME = chr(0x674E) + chr(0x96F7)
EMOJI = chr(0x1F600)

ODD_NAMES = [
    ("", "empty name"),
    ("   ", "blank name"),
    ("Dr.", "only a title"),
    ("J Smith", "single initial"),
    ("A. Lovelace", "initial with a period"),
    ("R2D2 Bot", "digits"),
    ("1st Place", "starts with a digit"),
    ("Ada_Lovelace Smith", "underscore"),
    ("Ada" + SLASH + "Bob Smith", "slash"),
    ("Ada&Bob Smith", "ampersand"),
    ("@ada Smith", "at sign"),
    ("Ada" + EMOJI + " Smith", "emoji"),
    ("Ada(Bob) Smith", "parentheses"),
]

FINE_NAMES = [
    "Ada Lovelace", "Mary-Jane Watson", "O'Brien Pat", "d'Artagnan Dumas", "Al Pacino", "Li Wei",
    "Zo" + E_DIAERESIS + " " + A_RING_UP + "kesson", CJK_NAME, "Dr. Jane Roe", "Smith, Alice", "ALICE SMITH",
]


def odd_issues(kit, **contact_over):
    return only(run(kit, contact=kit.contact(**contact_over)), "odd_greeting")


@pytest.mark.parametrize("name,why", ODD_NAMES, ids=[why for _, why in ODD_NAMES])
def test_an_odd_greeting_name_earns_a_warning(kit, name, why):
    (issue,) = odd_issues(kit, name=name)
    assert issue.severity == "warning" and issue.message


@pytest.mark.parametrize("name", FINE_NAMES)
def test_ordinary_greeting_names_earn_no_warning(kit, name):
    assert odd_issues(kit, name=name) == []


def test_an_odd_greeting_is_a_warning_only_so_nothing_else_changes(kit):
    issues = run(kit, contact=kit.contact(name="J Smith"))
    assert codes(issues) == ["odd_greeting"]
    assert not [i for i in issues if i.severity == "error"]


def test_the_warning_looks_at_the_greeting_name_not_at_the_body_text(kit):
    body = kit.clean_body(first_name="J")
    assert odd_issues(kit, name="Ada Lovelace") == []
    assert only(run(kit, body=body, contact=kit.contact(name="Ada Lovelace")), "odd_greeting") == []


def test_a_preferred_name_replaces_the_name_for_the_greeting_check(kit):
    assert odd_issues(kit, name="J Smith", preferred_name="Jay") == []
    assert len(odd_issues(kit, name="Ada Lovelace", preferred_name="A")) == 1
    assert len(odd_issues(kit, name="Ada Lovelace", preferred_name="Ada2")) == 1


def test_without_a_contact_there_is_no_greeting_name_to_judge(kit):
    issues = lint_draft(SUBJECT, kit.clean_body(), kit.sender(), None, "")
    assert only(issues, "odd_greeting") == []


def test_two_letters_is_long_enough(kit):
    assert odd_issues(kit, name="Al Pacino") == []
    assert len(odd_issues(kit, name="A Pacino")) == 1


# ----- hard_wrap: the final paragraph is exempt only when it looks like a signature (SPEC 21.4a) ----------------------------

TWO_SENTENCES_WRAPPED = "I really enjoyed your essay on small teams.\nI would love to hear what you think."
PROSE_SIGNATURE = "Sam Rivera, Founder.\nLumenlist makes outreach calm.\nWrite to me any time.\nOr visit our site?\nThanks again."


def with_final_paragraph(kit, final, *, sender_dict=None):
    paragraphs = kit.clean_body(sender=sender_dict).split("\n\n")
    return "\n\n".join(paragraphs[:-1] + [final])


def test_a_final_paragraph_of_two_sentences_wrapped_over_two_lines_is_flagged(kit):
    body = with_final_paragraph(kit, TWO_SENTENCES_WRAPPED)
    found = only(run(kit, body=body), "hard_wrap")
    assert found and all(i.severity == "error" for i in found)


def test_the_verdict_on_the_final_paragraph_does_not_depend_on_a_trailing_newline(kit):
    assert "hard_wrap" in codes(run(kit, body=with_final_paragraph(kit, TWO_SENTENCES_WRAPPED) + "\n"))
    assert "hard_wrap" not in codes(run(kit, body=with_final_paragraph(kit, "Sam\nPublik") + "\n"))


def test_a_short_two_line_name_and_company_that_is_not_the_senders_signature_is_not_flagged(kit):
    assert "hard_wrap" not in codes(run(kit, body=with_final_paragraph(kit, "Sam\nPublik")))


def test_the_sender_signature_is_never_flagged_even_when_it_reads_like_prose(kit):
    sender = kit.sender(signature=PROSE_SIGNATURE)
    body = kit.clean_body(sender=kit.sender_dict(signature=PROSE_SIGNATURE))
    assert "hard_wrap" not in codes(run(kit, body=body, sender=sender))


def test_the_same_prose_is_flagged_when_it_is_not_the_senders_signature(kit):
    body = kit.clean_body(sender=kit.sender_dict(signature=PROSE_SIGNATURE))
    assert "hard_wrap" in codes(run(kit, body=body))


def test_a_single_paragraph_body_wrapped_over_two_lines_is_flagged(kit):
    body = "Hi Ada, I really enjoyed your essay on small teams.\nI would love to hear what you think about our product."
    assert "hard_wrap" in codes(run(kit, body=body))


def test_a_single_paragraph_of_many_short_lines_is_flagged(kit):
    assert "hard_wrap" in codes(run(kit, body="\n".join(f"line {w}" for w in ("one", "two", "three", "four", "five", "six"))))


def test_a_single_line_final_paragraph_is_never_a_hard_wrap(kit):
    assert "hard_wrap" not in codes(run(kit, body=with_final_paragraph(kit, "Thank you for reading this note about small teams.")))


SIGNATURE_LIKE = [
    ("Sam\nPublik", "name-and-company"),
    ("Best,\nSam Rivera", "classic-sign-off"),
    ("Sam\nFounder\nLumenlist\nsam@lumenlist.test", "four-lines"),
    ("Sam Rivera\nFounder, Lumenlist\nhttps://lumenlist.example.com", "name-title-url"),
    ("Sam Rivera:\nFounder (Lumenlist)", "colon-and-parens"),
]


@pytest.mark.parametrize("final", [f for f, _ in SIGNATURE_LIKE], ids=[i for _, i in SIGNATURE_LIKE])
def test_signature_shaped_final_paragraphs_are_exempt(kit, final):
    assert "hard_wrap" not in codes(run(kit, body=with_final_paragraph(kit, final)))


LINE_60 = ("Publik " * 9)[:60]
LINE_61 = ("Publik " * 9)[:61]

PROSE_LIKE = [
    ("Sam\nFounder\nLumenlist\nBuilder\nOf lists", "five-lines"),
    ("Sam\n" + LINE_61, "a-line-of-61-characters"),
    ("Sam\nPublik, Inc.", "line-ends-with-a-period"),
    ("Sam\nWhat do you think?", "line-ends-with-a-question-mark"),
    (TWO_SENTENCES_WRAPPED, "two-sentences"),
]


@pytest.mark.parametrize("final", [f for f, _ in PROSE_LIKE], ids=[i for _, i in PROSE_LIKE])
def test_final_paragraphs_that_do_not_look_like_a_signature_are_checked(kit, final):
    assert "hard_wrap" in codes(run(kit, body=with_final_paragraph(kit, final)))


def test_a_line_of_exactly_sixty_characters_still_looks_like_a_signature(kit):
    assert len(LINE_60) == 60 and LINE_60[-1] not in ".?!"
    assert "hard_wrap" not in codes(run(kit, body=with_final_paragraph(kit, "Sam\n" + LINE_60)))


def test_a_final_line_ending_in_an_exclamation_mark_is_checked_too(kit):
    issues = run(kit, body=with_final_paragraph(kit, "Sam\nThank you!"))
    assert "hard_wrap" in codes(issues)


def test_the_send_time_relint_without_a_contact_applies_the_same_rule(kit):
    wrapped = lint_draft(SUBJECT, with_final_paragraph(kit, TWO_SENTENCES_WRAPPED), kit.sender(), None, "")
    signed = lint_draft(SUBJECT, with_final_paragraph(kit, "Sam\nPublik"), kit.sender(), None, "")
    assert "hard_wrap" in codes(wrapped)
    assert "hard_wrap" not in codes(signed)


def test_wrapped_paragraphs_before_the_signature_are_still_flagged_whatever_the_signature(kit):
    body = kit.clean_body(hook="I came across your essay on how small teams\nship weekly, and I found it interesting.")
    assert "hard_wrap" in codes(run(kit, body=body))


# ----- odd_greeting: the extended word list (SPEC 21.4b) -----------------------------------------------------------------------------

LISTED_GREETING_WORDS = ["the", "team", "staff", "info", "sales", "hello", "hi", "dear", "sir", "madam", "phd", "md", "mba",
                         "jr", "sr", "ii", "iii", "iv", "cpa", "cfa", "esq"]


@pytest.mark.parametrize("word", LISTED_GREETING_WORDS)
def test_each_listed_word_as_the_greeting_earns_the_warning_in_any_case(kit, word):
    for form in (word, word.capitalize(), word.upper()):
        (issue,) = odd_issues(kit, name="Ada Lovelace", preferred_name=form)
        assert issue.severity == "warning", form


@pytest.mark.parametrize("name", ["The Smith Team", "Team Lumenlist", "Sales Team", "Info Desk", "Hello Capital", "Hi Fund",
                                  "Staff Writer", "Dear Founder"])
def test_names_that_begin_with_a_listed_word_earn_the_warning(kit, name):
    assert len(odd_issues(kit, name=name)) == 1


@pytest.mark.parametrize("name", ["Theodore Roosevelt", "Sirius Black", "Ivy Lee", "Jrue Holiday", "Salesh Kumar", "Hilda Graef",
                                  "Deardorff Pat", "Teamo Lee"])
def test_names_that_merely_start_with_a_listed_word_are_fine(kit, name):
    assert odd_issues(kit, name=name) == []


@pytest.mark.parametrize("name", ["Alice Brown, PhD", "Alice Brown, Ph.D.", "Smith, Taylor", "John Smith, Jr.", "Dr. Alice Brown, MD",
                                  "Ann Lee, CPA", "Sam Reed III"])
def test_a_suffix_after_the_name_does_not_make_the_greeting_odd(kit, name):
    assert odd_issues(kit, name=name) == []


def test_the_known_imperfect_smith_jr_john_case_does_not_crash_lint(kit):
    issues = odd_issues(kit, name="Smith, Jr., John")
    assert isinstance(issues, list)
