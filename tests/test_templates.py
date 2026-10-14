"""templates.render_body / render_subject and the packaged template files (SPEC section 7)."""
from __future__ import annotations

import importlib.resources as resources
import re

import pytest

from vcoutreach.lint import lint_draft
from vcoutreach.sender import load_sender
from vcoutreach.templates import render_body, render_subject
from vcoutreach.types import Lane

FOLLOW_UP_OPENER = "Following up in case you missed my previous email."
PLACEHOLDERS = ["{first_name}", "{hook}", "{product_line}", "{traction_line}", "{ask_line}", "{signature}"]


def paragraphs(body):
    return body.strip("\n").split("\n\n")


def first_touch(kit, sender=None, hook=None, **contact_over):
    sender = sender or kit.sender()
    hook = hook if hook is not None else kit.HOOK
    contact = kit.contact(**contact_over)
    return render_body(contact, sender, hook), contact, sender


def follow_up(kit, sender=None, hook=None, **contact_over):
    sender = sender or kit.sender()
    hook = hook if hook is not None else kit.HOOK
    contact = kit.follow_up_contact(**contact_over)
    return render_body(contact, sender, hook), contact, sender


# ----- packaged template files -----------------------------------------------------------------------

def template_text(name):
    return resources.files("vcoutreach").joinpath("data", "templates", name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["first_touch.txt", "follow_up.txt"])
def test_template_files_ship_as_package_data_with_every_placeholder(name):
    text = template_text(name)
    for placeholder in PLACEHOLDERS:
        assert placeholder in text, f"{name} lacks {placeholder}"


@pytest.mark.parametrize("name", ["first_touch.txt", "follow_up.txt"])
def test_template_paragraphs_are_single_lines_without_dashes_or_exclamations(name):
    text = template_text(name).strip("\n")
    for para in text.split("\n\n"):
        assert "\n" not in para, f"hard wrap inside {para!r}"
    assert "\u2014" not in text and "\u2013" not in text and "!" not in text


def test_follow_up_template_has_the_exact_opener_paragraph_after_the_greeting():
    paras = template_text("follow_up.txt").strip("\n").split("\n\n")
    assert paras[0] == "Hi {first_name},"
    assert paras[1] == f"{FOLLOW_UP_OPENER} {{hook}}"


def test_first_touch_template_starts_with_the_greeting_then_the_hook():
    paras = template_text("first_touch.txt").strip("\n").split("\n\n")
    assert paras[0] == "Hi {first_name},"
    assert "{hook}" in paras[1]
    assert FOLLOW_UP_OPENER not in "".join(paras)


# ----- first touch body structure -----------------------------------------------------------------------

def test_first_touch_has_greeting_hook_product_ask_and_signature_in_order(kit):
    body, contact, sender = first_touch(kit)
    paras = paragraphs(body)
    assert len(paras) == 5
    assert paras[0] == "Hi Ada,"
    assert kit.HOOK in paras[1]
    assert sender.product_lines[0] in paras[2] and sender.traction_lines[0] in paras[2]
    assert paras[2].index(sender.product_lines[0]) < paras[2].index(sender.traction_lines[0])
    assert sender.ask_line in paras[3]
    assert paras[4].strip() == sender.signature.strip()


def test_greeting_uses_only_the_first_name(kit):
    body, _, _ = first_touch(kit, name="Ada Byron Lovelace")
    assert paragraphs(body)[0] == "Hi Ada,"
    assert "Lovelace" not in body


@pytest.mark.parametrize(
    "name,greeting",
    [("Dr. Jane Roe", "Hi Jane,"), ("Smith, Alice", "Hi Alice,"), ("ALICE SMITH", "Hi Alice,"),
     ("Prof. Alan Turing", "Hi Alan,"), ("mARY Jones", "Hi mARY,")],
)
def test_greeting_uses_the_cleaned_first_name(kit, name, greeting):
    body, _, _ = first_touch(kit, name=name)
    assert paragraphs(body)[0] == greeting


def test_greeting_prefers_the_preferred_name(kit):
    body, _, _ = first_touch(kit, name="Alexander Hamilton", preferred_name="Alex")
    assert paragraphs(body)[0] == "Hi Alex,"
    follow_body, _, _ = follow_up(kit, name="Grace Brewster Hopper", preferred_name="Amazing Grace")
    assert paragraphs(follow_body)[0] == "Hi Amazing Grace,"


