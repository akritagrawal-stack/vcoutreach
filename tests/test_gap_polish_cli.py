"""SPEC 23.2 (why a draft was blocked, on stderr) and 23.6 (clearer errors and the kept-drafts line), through the real command line."""
from __future__ import annotations

import json
import re

import pytest

KEY = "fk_test_0123456789abcdefZQ4W"
BLAND_HOOK = "I read your recent work closely, and I respect the focus on clear writing."
NOUN_PHRASE = 'the offline template needs facts written as noun phrases (for example "your 2024 essay on X"), not sentences'
SEND_ENV = {"SEND_MODE": "test", "TEST_RECIPIENT_EMAIL": "me@mail.test", "SENDER_EMAIL": "sam@lumenlist.example.com"}
LIBRARIES_SENTENCE = (
    "Gmail sending needs optional Google libraries that are not installed. "
    "Run, from the repository folder: pip install -r requirements-gmail.txt "
    '(or pip install -e ".[gmail]")'
)
GOOD_FACT = "your essay on how small teams ship weekly"


def person(kit, ident, name="Ada Lovelace", fact=GOOD_FACT, **over):
    return kit.csv_row(id=ident, name=name, email=f"{ident.lower()}@example.com", org="Northwind", facts=fact, **over)


def draft_cmd(kit, rows, *extra, env=None, net_guard=True):
    contacts = kit.write_csv(rows, name="people.csv")
    sender = kit.write_sender()
    return kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, *extra, env=env, net_guard=net_guard)


def stripped(text):
    return [ln.strip() for ln in text.splitlines()]


def squashed(text):
    return re.sub(r"\s+", " ", text)


def index_rows(kit, out="drafts"):
    return kit.read_index(kit.workdir / out)


def first_message(row):
    """The first issue message as the console shows it (the console prints hyphens where a message holds a dash character)."""
    return row["issues"][0]["message"].replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def note_lines(res):
    return [ln for ln in stripped(res.err) if re.match(r"note: \S+ blocked: ", ln)]


# ----- 23.2 the note that explains each blocked draft -----------------------------------------------------------------------------------

def test_a_blocked_draft_gets_a_note_with_its_first_issue_message_on_stderr(kit):
    res = draft_cmd(kit, [person(kit, "ada"), person(kit, "zed", "Zed Zero", fact="")], "--offline")
    assert res.code == 0, res.combined
    rows = {r["contact_id"]: r for r in index_rows(kit)}
    message = first_message(rows["zed"])
    assert message.strip(), "a blocked draft carries a message"
    assert f"note: zed blocked: {message}" in stripped(res.err)


def test_the_notes_never_appear_on_stdout_and_the_stdout_blocked_lines_are_unchanged(kit):
    res = draft_cmd(kit, [person(kit, "ada"), person(kit, "zed", "Zed Zero", fact=""), person(kit, "amy", "Amy Zip", fact="")], "--offline")
    assert res.code == 0, res.combined
    assert [ln for ln in stripped(res.out) if ln.startswith("blocked ")] == ["blocked zed: insufficient_facts", "blocked amy: insufficient_facts"]
    assert not [ln for ln in stripped(res.out) if "note:" in ln or " blocked: " in ln]
    assert not [ln for ln in stripped(res.err) if ln.startswith("blocked ")]


def test_every_blocked_draft_gets_one_note_in_file_order_and_ok_drafts_get_none(kit):
    rows = [
        person(kit, "ada"),
        person(kit, "Zed-Zero", "Zed Zero", fact=""),
        person(kit, "bob", "Bob Byte", fact=f"your talk {kit.EM} and your essay!"),
        person(kit, "cyd", "Cyd Cole"),
        person(kit, "amy", "Amy Zip", fact="You've been writing about queues"),
    ]
    res = draft_cmd(kit, rows, "--offline")
    assert res.code == 0, res.combined
    by_id = {r["contact_id"]: r for r in index_rows(kit)}
    assert [i for i, r in by_id.items() if r["status"] == "blocked"] == ["Zed-Zero", "bob", "amy"]
    notes = note_lines(res)
    assert [n.split(" ")[1] for n in notes] == ["Zed-Zero", "bob", "amy"]
    for ident in ("Zed-Zero", "bob", "amy"):
        message = first_message(by_id[ident])
        assert message.strip()
        assert f"note: {ident} blocked: {message}" in stripped(res.err), ident
    assert not [n for n in notes if n.split(" ")[1] in ("ada", "cyd")]


