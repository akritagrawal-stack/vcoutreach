"""Round 3 draft content and grounding acceptance examples."""
import pytest

from web_helpers import ADA, export_rows, make_web, seed_contacts, web_env_isolation, read_draft_file


@pytest.mark.parametrize("claim,supported", [
    ("about 10M organic views", True), ("roughly 10M organic views", True),
    ("over 10M organic views", False), ("10M+ organic views", False),
    ("10M organic views", False), ("873 organic views", False),
])
def test_approximate_claim_cannot_be_strengthened_or_invented(kit, claim, supported):
    from vcoutreach.grounding import check_grounding
    contact = kit.contact(facts=("your essay about research tools",))
    sender = kit.sender(approved_claims={"reach": "about 10M organic views"})
    issues = check_grounding("we reached " + claim + ".", contact, sender)
    errors = [i for i in issues if i.code == "ungrounded_claim" and i.severity == "error"]
    assert bool(errors) is (not supported), issues


def test_hostile_greeting_name_uses_safe_fallback(make_web):
    w = make_web(sender=True)
    seed_contacts(w, [{**ADA, "name": '"click"),'}])
    r = w.post("/crm/drafts/write", {"provider": "template"}, follow=True)
    assert r.status_code == 200
    draft = read_draft_file(w.ws, "ada")
    assert draft.body.startswith("Hi there,"), draft.body


@pytest.mark.parametrize("phrase", ["I hope this email finds you well", "Hope this email finds you well",
                                    "I hope this message finds you well"])
def test_engine_lint_flags_canned_greeting_variants(kit, phrase):
    from vcoutreach.lint import lint_draft
    hook = kit.HOOK + " " + phrase + "."
    issues = lint_draft("a note for Ada", kit.clean_body(hook=hook), kit.sender(), kit.contact(), hook)
    assert any(i.code == "filler" and i.severity == "error" for i in issues), issues


@pytest.mark.parametrize("opener,repeated", [
    ("We reached 10M organic views through our own channels.",
     "Our videos have generated 10M organic views."),
    ("We make open source software easy to install.",
     "Our product helps teams deploy open-source alternatives to paid software."),
])
def test_replacing_opener_keeps_each_sender_claim_once_and_preserves_the_ask(opener, repeated):
    from vcoutreach.email_copy import replace_opener

    ask = "Would you be open to a short conversation next week?"
    signature = "Best,\nSam Rivera"
    body = "\n\n".join(["Hi Priya,", "I enjoyed your essay on research tools.",
                          repeated + " " + ask, signature])
    updated = replace_opener(body, opener, "Priya")
    assert updated.split("\n\n") == ["Hi Priya,", opener, ask, signature]


@pytest.mark.parametrize("claim,supported", [("10M organic views", True), ("10M+ organic views", False)])
def test_exact_approved_reach_does_not_authorize_a_larger_reach(kit, claim, supported):
    from vcoutreach.grounding import check_grounding

    sender = kit.sender(approved_claims={"reach": "10M organic views"})
    issues = check_grounding("we reached " + claim + ".", kit.contact(), sender)
    errors = [i for i in issues if i.code == "ungrounded_claim" and i.severity == "error"]
    assert bool(errors) is (not supported), issues