def test_follow_up_greeting_uses_the_cleaned_first_name_too(kit):
    body, _, _ = follow_up(kit, name="Hopper, Grace")
    assert paragraphs(body)[0] == "Hi Grace,"
    assert paragraphs(body)[1].startswith("Following up in case you missed my previous email.")


def test_every_paragraph_before_the_signature_is_one_line(kit):
    long_hook = ("I came across your essay on how small teams ship weekly and the part about "
                 "keeping scope honest, " * 6).strip() + " and I found it interesting."
    body, _, _ = first_touch(kit, hook=long_hook)
    for para in paragraphs(body)[:-1]:
        assert "\n" not in para
    assert long_hook in body


def test_signature_keeps_its_own_line_breaks(kit):
    body, _, sender = first_touch(kit)
    assert paragraphs(body)[-1].count("\n") == sender.signature.count("\n") >= 2


def test_hook_paragraph_never_describes_the_product(kit):
    body, _, sender = first_touch(kit)
    assert sender.product_lines[0] not in paragraphs(body)[1]
    assert body.count(sender.product_lines[0]) == 1
    assert body.count(sender.traction_lines[0]) == 1


def test_only_the_first_product_and_traction_lines_are_used(kit):
    sender = kit.sender(
        product_lines=["First product line.", "Second product line."],
        traction_lines=["First traction line.", "Second traction line."],
    )
    body, _, _ = first_touch(kit, sender=sender)
    assert "First product line." in body and "First traction line." in body
    assert "Second product line." not in body and "Second traction line." not in body


def test_no_placeholder_is_left_unrendered(kit):
    for body in (first_touch(kit)[0], follow_up(kit)[0]):
        for placeholder in PLACEHOLDERS:
            assert placeholder not in body
        assert "None" not in body


def test_a_clean_render_passes_lint_without_errors(kit):
    body, contact, sender = first_touch(kit)
    subject = render_subject(contact, sender)
    issues = lint_draft(subject, body, sender, contact, kit.HOOK)
    assert [i for i in issues if i.severity == "error"] == []


def test_first_touch_never_contains_the_follow_up_opener(kit):
    assert FOLLOW_UP_OPENER not in first_touch(kit)[0]


# ----- missing sender lines -------------------------------------------------------------------------------

def assert_tidy(body):
    assert "\n\n\n" not in body
    assert not body.startswith("\n")
    for para in paragraphs(body):
        assert para.strip() != ""
    for para in paragraphs(body)[:-1]:
        assert para == para.strip(), f"stray whitespace around {para!r}"


def test_missing_traction_line_leaves_no_stray_blank_pair_or_trailing_space(kit):
    body, _, sender = first_touch(kit, sender=kit.sender(traction_lines=[]))
    assert_tidy(body)
    paras = paragraphs(body)
    assert len(paras) == 5
    assert paras[2] == sender.product_lines[0]


def test_missing_product_line_keeps_the_traction_line_tidy(kit):
    body, _, sender = first_touch(kit, sender=kit.sender(product_lines=[]))
    assert_tidy(body)
    assert len(paragraphs(body)) == 5
    assert paragraphs(body)[2] == sender.traction_lines[0]


def test_missing_product_and_traction_removes_that_paragraph_entirely(kit):
    body, _, sender = first_touch(kit, sender=kit.sender(product_lines=[], traction_lines=[]))
    assert_tidy(body)
    paras = paragraphs(body)
    assert len(paras) == 4
    assert [paras[0], paras[-2].strip(), paras[-1].strip()] == ["Hi Ada,", sender.ask_line, sender.signature]


def test_missing_ask_line_removes_the_ask_paragraph(kit):
    body, _, _ = first_touch(kit, sender=kit.sender(ask_line=""))
    assert_tidy(body)
    assert len(paragraphs(body)) == 4


def test_missing_signature_still_renders_without_blank_tail(kit):
    body, _, _ = first_touch(kit, sender=kit.sender(signature=""))
    assert not body.endswith("\n\n")
    assert "\n\n\n" not in body


def test_bare_sender_with_only_a_name_still_renders_a_greeting_and_hook(kit):
    from vcoutreach.types import SenderProfile

    body = render_body(kit.contact(), SenderProfile(name="Sam"), kit.HOOK)
    paras = paragraphs(body)
    assert paras[0] == "Hi Ada," and kit.HOOK in paras[1]
    assert_tidy(body)


# ----- follow up --------------------------------------------------------------------------------------------