def test_the_note_for_a_sentence_style_fact_carries_the_noun_phrase_message(kit):
    res = draft_cmd(kit, [person(kit, "amy", "Amy Zip", fact="You've been writing about queues")], "--offline")
    assert res.code == 0, res.combined
    (line,) = note_lines(res)
    assert line.startswith("note: amy blocked: ") and NOUN_PHRASE in line, line


def test_a_draft_blocked_for_several_reasons_gets_a_single_note_not_one_per_reason(kit):
    res = draft_cmd(kit, [person(kit, "bob", "Bob Byte", fact=f"your talk {kit.EM} and your essay!")], "--offline")
    (row,) = index_rows(kit)
    assert len(row["issues"]) >= 2
    (line,) = note_lines(res)
    assert line == f"note: bob blocked: {first_message(row)}"


def test_no_blocked_note_when_nothing_is_blocked(kit):
    res = draft_cmd(kit, [person(kit, "ada"), person(kit, "bob", "Bob Byte")], "--offline")
    assert res.code == 0, res.combined
    assert note_lines(res) == []
    assert " blocked: " not in res.combined


def test_a_warning_only_draft_gets_no_blocked_note_but_keeps_its_warnings_note(kit):
    res = draft_cmd(kit, [person(kit, "team", "The Smith Team"), person(kit, "zed", "Zed Zero", fact="")], "--offline")
    assert res.code == 0, res.combined
    assert [n.split(" ")[1] for n in note_lines(res)] == ["zed"]
    assert any(ln.startswith("note: 1 draft(s) have warnings (odd_greeting)") for ln in stripped(res.err)), res.err


def test_strict_still_exits_2_and_still_explains_each_blocked_draft(kit):
    res = draft_cmd(kit, [person(kit, "ada"), person(kit, "zed", "Zed Zero", fact="")], "--offline", "--strict")
    assert res.code == 2, res.combined
    (line,) = note_lines(res)
    assert line.startswith("note: zed blocked: ")
    assert "blocked zed: insufficient_facts" in stripped(res.out + res.err)


def test_the_quickstart_explains_its_one_blocked_sample_row(kit):
    res = kit.quickstart()
    assert res.code == 0, res.combined
    (line,) = note_lines(res)
    blocked = [r for r in index_rows(kit) if r["status"] == "blocked"]
    assert len(blocked) == 1
    assert line == f"note: {blocked[0]['contact_id']} blocked: {first_message(blocked[0])}"


# ----- 23.6 (a) the missing Google libraries message ---------------------------------------------------------------------------------------

def missing_libraries(kit, command):
    kit.quickstart()
    if command == "send":
        return kit.cli("send", "--drafts", "drafts", "--execute", env=SEND_ENV, no_google=True)
    return kit.cli("auth", "gmail", no_google=True)


@pytest.mark.parametrize("command", ["send", "auth"])
def test_the_missing_libraries_message_has_the_exact_wording_and_exits_3(kit, command):
    res = missing_libraries(kit, command)
    assert res.code == 3, res.combined
    assert LIBRARIES_SENTENCE in squashed(res.combined), res.combined
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


@pytest.mark.parametrize("command", ["send", "auth"])
def test_the_missing_libraries_message_never_points_at_a_package_index(kit, command):
    text = squashed(missing_libraries(kit, command).combined)
    assert 'pip install "vcoutreach[gmail]"' not in text
    assert "pip install vcoutreach" not in text
    assert "vcoutreach[gmail]" not in text
    assert not re.search(r"pip install\s+[\"']?vcoutreach", text)


@pytest.mark.parametrize("command", ["send", "auth"])
def test_the_missing_libraries_message_names_both_ways_to_install_and_the_repository_folder(kit, command):
    text = squashed(missing_libraries(kit, command).combined)
    assert "pip install -r requirements-gmail.txt" in text
    assert 'pip install -e ".[gmail]"' in text
    assert "from the repository folder" in text


def test_the_library_level_message_never_points_at_a_package_index_either(kit):
    from email.message import EmailMessage

    from vcoutreach.send.gmail import GmailTransport, authorize
    from vcoutreach.types import OutreachError

    kit.hide_google()
    settings = kit.send_settings("live")
    for call in (lambda: GmailTransport(settings).send(EmailMessage()), lambda: authorize(settings)):
        with pytest.raises(OutreachError) as info:
            call()
        text = squashed(str(info.value))
        assert "pip install -r requirements-gmail.txt" in text
        assert "vcoutreach[gmail]" not in text and not re.search(r"pip install\s+[\"']?vcoutreach", text)


def test_a_plan_still_never_needs_the_google_libraries(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV, no_google=True)
    assert res.code == 0, res.combined
    assert "requirements-gmail.txt" not in res.combined


# ----- 23.6 (b) send --drafts <file> ----------------------------------------------------------------------------------------------------

def drafts_error_lines(res):
    return [ln for ln in stripped(res.combined) if "--drafts expects a directory" in ln]


@pytest.mark.parametrize("target", ["drafts/index.jsonl", "contacts.sample.csv", "FIRST_DRAFT"], ids=["jsonl", "csv", "a-draft-file"])
def test_send_given_a_file_says_it_expects_a_directory_and_exits_2(kit, target):
    kit.quickstart()
    if target == "FIRST_DRAFT":
        target = "drafts/" + sorted(p.name for p in (kit.workdir / "drafts").glob("*.md"))[0]
    res = kit.cli("send", "--drafts", target, env=SEND_ENV)
    assert res.code == 2, res.combined
    (line,) = drafts_error_lines(res)
    assert re.fullmatch(r"(?:vcoutreach: )?--drafts expects a directory, not a file: \S*" + re.escape(target.split("/")[-1]), line), line
    assert "Traceback" not in res.combined
    assert "no drafts found" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


def test_send_and_lint_say_the_same_thing_about_a_file(kit):
    kit.quickstart()
    send = kit.cli("send", "--drafts", "drafts/index.jsonl", env=SEND_ENV)
    lint = kit.cli("lint", "--drafts", "drafts/index.jsonl")
    assert send.code == 2 and lint.code == 2, (send.combined, lint.combined)
    (send_line,) = drafts_error_lines(send)
    (lint_line,) = drafts_error_lines(lint)
    assert send_line == lint_line
    assert re.fullmatch(r"(?:vcoutreach: )?--drafts expects a directory, not a file: \S*index\.jsonl", lint_line), lint_line