def test_follow_up_has_greeting_then_the_exact_opener_then_the_same_rest(kit):
    ft_body, _, _ = first_touch(kit)
    fu_body, contact, _ = follow_up(kit, name="Grace Hopper")
    paras = paragraphs(fu_body)
    assert len(paras) == 5
    assert paras[0] == "Hi Grace,"
    assert paras[1] == f"{FOLLOW_UP_OPENER} {kit.HOOK}"
    assert paras[2:] == paragraphs(ft_body)[2:]


def test_follow_up_first_sentence_is_exact_even_with_an_awkward_hook(kit):
    hook = "I keep returning to your note on shipping."
    body, _, _ = follow_up(kit, hook=hook)
    assert paragraphs(body)[1] == f"Following up in case you missed my previous email. {hook}"


def test_follow_up_product_line_appears_once(kit):
    body, _, sender = follow_up(kit)
    assert body.count(sender.product_lines[0]) == 1


def test_follow_up_with_missing_traction_is_tidy(kit):
    body, _, _ = follow_up(kit, sender=kit.sender(traction_lines=[]))
    assert_tidy(body)
    assert len(paragraphs(body)) == 5


# ----- hostile text ---------------------------------------------------------------------------------------------

def test_braces_in_hook_and_sender_lines_are_rendered_verbatim(kit):
    sender = kit.sender(product_lines=["We build {smart} tools for {everyone}."])
    hook = "I liked your {signature} idea and the {ask_line} bit, and I found it interesting."
    body, _, _ = first_touch(kit, sender=sender, hook=hook)
    assert hook in body
    assert "We build {smart} tools for {everyone}." in body
    assert body.count(sender.signature) == 1
    assert body.count(sender.ask_line) == 1


def test_percent_signs_and_backslashes_survive(kit):
    hook = "I liked the 100% honest tone, even the C:\\temp joke, and I found it interesting."
    body, _, _ = first_touch(kit, hook=hook)
    assert hook in body


def test_empty_contact_name_does_not_produce_none_or_a_dangling_greeting(kit):
    body, _, _ = first_touch(kit, name="")
    assert "None" not in body
    assert_tidy(body)


# ----- subjects ----------------------------------------------------------------------------------------------------

def test_first_touch_subject_defaults_to_a_note_for_the_first_name(kit):
    subject = render_subject(kit.contact(), kit.sender())
    assert subject.lower() == "a note for ada"
    assert subject.startswith("a note for ")


def test_default_subject_uses_the_cleaned_first_name_or_the_preferred_name(kit):
    sender = kit.sender()
    assert render_subject(kit.contact(name="Dr. Jane Roe"), sender) == "a note for Jane"
    assert render_subject(kit.contact(name="Smith, Alice"), sender) == "a note for Alice"
    assert render_subject(kit.contact(name="Alexander Hamilton", preferred_name="Alex"), sender) == "a note for Alex"


def test_follow_up_subject_is_exactly_the_original_thread_subject(kit):
    for original in ("a note for Grace", "Re: quick intro", "Caf\u00e9 plans, take 2"):
        contact = kit.follow_up_contact(thread_subject=original)
        assert render_subject(contact, kit.sender()) == original


def test_subject_is_a_single_line(kit):
    assert "\n" not in render_subject(kit.contact(), kit.sender())


def test_subject_template_from_the_sender_file_is_used_for_first_touch(kit):
    path = kit.write_sender(subject_template="Idea for {org}, {first_name}")
    sender = load_sender(path)
    subject = render_subject(kit.contact(org="Northwind Labs"), sender)
    assert subject == "Idea for Northwind Labs, Ada"


def test_subject_template_does_not_override_a_follow_up_thread_subject(kit):
    path = kit.write_sender(subject_template="Idea for {org}, {first_name}")
    sender = load_sender(path)
    contact = kit.follow_up_contact(thread_subject="a note for Grace")
    assert render_subject(contact, sender) == "a note for Grace"


def test_render_functions_are_deterministic(kit):
    contact, sender = kit.contact(), kit.sender()
    assert render_body(contact, sender, kit.HOOK) == render_body(contact, sender, kit.HOOK)
    assert render_subject(contact, sender) == render_subject(contact, sender)


def test_lane_decides_the_template_not_the_text_of_the_hook(kit):
    contact = kit.contact(lane=Lane.FIRST_TOUCH)
    body = render_body(contact, kit.sender(), f"{FOLLOW_UP_OPENER} but not really.")
    assert paragraphs(body)[1].count(FOLLOW_UP_OPENER) == 1
    assert re.match(r"Hi Ada,\n\n", body)