def test_send_given_a_file_with_a_limit_still_says_so(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts/index.jsonl", "--limit", "1", env=SEND_ENV)
    assert res.code == 2, res.combined
    assert len(drafts_error_lines(res)) == 1


def test_send_given_a_real_directory_is_unaffected(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "expects a directory" not in res.combined


# ----- 23.6 (c) init --dir <file> ---------------------------------------------------------------------------------------------------------

def test_init_dir_pointing_at_a_file_says_it_is_a_file_not_a_folder(kit):
    kit.write_raw("afile", "i am a file")
    res = kit.cli("init", "--dir", "afile")
    assert res.code == 2, res.combined
    lines = [ln for ln in stripped(res.combined) if ln.startswith("cannot use --dir") or ln.startswith("vcoutreach: cannot use --dir")]
    assert len(lines) == 1, res.combined
    assert re.fullmatch(r"(?:vcoutreach: )?cannot use --dir \S*afile: it is a file, not a folder", lines[0]), lines[0]
    assert "Traceback" not in res.combined
    assert (kit.workdir / "afile").read_text(encoding="utf-8") == "i am a file"
    assert not (kit.workdir / "contacts.sample.csv").exists()


def test_init_dir_given_an_absolute_path_to_a_file_names_that_path(kit):
    target = kit.write_raw("somefile.txt", "x")
    res = kit.cli("init", "--dir", str(target))
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?cannot use --dir \S*somefile\.txt: it is a file, not a folder$", res.combined, re.M), res.combined


def test_init_dir_that_cannot_be_created_below_a_file_still_exits_2_with_a_reason(kit):
    kit.write_raw("afile", "x")
    res = kit.cli("init", "--dir", "afile/sub")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?cannot use --dir \S*afile/sub: \S.*$", res.combined, re.M), res.combined


def test_init_dir_pointing_at_a_folder_still_works(kit):
    (kit.workdir / "fresh").mkdir()
    res = kit.cli("init", "--dir", "fresh")
    assert res.code == 0, res.combined
    assert (kit.workdir / "fresh" / "contacts.sample.csv").is_file()
    assert "cannot use --dir" not in res.combined


# ----- 23.6 (d) the finished drafts that stay on disk when a provider aborts the run ----------------------------------------------------------

def abort_env(gateway):
    return {"PUBLIK_API_KEY": KEY, "PUBLIK_API_BASE_URL": gateway.base_url, "PUBLIK_MAX_RETRIES": "0"}


def three_rows(kit):
    return [person(kit, "ada", "Ada Lovelace"), person(kit, "bob", "Bob Byte"), person(kit, "cyd", "Cyd Cole")]


def answer_then_run_out_of_credit(gateway, answers):
    for _ in range(answers):
        gateway.reply_chat(BLAND_HOOK)
    gateway.reply(402, json_body={"error": {"message": "Your balance is empty"}})


@pytest.mark.local_http
@pytest.mark.parametrize("answers", [1, 2])
def test_an_abort_after_finished_drafts_says_how_many_were_kept_before_the_error_line(kit, gateway, answers):
    answer_then_run_out_of_credit(gateway, answers)
    res = draft_cmd(kit, three_rows(kit), "--provider", "publik", env=abort_env(gateway), net_guard=False)
    assert res.code == 3, res.combined
    lines = stripped(res.err)
    kept = f"kept {answers} finished draft(s) in drafts/"
    assert lines.count(kept) == 1, res.err
    error_at = next(i for i, ln in enumerate(lines) if "Your balance is empty" in ln)
    assert lines.index(kept) < error_at, "the kept line comes before the error line"
    assert "kept " not in res.out
    assert "Traceback" not in res.combined and KEY not in res.combined
    assert len(gateway.requests) == answers + 1


@pytest.mark.local_http
def test_the_kept_line_is_true_the_finished_drafts_really_are_on_disk(kit, gateway):
    answer_then_run_out_of_credit(gateway, 2)
    res = draft_cmd(kit, three_rows(kit), "--provider", "publik", env=abort_env(gateway), net_guard=False)
    assert res.code == 3, res.combined
    assert "kept 2 finished draft(s) in drafts/" in stripped(res.err)
    assert sorted(p.name for p in (kit.workdir / "drafts").glob("*.md")) == ["ada.md", "bob.md"]
    assert [r["contact_id"] for r in index_rows(kit)] == ["ada", "bob"]


@pytest.mark.local_http
def test_the_kept_line_names_the_out_folder_that_was_asked_for(kit, gateway):
    answer_then_run_out_of_credit(gateway, 1)
    res = draft_cmd(kit, three_rows(kit), "--provider", "publik", "--out", "mail/round-one", env=abort_env(gateway), net_guard=False)
    assert res.code == 3, res.combined
    assert "kept 1 finished draft(s) in mail/round-one/" in stripped(res.err), res.err
    assert (kit.workdir / "mail" / "round-one" / "ada.md").exists()


@pytest.mark.local_http
def test_a_trailing_slash_on_out_is_not_doubled(kit, gateway):
    answer_then_run_out_of_credit(gateway, 1)
    res = draft_cmd(kit, three_rows(kit), "--provider", "publik", "--out", "mail/", env=abort_env(gateway), net_guard=False)
    assert res.code == 3, res.combined
    assert "kept 1 finished draft(s) in mail/" in stripped(res.err), res.err
    assert "mail//" not in res.err


@pytest.mark.local_http
def test_an_abort_before_any_draft_was_finished_prints_no_kept_line(kit, gateway):
    gateway.reply(402, json_body={"error": {"message": "Your balance is empty"}})
    res = draft_cmd(kit, three_rows(kit), "--provider", "publik", env=abort_env(gateway), net_guard=False)
    assert res.code == 3, res.combined
    assert "kept " not in res.combined
    assert "Your balance is empty" in res.combined
    assert len(gateway.requests) == 1


def test_an_abort_with_an_unreachable_provider_before_the_first_draft_prints_no_kept_line(kit):
    env = {"PUBLIK_API_KEY": KEY, "PUBLIK_API_BASE_URL": "http://127.0.0.1:9/api/v1", "PUBLIK_MAX_RETRIES": "0"}
    res = draft_cmd(kit, three_rows(kit), "--provider", "publik", env=env)
    assert res.code == 3, res.combined
    assert "kept " not in res.combined


@pytest.mark.local_http
def test_a_complete_run_prints_no_kept_line(kit, gateway):
    for _ in range(3):
        gateway.reply_chat(BLAND_HOOK)
    res = draft_cmd(kit, three_rows(kit), "--provider", "publik", env=abort_env(gateway), net_guard=False)
    assert res.code == 0, res.combined
    assert "kept " not in res.combined
    assert json.loads((kit.workdir / "drafts" / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])["contact_id"] == "ada"
