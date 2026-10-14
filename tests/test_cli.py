"""The command line (SPEC section 13 and the quickstart of section 14), run as real subprocesses.

Each subprocess gets a scrubbed environment (no keys) and a sitecustomize that records and
refuses every outbound socket, so "offline means offline" is proven, not assumed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

KEY = "fk_test_0123456789abcdefZQ4W"
SUMMARY = re.compile(r"^drafted=(\d+) blocked=(\d+) provider=([a-z]+)$")
BLAND_HOOK = "I read your recent work closely, and I respect the focus on clear writing."
NO_KEY_WARNING = (
    "warning: no provider key found (set PUBLIK_API_KEY, OPENAI_API_KEY or ANTHROPIC_API_KEY); "
    "using the offline template provider, which writes placeholder hooks."
)


def summary_of(res):
    for line in (res.out + "\n" + res.err).splitlines():
        m = SUMMARY.match(line.strip())
        if m:
            return line.strip(), int(m.group(1)), int(m.group(2)), m.group(3)
    return None


def nonblank(text):
    return [ln for ln in text.splitlines() if ln.strip()]


def drafted_files(workdir, sub="drafts"):
    return sorted(p.name for p in (workdir / sub).glob("*.md"))


# ----- meta ---------------------------------------------------------------------------------------------------------------

def test_version_flag_prints_the_package_version(kit):
    res = kit.cli("--version")
    assert res.code == 0
    assert "0.1.0" in res.combined


def test_help_lists_every_subcommand(kit):
    res = kit.cli("--help")
    assert res.code == 0
    for name in ("init", "draft", "lint", "providers", "send", "auth"):
        assert name in res.out


def test_running_with_no_arguments_never_shows_a_traceback(kit):
    res = kit.cli()
    assert "Traceback" not in res.combined
    assert res.code in (0, 1, 2)


def test_unknown_subcommand_is_a_usage_error(kit):
    res = kit.cli("frobnicate")
    assert res.code == 2 and "Traceback" not in res.combined


# ----- init ------------------------------------------------------------------------------------------------------------------

def test_init_copies_the_three_starter_files_into_the_current_directory(kit):
    res = kit.cli("init")
    assert res.code == 0, res.combined
    for name in ("contacts.sample.csv", "sender.sample.json", ".env.example"):
        assert (kit.workdir / name).is_file(), name
    from vcoutreach.contacts import load_contacts
    from vcoutreach.sender import load_sender

    assert len(load_contacts(kit.workdir / "contacts.sample.csv")) == 5
    assert load_sender(kit.workdir / "sender.sample.json").name == "Sam Rivera"


def test_init_never_overwrites_an_existing_file(kit):
    mine = kit.write_raw("contacts.sample.csv", "id,name,email\nmine,My Own,mine@example.com\n")
    res = kit.cli("init")
    assert "Traceback" not in res.combined
    assert mine.read_text(encoding="utf-8") == "id,name,email\nmine,My Own,mine@example.com\n"
    assert "contacts.sample.csv" in res.combined, "the user should be told which file was left alone"


def test_running_init_twice_changes_nothing(kit):
    kit.cli("init")
    before = {p.name: p.read_bytes() for p in kit.workdir.iterdir() if p.is_file()}
    second = kit.cli("init")
    after = {p.name: p.read_bytes() for p in kit.workdir.iterdir() if p.is_file()}
    assert before == after
    assert "Traceback" not in second.combined


def test_init_into_a_named_directory(kit):
    target = kit.workdir / "starter"
    target.mkdir()
    res = kit.cli("init", "--dir", str(target))
    assert res.code == 0, res.combined
    assert sorted(p.name for p in target.iterdir()) == [".env.example", "contacts.sample.csv", "sender.sample.json"]
    assert not (kit.workdir / "contacts.sample.csv").exists()


def test_the_copied_env_example_is_safe_to_use_as_is(kit):
    kit.cli("init")
    (kit.workdir / ".env").write_bytes((kit.workdir / ".env.example").read_bytes())
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json")
    assert res.code == 0, res.combined
    assert summary_of(res)[3] == "template", "an untouched .env must not enable any paid provider"
    assert res.net_attempts == ""


# ----- the quickstart ----------------------------------------------------------------------------------------------------------

def test_quickstart_offline_prints_the_exact_summary_line(kit):
    res = kit.quickstart()
    assert res.code == 0, res.combined
    assert "drafted=4 blocked=1 provider=template" in [ln.strip() for ln in (res.out + res.err).splitlines()]


def test_quickstart_writes_drafts_blocked_drafts_and_the_index(kit):
    kit.quickstart()
    assert len(drafted_files(kit.workdir)) == 4
    assert len(drafted_files(kit.workdir, "drafts/blocked")) == 1
    rows = kit.read_index(kit.workdir / "drafts")
    assert len(rows) == 5
    assert sorted(r["status"] for r in rows) == ["blocked", "ok", "ok", "ok", "ok"]
    (blocked,) = [r for r in rows if r["status"] == "blocked"]
    assert any(i["code"] == "insufficient_facts" for i in blocked["issues"])
    assert all(r["hook_provider"] == "template" for r in rows if r["status"] == "ok")


def test_quickstart_makes_no_network_attempts(kit):
    res = kit.quickstart()
    assert res.net_attempts == ""


def test_offline_ignores_provider_keys_in_the_environment(kit):
    env = {"PUBLIK_API_KEY": KEY, "OPENAI_API_KEY": KEY + "o", "ANTHROPIC_API_KEY": KEY + "a"}
    res = kit.quickstart(extra_env=env)
    assert res.code == 0, res.combined
    assert summary_of(res)[3] == "template"
    assert res.net_attempts == ""
    assert KEY not in res.combined


def test_offline_ignores_a_key_in_the_dotenv_file(kit):
    kit.cli("init")
    (kit.workdir / ".env").write_text(f"PUBLIK_API_KEY={KEY}\n", encoding="utf-8")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--offline")
    assert res.code == 0 and summary_of(res)[3] == "template"
    assert res.net_attempts == ""


def test_auto_provider_without_any_key_falls_back_to_the_template(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json")
    assert res.code == 0, res.combined
    assert summary_of(res)[1:] == (4, 1, "template")
    assert res.net_attempts == ""


def test_explicit_template_provider_and_custom_output_directory(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json",
                  "--provider", "template", "--out", "my-drafts")
    assert res.code == 0, res.combined
    assert len(drafted_files(kit.workdir, "my-drafts")) == 4
    assert (kit.workdir / "my-drafts" / "index.jsonl").exists()
    assert not (kit.workdir / "drafts").exists()


def test_rerunning_the_quickstart_gives_the_same_summary_and_no_duplicate_rows(kit):
    first = kit.quickstart()
    second = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--offline")
    assert summary_of(first)[0] == summary_of(second)[0]
    assert len(kit.read_index(kit.workdir / "drafts")) == 5


def test_an_excel_style_contacts_file_works_end_to_end(kit):
    rows = [
        kit.csv_row(id="ada", name="Ada Lovelace", email="ada@example.com", org="Northwind, Labs",
                    facts="your essay on how small teams ship weekly | your newsletter, read by many"),
        kit.csv_row(name="Grace Hopper", email="grace@example.org", facts="your talk on making software readable"),
    ]
    contacts = kit.write_csv(rows, bom=True, crlf=True)
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 0, res.combined
    assert summary_of(res)[1:] == (2, 0, "template")
    assert len(kit.read_index(kit.workdir / "drafts")) == 2


# ----- exit codes: contact and sender problems --------------------------------------------------------------------------------

def test_bad_email_in_the_contacts_file_exits_2_and_names_the_row_without_a_traceback(kit):
    letters = "abcde"
    rows = [kit.csv_row(id=f"c{ch}", name=f"Person {ch.upper()}", email=f"person{ch}@example.com", facts="a fact") for ch in letters]
    rows[3]["email"] = "not-an-email"
    contacts = kit.write_csv(rows)
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2, res.combined
    assert "Traceback" not in res.combined
    kit.assert_names_rows(res.err or res.out, [4], 5, contacts)
    assert not (kit.workdir / "drafts").exists() or list((kit.workdir / "drafts").glob("*.md")) == []


def test_missing_contacts_file_exits_2(kit):
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", "nope.csv", "--sender", sender.name, "--offline")
    assert res.code == 2 and "Traceback" not in res.combined


def test_sender_file_without_a_name_exits_2(kit):
    contacts = kit.write_csv([kit.csv_row(id="a", name="A B", email="a@example.com", facts="x")])
    sender = kit.write_raw("sender.json", json.dumps({"title": "Founder"}))
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2 and "Traceback" not in res.combined


def test_broken_sender_json_exits_2(kit):
    contacts = kit.write_csv([kit.csv_row(id="a", name="A B", email="a@example.com", facts="x")])
    sender = kit.write_raw("sender.json", '{"name": "Sam",')
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2 and "Traceback" not in res.combined


def test_missing_required_arguments_are_a_usage_error(kit):
    res = kit.cli("draft", "--offline")
    assert res.code == 2 and "Traceback" not in res.combined


def test_strict_turns_blocked_drafts_into_exit_code_2(kit):
    kit.cli("init")
    base = ["draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--offline"]
    assert kit.cli(*base).code == 0
    strict = kit.cli(*base, "--strict")
    assert strict.code == 2
    assert "Traceback" not in strict.combined


def test_strict_with_nothing_blocked_still_exits_0(kit):
    contacts = kit.write_csv([kit.csv_row(id="a", name="A B", email="a@example.com", facts="your essay on small teams")])
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline", "--strict")
    assert res.code == 0, res.combined
    assert summary_of(res)[1:] == (1, 0, "template")


# ----- exit code 3: provider problems ---------------------------------------------------------------------------------------------

def test_publik_provider_without_a_key_exits_3_with_one_clear_line(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--provider", "publik")
    assert res.code == 3, res.combined
    assert "Traceback" not in res.combined
    message = nonblank(res.err) or nonblank(res.out)
    assert len(message) == 1, message
    assert "PUBLIK_API_KEY" in message[0]
    assert "https://publikhq.com/dashboard/api" in message[0]
    assert "--provider template" in message[0]
    assert res.net_attempts == ""
    drafts_dir = kit.workdir / "drafts"
    assert not drafts_dir.exists() or list(drafts_dir.glob("*.md")) == []


def test_debug_environment_variable_restores_the_traceback(kit):
    kit.cli("init")
    args = ["draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--provider", "publik"]
    quiet = kit.cli(*args)
    loud = kit.cli(*args, env={"VCOUTREACH_DEBUG": "1"})
    assert "Traceback" not in quiet.combined
    assert "Traceback" in loud.combined
    assert loud.code != 0


def test_unreachable_provider_exits_3_with_one_line_and_never_prints_the_key(kit):
    kit.cli("init")
    env = {"PUBLIK_API_KEY": KEY, "PUBLIK_API_BASE_URL": "http://127.0.0.1:9/api/v1", "PUBLIK_MAX_RETRIES": "0"}
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", env=env)
    assert res.code == 3, res.combined
    assert "Traceback" not in res.combined and KEY not in res.combined
    assert len(nonblank(res.err) or nonblank(res.out)) == 1


@pytest.mark.local_http
def test_revoked_key_against_a_real_gateway_exits_3_without_leaking_the_key(kit, gateway):
    kit.cli("init")
    gateway.reply(401, json_body={"error": {"message": f"Invalid API key {KEY}"}})
    env = {"PUBLIK_API_KEY": KEY, "PUBLIK_API_BASE_URL": gateway.base_url, "PUBLIK_MAX_RETRIES": "0"}
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json",
                  "--provider", "publik", env=env, net_guard=False)
    assert res.code == 3, res.combined
    assert KEY not in res.combined and "Traceback" not in res.combined
    assert len(gateway.requests) == 1, "an auth failure must stop the run at the first row"


@pytest.mark.local_http
def test_empty_balance_against_a_real_gateway_exits_3_and_says_so(kit, gateway):
    kit.cli("init")
    gateway.reply(402, json_body={"error": {"message": "Your balance is empty", "top_up_url": "https://publikhq.com/top-up"}})
    env = {"PUBLIK_API_KEY": KEY, "PUBLIK_API_BASE_URL": gateway.base_url, "PUBLIK_MAX_RETRIES": "0"}
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", env=env, net_guard=False)
    assert res.code == 3, res.combined
    assert "Your balance is empty" in res.combined
    assert "Traceback" not in res.combined
    assert len(gateway.requests) == 1


@pytest.mark.local_http
def test_full_run_against_a_real_gateway_uses_the_key_once_per_row_with_facts(kit, gateway):
    kit.cli("init")
    for _ in range(4):
        gateway.reply_chat(BLAND_HOOK, headers={"x-publik-model": "publik-balanced", "x-publik-balance": "1000"})
    env = {"PUBLIK_API_KEY": KEY, "PUBLIK_API_BASE_URL": gateway.base_url}
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", env=env, net_guard=False)
    assert res.code == 0, res.combined
    assert summary_of(res)[1:] == (4, 1, "publik")
    assert NO_KEY_WARNING not in res.err and "no provider key" not in res.combined
    assert len(gateway.requests) == 4, "the row without facts must not cost a call"
    assert all(r.header("authorization") == f"Bearer {KEY}" for r in gateway.requests)
    assert KEY not in res.combined
    for path in (kit.workdir / "drafts").rglob("*"):
        if path.is_file():
            assert KEY not in path.read_text(encoding="utf-8"), path
    assert all(r["hook_provider"] == "publik" for r in kit.read_index(kit.workdir / "drafts") if r["status"] == "ok")


# ----- lint ---------------------------------------------------------------------------------------------------------------------------

def edit_hook_paragraph(path: Path, how: str) -> str:
    original = path.read_text(encoding="utf-8")
    head, sep, body = original.partition("\n\n")
    paras = body.split("\n\n")
    p = paras[1]
    p = {
        "em-dash": p + " \u2014 indeed",
        "en-dash": p + " \u2013 indeed",
        "exclamation": p + "!",
        "filler": p + " Just circling back.",
        "hard-wrap": p.replace(" ", "\n", 1),
    }[how]
    paras[1] = p
    path.write_text(head + sep + "\n\n".join(paras), encoding="utf-8")
    return original


def test_lint_passes_on_untouched_drafts(kit):
    kit.quickstart()
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 0, res.combined
    assert "Traceback" not in res.combined


@pytest.mark.parametrize("how", ["em-dash", "en-dash", "exclamation", "filler", "hard-wrap"])
def test_lint_exits_1_when_a_draft_was_hand_broken_and_0_again_once_fixed(kit, how):
    kit.quickstart()
    target = sorted((kit.workdir / "drafts").glob("*.md"))[0]
    original = edit_hook_paragraph(target, how)
    broken = kit.cli("lint", "--drafts", "drafts")
    assert broken.code == 1, broken.combined
    assert target.stem in broken.combined, "the report should name the offending draft"
    assert "Traceback" not in broken.combined
    target.write_text(original, encoding="utf-8")
    assert kit.cli("lint", "--drafts", "drafts").code == 0


def test_lint_only_fails_for_the_broken_draft_not_its_neighbours(kit):
    kit.quickstart()
    files = sorted((kit.workdir / "drafts").glob("*.md"))
    edit_hook_paragraph(files[-1], "em-dash")
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 1
    assert files[-1].stem in res.combined


# ----- providers -------------------------------------------------------------------------------------------------------------------------

def line_for(text, name):
    for line in text.splitlines():
        if name in line.lower():
            return line
    return None


def test_providers_lists_every_provider_and_marks_the_configured_ones(kit):
    bare = kit.cli("providers")
    assert bare.code == 0, bare.combined
    for name in ("template", "publik", "openai", "anthropic"):
        assert line_for(bare.out, name), f"{name} is missing from the listing"
    configured = kit.cli("providers", env={"PUBLIK_API_KEY": KEY})
    assert line_for(configured.out, "publik") != line_for(bare.out, "publik")
    assert line_for(configured.out, "openai") == line_for(bare.out, "openai")


def test_providers_never_prints_a_key_from_the_environment_or_from_dotenv(kit):
    keys = {"PUBLIK_API_KEY": "pk_fake_aaaaaaaaaaaaaaaaaaaaZ111", "OPENAI_API_KEY": "ok_fake_bbbbbbbbbbbbbbbbbbbbZ222",
            "ANTHROPIC_API_KEY": "ak_fake_ccccccccccccccccccccZ333"}
    res = kit.cli("providers", env=keys)
    (kit.workdir / ".env").write_text("PUBLIK_API_KEY=pk_fake_dotenvdotenvdotenvZ444\n", encoding="utf-8")
    res2 = kit.cli("providers")
    for value in list(keys.values()) + ["pk_fake_dotenvdotenvdotenvZ444"]:
        assert value not in res.combined + res2.combined
        assert value[-4:] not in res.combined + res2.combined
        assert value[:8] not in res.combined + res2.combined


def test_providers_does_not_echo_credentials_embedded_in_a_base_url(kit):
    res = kit.cli("providers", env={
        "OPENAI_API_KEY": "ok_fake_bbbbbbbbbbbbbbbbbbbbZ222",
        "OPENAI_BASE_URL": "https://someuser:hunter2secret@gateway.example.test/v1",
    })
    assert "hunter2secret" not in res.combined


def test_providers_never_touches_the_network(kit):
    res = kit.cli("providers", env={"PUBLIK_API_KEY": KEY})
    assert res.net_attempts == ""


# ----- send (plan only; nothing here can reach a mail server) -------------------------------------------------------------------------

SEND_ENV = {"SEND_MODE": "test", "TEST_RECIPIENT_EMAIL": "me@mail.test", "SENDER_EMAIL": "sam@lumenlist.example.com"}


def test_send_without_execute_prints_a_plan_and_sends_nothing(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "me@mail.test" in res.out, "the plan must show where messages would really go"
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


def test_send_plan_does_not_include_the_blocked_draft(kit):
    kit.quickstart()
    blocked = json.loads(next(ln for ln in (kit.workdir / "drafts" / "index.jsonl").read_text(encoding="utf-8").splitlines()
                              if json.loads(ln)["status"] == "blocked"))
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert blocked["to"] not in res.out


# ----- send without --sender (SPEC 18.4) ---------------------------------------------------------------------------------------------------

NO_SENDER_NOTE = "note: no --sender given; sender forbidden phrases are not checked at send time"


def test_send_works_without_a_sender_file(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "me@mail.test" in res.out
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""


def test_send_without_a_sender_file_prints_the_note_exactly_once_on_stderr(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert [ln.strip() for ln in res.err.splitlines()].count(NO_SENDER_NOTE) == 1
    assert NO_SENDER_NOTE not in res.out, "the note belongs on stderr, not in the plan on stdout"


def test_the_plan_lists_the_same_messages_with_and_without_a_sender_file(kit):
    kit.quickstart()
    with_sender = kit.cli("send", "--drafts", "drafts", "--sender", "sender.sample.json", env=SEND_ENV)
    without = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert with_sender.code == 0 and without.code == 0, with_sender.combined + without.combined
    assert without.out.count("me@mail.test") >= 1
    assert without.out.count("me@mail.test") == with_sender.out.count("me@mail.test")
    assert "note:" not in without.out


def test_send_with_a_sender_file_prints_no_note(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--sender", "sender.sample.json", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert NO_SENDER_NOTE not in res.combined
    assert "no --sender given" not in res.combined


def test_send_help_documents_the_optional_sender_flag(kit):
    res = kit.cli("send", "--help")
    assert res.code == 0, res.combined
    assert "--sender" in res.out


def test_send_with_a_missing_sender_file_exits_2_without_a_traceback(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--sender", "no-such-sender.json", env=SEND_ENV)
    assert res.code == 2, res.combined
    assert "Traceback" not in res.combined
    assert NO_SENDER_NOTE not in res.combined, "a sender was given, so there is nothing to warn about"
    assert res.net_attempts == ""


def test_send_with_a_broken_sender_file_exits_2_without_a_traceback(kit):
    kit.quickstart()
    broken = kit.write_raw("broken-sender.json", '{"name": "Sam",')
    res = kit.cli("send", "--drafts", "drafts", "--sender", broken.name, env=SEND_ENV)
    assert res.code == 2, res.combined
    assert "Traceback" not in res.combined


def test_a_second_send_plan_without_a_sender_prints_the_note_again(kit):
    kit.quickstart()
    first = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    second = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert first.err.count(NO_SENDER_NOTE) == 1 and second.err.count(NO_SENDER_NOTE) == 1
    assert first.out == second.out, "a plan-only run leaves no state, so the plan is repeatable"


@pytest.mark.parametrize("no_google", [False, True], ids=["google-as-installed", "google-missing"])
@pytest.mark.parametrize(
    "args,env",
    [
        (["--live"], {"SEND_MODE": "live"}),
        (["--live", "--execute"], {"SEND_MODE": "live"}),
        (["--live", "--execute", "--confirm-live", "nope"], {"SEND_MODE": "live"}),
        (["--live", "--execute", "--confirm-live", "CONFIRM_LIVE_SEND"], {"SEND_MODE": "test"}),
        (["--execute", "--confirm-live", "CONFIRM_LIVE_SEND"], {"SEND_MODE": "live"}),
    ],
    ids=["live-only", "live-execute", "wrong-confirm", "settings-say-test", "no-live-flag"],
)
def test_live_sending_without_every_condition_fails_cleanly_and_sends_nothing(kit, args, env, no_google):
    kit.quickstart()
    full_env = dict(SEND_ENV, **env)
    res = kit.cli("send", "--drafts", "drafts", *args, env=full_env, no_google=no_google)
    assert res.code == 2, res.combined
    assert "requirements-gmail.txt" not in res.combined, "the safeguard message comes before the library check"
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


def test_execute_without_the_gmail_extra_explains_how_to_install_it(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--execute", env=SEND_ENV, no_google=True)
    assert res.code == 3, res.combined
    assert "requirements-gmail.txt" in res.combined
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


@pytest.mark.parametrize("no_google", [False, True], ids=["google-as-installed", "google-missing"])
def test_execute_in_test_mode_without_a_test_recipient_fails_cleanly(kit, no_google):
    kit.quickstart()
    env = {"SEND_MODE": "test", "SENDER_EMAIL": "sam@lumenlist.example.com"}
    res = kit.cli("send", "--drafts", "drafts", "--execute", env=env, no_google=no_google)
    assert res.code == 2, res.combined
    assert "TEST_RECIPIENT_EMAIL" in res.combined
    assert "requirements-gmail.txt" not in res.combined and "Traceback" not in res.combined
    assert res.net_attempts == ""


def test_auth_gmail_without_the_extra_explains_how_to_install_it(kit):
    res = kit.cli("auth", "gmail", no_google=True)
    assert res.code == 3, res.combined
    assert "requirements-gmail.txt" in res.combined
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""


# ======================================================================================================================
# SPEC section 19
# ======================================================================================================================

def stripped_lines(text):
    return [ln.strip() for ln in text.splitlines()]


def person(kit, ident, name, fact="your essay on how small teams ship weekly", **over):
    return kit.csv_row(id=ident, name=name, email=f"{ident}@example.com", org="Northwind", facts=fact, **over)


def run_draft(kit, rows, *extra, sender=None, env=None, contacts_name="people.csv"):
    contacts = kit.write_csv(rows, name=contacts_name)
    sender_path = sender or kit.write_sender()
    return kit.cli("draft", "--contacts", contacts.name, "--sender", sender_path.name, *extra, env=env)


def ok_drafts(kit, sub="drafts"):
    return sorted((kit.workdir / sub).glob("*.md"))


# ----- 19.1 the silent fallback to placeholder hooks ----------------------------------------------------------------------

def test_auto_fallback_warns_exactly_once_on_stderr_and_still_succeeds(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json")
    assert res.code == 0, res.combined
    assert stripped_lines(res.err).count(NO_KEY_WARNING) == 1
    assert NO_KEY_WARNING not in res.out
    assert summary_of(res)[1:] == (4, 1, "template")
    assert res.net_attempts == ""


def test_an_explicit_auto_provider_warns_the_same_way(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--provider", "auto")
    assert res.code == 0, res.combined
    assert stripped_lines(res.err).count(NO_KEY_WARNING) == 1


def test_the_warning_also_appears_when_the_env_file_has_no_key(kit):
    kit.cli("init")
    (kit.workdir / ".env").write_text("SEND_MODE=test\n", encoding="utf-8")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json")
    assert res.code == 0 and stripped_lines(res.err).count(NO_KEY_WARNING) == 1


def test_offline_prints_no_fallback_warning(kit):
    res = kit.quickstart()
    assert res.code == 0, res.combined
    assert "no provider key" not in res.combined and "placeholder hooks" not in res.combined


def test_an_explicit_template_provider_prints_no_fallback_warning(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--provider", "template")
    assert res.code == 0, res.combined
    assert "no provider key" not in res.combined and "placeholder hooks" not in res.combined
    assert summary_of(res)[1:] == (4, 1, "template")


def test_offline_with_keys_in_the_environment_prints_no_warning_either(kit):
    res = kit.quickstart(extra_env={"PUBLIK_API_KEY": KEY})
    assert res.code == 0 and "no provider key" not in res.combined


# ----- 19.2 why was a draft blocked, and what should I look at -------------------------------------------------------------------

def blocked_lines(res):
    return [ln.strip() for ln in res.out.splitlines() if ln.startswith("blocked ")]


def test_each_blocked_draft_gets_one_line_with_its_codes_on_stdout(kit):
    res = kit.quickstart()
    from vcoutreach.contacts import load_contacts

    empty_id = next(c.id for c in load_contacts(kit.workdir / "contacts.sample.csv") if not c.facts)
    assert blocked_lines(res) == [f"blocked {empty_id}: insufficient_facts"]
    assert not [ln for ln in res.err.splitlines() if ln.startswith("blocked ")]


def test_the_blocked_lines_come_after_the_summary_line_in_file_order(kit):
    rows = [person(kit, "ada", "Ada Lovelace"), person(kit, "zed", "Zed Zero", fact=""),
            person(kit, "bob", "Bob Byte"), person(kit, "amy", "Amy Zip", fact="")]
    res = run_draft(kit, rows, "--offline")
    assert res.code == 0, res.combined
    out_lines = stripped_lines(res.out)
    assert blocked_lines(res) == ["blocked zed: insufficient_facts", "blocked amy: insufficient_facts"]
    summary = "drafted=2 blocked=2 provider=template"
    if summary in out_lines:
        assert out_lines.index(summary) < out_lines.index("blocked zed: insufficient_facts")


def test_a_draft_blocked_for_several_reasons_lists_all_the_codes_separated_by_commas(kit):
    fact = f"your talk {kit.EM} and your essay!"
    res = run_draft(kit, [person(kit, "bob", "Bob Byte", fact=fact)], "--offline")
    assert res.code == 0, res.combined
    (line,) = blocked_lines(res)
    match = re.fullmatch(r"blocked bob: ([a-z_]+(?:, [a-z_]+)+)", line)
    assert match, line
    assert {"em_dash", "exclamation"} <= set(match.group(1).split(", "))


def test_no_blocked_lines_when_nothing_is_blocked(kit):
    res = run_draft(kit, [person(kit, "ada", "Ada Lovelace"), person(kit, "bob", "Bob Byte")], "--offline")
    assert res.code == 0, res.combined
    assert blocked_lines(res) == []


def test_strict_still_exits_2_and_still_lists_the_blocked_drafts(kit):
    res = run_draft(kit, [person(kit, "ada", "Ada Lovelace"), person(kit, "zed", "Zed Zero", fact="")], "--offline", "--strict")
    assert res.code == 2, res.combined
    assert "blocked zed: insufficient_facts" in stripped_lines(res.out + res.err)


def warnings_note(res):
    return [ln for ln in stripped_lines(res.err) if ln.startswith("note:") and "have warnings" in ln]


def test_ok_drafts_with_warnings_trigger_one_note_on_stderr(kit):
    rows = [person(kit, "ada", "Ada Lovelace"), person(kit, "jay", "J Smith")]
    res = run_draft(kit, rows, "--offline")
    assert res.code == 0, res.combined
    (note,) = warnings_note(res)
    assert note == ("note: 1 draft(s) have warnings (odd_greeting). "
                    "See the issues: line at the top of each draft, or drafts/index.jsonl."), note
    assert "lint" not in note, "lint cannot reproduce odd_greeting, so the note must not point at it"
    assert "have warnings" not in res.out
    assert "jay.md" in [p.name for p in ok_drafts(kit)], "a warning must not block the draft"


def test_the_note_counts_drafts_and_names_the_output_directory_it_was_given(kit):
    rows = [person(kit, "ada", "Ada Lovelace"), person(kit, "jay", "J Smith"), person(kit, "kay", "K Jones")]
    res = run_draft(kit, rows, "--offline", "--out", "mydrafts")
    (note,) = warnings_note(res)
    match = re.fullmatch(r"note: (\d+) draft\(s\) have warnings \(([^)]*)\)\. See the issues: line at the top of each draft, or (\S+index\.jsonl)\.", note)
    assert match, note
    assert match.group(1) == "2" and match.group(2) == "odd_greeting"
    assert match.group(3) in ("mydrafts/index.jsonl", "drafts/index.jsonl"), "the index the user can actually open"
    assert "lint" not in note


def test_the_note_lists_every_warning_code_in_parentheses(kit):
    short = kit.write_sender(product_lines=[], traction_lines=[], ask_line="")
    rows = [person(kit, "ada", "Ada Lovelace"), person(kit, "jay", "J Smith")]
    res = run_draft(kit, rows, "--offline", sender=short)
    (note,) = warnings_note(res)
    match = re.fullmatch(r"note: (\d+) draft\(s\) have warnings \(([^)]*)\)\. See the issues: line at the top of each draft, or (\S+index\.jsonl)\.", note)
    assert match, note
    assert int(match.group(1)) == 2
    assert set(re.split(r",\s*", match.group(2))) == {"odd_greeting", "word_count"}


def test_no_note_when_no_draft_has_warnings(kit):
    res = run_draft(kit, [person(kit, "ada", "Ada Lovelace"), person(kit, "bob", "Bob Byte")], "--offline")
    assert warnings_note(res) == []
    assert "have warnings" not in res.combined


def test_the_draft_header_of_a_blocked_file_lists_its_codes(kit):
    kit.quickstart()
    (blocked,) = (kit.workdir / "drafts" / "blocked").glob("*.md")
    _, headers, _ = kit.parse_md(blocked)
    assert "insufficient_facts" in headers["issues"]


# ----- 19.3 send: skipped and not-considered lines -----------------------------------------------------------------------------

def append_to_hook_paragraph(path, text):
    """Append ``text`` to the hook paragraph (the second one) of a draft.

    A draft without a body, such as the blocked no-facts row of the sample, has no hook paragraph:
    a fresh body is written instead, so the text is still there for lint to find.
    """
    head, sep, body = path.read_text(encoding="utf-8").partition("\n\n")
    paras = body.split("\n\n") if body.strip() else []
    if len(paras) < 2:
        paras = ["Hi there,", "I would leverage this" + text, "Sam"]
    else:
        paras[1] = paras[1] + text
    path.write_text(head + "\n\n" + "\n\n".join(paras), encoding="utf-8")


def test_send_plan_reports_the_blocked_drafts_it_did_not_consider(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "blocked drafts not considered: 1 (see drafts/blocked)" in stripped_lines(res.out + res.err)


def test_send_plan_counts_every_blocked_draft(kit):
    rows = [person(kit, "ada", "Ada Lovelace"), person(kit, "zed", "Zed Zero", fact=""), person(kit, "amy", "Amy Zip", fact="")]
    run_draft(kit, rows, "--offline")
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "blocked drafts not considered: 2 (see drafts/blocked)" in stripped_lines(res.out + res.err)


def test_send_plan_says_nothing_about_blocked_drafts_when_there_are_none(kit):
    run_draft(kit, [person(kit, "ada", "Ada Lovelace"), person(kit, "bob", "Bob Byte")], "--offline")
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "not considered" not in res.combined


def test_send_plan_prints_a_skipped_line_with_the_lint_codes(kit):
    kit.quickstart()
    target = ok_drafts(kit)[0]
    append_to_hook_paragraph(target, "!")
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    lines = [ln for ln in stripped_lines(res.out + res.err) if ln.startswith("skipped ")]
    (line,) = lines
    match = re.fullmatch(rf"skipped {re.escape(target.stem)}: lint \(([^)]*)\)", line)
    assert match, line
    assert "exclamation" in match.group(1).split(", ")


def test_send_plan_prints_one_skipped_line_per_skipped_draft(kit):
    kit.quickstart()
    first, second = ok_drafts(kit)[:2]
    append_to_hook_paragraph(first, "!")
    append_to_hook_paragraph(second, "!!")
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    skipped = [ln for ln in stripped_lines(res.out + res.err) if ln.startswith("skipped ")]
    assert sorted(ln.split(":")[0] for ln in skipped) == sorted(f"skipped {p.stem}" for p in (first, second))


def test_send_plan_works_without_a_test_recipient_and_says_so(kit):
    kit.quickstart()
    env = {"SEND_MODE": "test", "SENDER_EMAIL": "sam@lumenlist.example.com"}
    res = kit.cli("send", "--drafts", "drafts", env=env)
    assert res.code == 0, res.combined
    assert "<TEST_RECIPIENT_EMAIL not set>" in res.out
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


# ----- 19.8 messages and exit codes ------------------------------------------------------------------------------------------------

def test_draft_reports_how_many_drafts_and_blocked_drafts_it_wrote_on_stderr(kit):
    res = kit.quickstart()
    assert res.code == 0, res.combined
    (line,) = [ln for ln in stripped_lines(res.err) if ln.startswith("wrote ")]
    match = re.fullmatch(r"wrote drafts/ \((\d+) drafts, (\d+) blocked, index\.jsonl\)", line)
    assert match, line
    assert (int(match.group(1)), int(match.group(2))) == (len(ok_drafts(kit)), len(ok_drafts(kit, "drafts/blocked")))
    assert (int(match.group(1)), int(match.group(2))) == (4, 1)
    assert "wrote " not in res.out
    assert "files" not in line, "SPEC 21.7a: the old '(<n> files)' wording is gone"


def test_the_wrote_line_names_a_custom_output_directory(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--offline", "--out", "my-drafts")
    assert any(re.fullmatch(r"wrote my-drafts/ \(\d+ drafts, \d+ blocked, index\.jsonl\)", ln) for ln in stripped_lines(res.err)), res.err


def test_a_contacts_file_with_only_a_header_exits_2_with_a_clear_line(kit):
    contacts = kit.write_csv([], name="empty.csv")
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?no contacts found in \S*empty\.csv$", res.combined, re.M), res.combined
    assert "is empty" not in res.combined, "a header-only file is not a zero-byte file"
    assert "Traceback" not in res.combined
    assert not (kit.workdir / "drafts").exists() or list((kit.workdir / "drafts").rglob("*.md")) == []


def test_a_contacts_file_with_only_blank_rows_also_has_no_contacts(kit):
    text = kit.csv_text([]) + "\n,,,,,,,,,,\n\n"
    contacts = kit.write_raw("blank.csv", text)
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2 and "no contacts found in" in res.combined


def test_lint_of_an_empty_directory_exits_2(kit):
    (kit.workdir / "nothing").mkdir()
    res = kit.cli("lint", "--drafts", "nothing")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?no drafts found in \S*nothing$", res.combined, re.M), res.combined
    assert "Traceback" not in res.combined


def test_lint_of_a_directory_holding_only_blocked_drafts_finds_no_drafts(kit):
    run_draft(kit, [person(kit, "zed", "Zed Zero", fact="")], "--offline")
    assert ok_drafts(kit) == [] and list((kit.workdir / "drafts" / "blocked").glob("*.md"))
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 2, res.combined
    pattern = r"^(?:vcoutreach: )?no drafts found in drafts; 1 blocked draft\(s\) are in drafts/blocked$"
    assert re.search(pattern, res.combined, re.M), res.combined


def test_lint_checks_only_the_top_level_drafts_never_the_blocked_folder(kit):
    res = kit.quickstart()
    assert res.code == 0, res.combined
    assert len(list((kit.workdir / "drafts" / "blocked").glob("*.md"))) == 1, "the sample run does block one draft"
    lint = kit.cli("lint", "--drafts", "drafts")
    assert lint.code == 0, lint.combined
    linted, errors, _ = lint_summary(lint)
    assert (linted, errors) == (4, 0), "four ok drafts are linted, the blocked one is not counted"


def test_breaking_a_blocked_draft_by_hand_does_not_change_the_lint_result(kit):
    kit.quickstart()
    (blocked,) = (kit.workdir / "drafts" / "blocked").glob("*.md")
    before = kit.cli("lint", "--drafts", "drafts")
    append_to_hook_paragraph(blocked, "!")
    text = blocked.read_text(encoding="utf-8")
    assert "!" in text and "leverage" in text, "the blocked file now really breaks two lint rules"
    lint = kit.cli("lint", "--drafts", "drafts")
    assert lint.code == 0, lint.combined
    assert lint_summary(lint)[:2] == (4, 0)
    assert lint_summary(lint) == lint_summary(before)


@pytest.mark.parametrize("target", ["drafts/index.jsonl", "contacts.sample.csv", "FIRST_DRAFT"], ids=["jsonl", "csv", "a-draft-file"])
def test_lint_given_a_file_instead_of_a_directory_exits_2_and_says_what_it_expects(kit, target):
    kit.quickstart()
    if target == "FIRST_DRAFT":
        target = "drafts/" + ok_drafts(kit)[0].name
    res = kit.cli("lint", "--drafts", target)
    assert res.code == 2, res.combined
    assert "--drafts expects a directory" in res.combined, res.combined
    assert "Traceback" not in res.combined
    assert lint_summary(res) is None, "nothing was linted"


def test_lint_of_a_missing_directory_exits_2_without_a_traceback(kit):
    res = kit.cli("lint", "--drafts", "no-such-dir")
    assert res.code == 2, res.combined
    assert "Traceback" not in res.combined


LINT_SUMMARY = re.compile(r"^linted=(\d+) errors=(\d+) warnings=(\d+)$")


def lint_summary(res):
    for line in stripped_lines(res.out + "\n" + res.err):
        m = LINT_SUMMARY.match(line)
        if m:
            return tuple(int(g) for g in m.groups())
    return None


def test_lint_prints_the_linted_errors_warnings_summary(kit):
    kit.quickstart()
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 0, res.combined
    linted, errors, warnings = lint_summary(res)
    assert (linted, errors) == (4, 0)
    assert warnings >= 0


def test_lint_summary_counts_a_broken_draft_as_an_error(kit):
    kit.quickstart()
    clean = lint_summary(kit.cli("lint", "--drafts", "drafts"))
    append_to_hook_paragraph(ok_drafts(kit)[0], "!")
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 1, res.combined
    assert lint_summary(res) == (4, 1, clean[2])


def test_lint_summary_counts_warnings_and_warnings_alone_do_not_fail(kit):
    kit.quickstart()
    before = lint_summary(kit.cli("lint", "--drafts", "drafts"))
    target = ok_drafts(kit)[0]
    head, sep, _ = target.read_text(encoding="utf-8").partition("\n\n")
    target.write_text(head + sep + "Hi there,\n\nThanks for reading.\n\nSam", encoding="utf-8")
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 0, res.combined
    linted, errors, warnings = lint_summary(res)
    assert (linted, errors) == (4, 0)
    assert warnings >= max(1, before[2]), "a body under 50 words earns a word_count warning"


def test_lint_accepts_a_sender_file_and_then_checks_its_forbidden_phrases(kit):
    kit.quickstart()
    sender = kit.write_sender(forbidden_phrases=["circle back"])
    append_to_hook_paragraph(ok_drafts(kit)[0], " We should circle back.")
    without = kit.cli("lint", "--drafts", "drafts")
    assert without.code == 0, without.combined
    with_sender = kit.cli("lint", "--drafts", "drafts", "--sender", sender.name)
    assert with_sender.code == 1, with_sender.combined
    assert "forbidden_phrase" in with_sender.combined
    assert "Traceback" not in with_sender.combined


def test_lint_with_the_sample_sender_passes_on_untouched_drafts(kit):
    kit.quickstart()
    res = kit.cli("lint", "--drafts", "drafts", "--sender", "sender.sample.json")
    assert res.code == 0, res.combined


def test_lint_with_a_missing_sender_file_exits_2(kit):
    kit.quickstart()
    res = kit.cli("lint", "--drafts", "drafts", "--sender", "no-such-sender.json")
    assert res.code == 2, res.combined
    assert "Traceback" not in res.combined


def test_an_out_path_that_is_a_file_exits_2_with_the_exact_message_shape(kit):
    kit.cli("init")
    kit.write_raw("occupied", "i am a file, not a folder")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--offline", "--out", "occupied")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?cannot write to --out \S*occupied: \S.*$", res.combined, re.M), res.combined
    assert "Traceback" not in res.combined
    assert (kit.workdir / "occupied").read_text(encoding="utf-8") == "i am a file, not a folder"


def test_an_out_path_below_a_file_exits_2_too(kit):
    kit.cli("init")
    kit.write_raw("occupied", "x")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--offline",
                  "--out", "occupied/sub")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?cannot write to --out \S*occupied/sub: \S.*$", res.combined, re.M), res.combined


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_an_unwritable_out_directory_exits_2_with_the_exact_message_shape(kit):
    kit.cli("init")
    locked = kit.workdir / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--offline",
                      "--out", "locked")
    finally:
        os.chmod(locked, 0o700)
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?cannot write to --out \S*locked: \S.*$", res.combined, re.M), res.combined
    assert "Traceback" not in res.combined


def test_an_unknown_provider_name_exits_2_and_lists_the_valid_names(kit):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--provider", "bogus")
    assert res.code == 2, res.combined
    assert "Traceback" not in res.combined
    for name in ("template", "publik", "openai", "anthropic"):
        assert name in res.combined, f"the message should list {name}"
    assert res.net_attempts == ""
    assert not (kit.workdir / "drafts").exists() or list((kit.workdir / "drafts").rglob("*.md")) == []


def test_missing_google_libraries_exit_3_for_both_gmail_commands(kit):
    kit.quickstart()
    send = kit.cli("send", "--drafts", "drafts", "--execute", env=SEND_ENV, no_google=True)
    auth = kit.cli("auth", "gmail", no_google=True)
    assert send.code == 3 and auth.code == 3, (send.combined, auth.combined)
    for res in (send, auth):
        assert "requirements-gmail.txt" in res.combined and "Traceback" not in res.combined


# ----- every flag has a help string (19.8) ---------------------------------------------------------------------------------------------

def option_descriptions(help_text):
    """{invocation: description} for every line of the options section of an argparse help screen."""
    lines = help_text.splitlines()
    found = {}
    in_options = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\S.*:\s*$", line):
            in_options = line.strip().lower() in ("options:", "optional arguments:")
            i += 1
            continue
        match = re.match(r"^(\s{1,6})(-\S.*)$", line)
        if in_options and match:
            indent = len(match.group(1))
            parts = re.split(r"\s{2,}", match.group(2).rstrip(), maxsplit=1)
            invocation = parts[0]
            description = parts[1] if len(parts) > 1 else ""
            j = i + 1
            while j < len(lines) and lines[j].strip() and not re.match(r"^\s{1,6}-\S", lines[j]) \
                    and len(lines[j]) - len(lines[j].lstrip()) > indent:
                description += " " + lines[j].strip()
                j += 1
            found[invocation] = description.strip()
            i = j
            continue
        i += 1
    return found


EXPECTED_FLAGS = {
    (): {"--version"},
    ("init",): {"--dir"},
    ("draft",): {"--contacts", "--sender", "--provider", "--out", "--strict", "--offline"},
    ("lint",): {"--drafts", "--sender"},
    ("providers",): set(),
    ("send",): {"--drafts", "--execute", "--live", "--confirm-live", "--limit", "--sender"},
    ("auth",): set(),
}


def test_the_help_parser_understands_the_usual_argparse_layout():
    sample = (
        "usage: prog draft [-h] --contacts CONTACTS\n\n"
        "options:\n"
        "  -h, --help            show this help message and exit\n"
        "  --contacts CONTACTS   path to the contacts CSV\n"
        "  --very-long-option-name VERY_LONG_METAVAR\n"
        "                        wrapped onto the next line\n"
        "  --bare\n"
    )
    found = option_descriptions(sample)
    assert found["-h, --help"] == "show this help message and exit"
    assert found["--contacts CONTACTS"] == "path to the contacts CSV"
    assert found["--very-long-option-name VERY_LONG_METAVAR"] == "wrapped onto the next line"
    assert found["--bare"] == ""


@pytest.mark.parametrize("command", list(EXPECTED_FLAGS), ids=lambda c: " ".join(c) or "top-level")
def test_every_flag_of_every_command_has_a_help_string(kit, command):
    res = kit.cli(*command, "--help")
    assert res.code == 0, res.combined
    described = option_descriptions(res.out)
    assert described, f"no option lines found in the help of {command}:\n{res.out}"
    long_flags = set()
    for invocation, description in described.items():
        long_flags.update(re.findall(r"--[a-z][a-z-]*", invocation))
        assert len(description.split()) >= 2, f"{invocation!r} has no real help text: {description!r}"
        assert description.strip("- ").lower() not in invocation.lower(), f"{invocation!r} just repeats its own name"
    assert EXPECTED_FLAGS[command] <= long_flags, f"{command}: missing {EXPECTED_FLAGS[command] - long_flags}"


# ----- the shipped sample (SPEC 19.4 and 17) ----------------------------------------------------------------------------------------

def test_the_sample_quickstart_still_reports_exactly_four_drafted_one_blocked(kit):
    res = kit.quickstart()
    assert res.code == 0, res.combined
    assert "drafted=4 blocked=1 provider=template" in stripped_lines(res.out + res.err)


def test_the_first_sample_contact_gets_a_readable_draft_for_the_readme_cat_command(kit):
    from vcoutreach.contacts import load_contacts

    kit.quickstart()
    first = load_contacts(kit.workdir / "contacts.sample.csv")[0]
    path = kit.workdir / "drafts" / f"{first.id}.md"
    assert path.is_file(), "the README ends the quickstart with cat drafts/<first sample id>.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("to: ") and f"Hi {first.first_name}," in text


def test_the_sample_follow_up_draft_is_threaded_and_ok(kit):
    kit.quickstart()
    rows = kit.read_index(kit.workdir / "drafts")
    (follow_up,) = [r for r in rows if r["lane"] == "follow_up"]
    assert follow_up["status"] == "ok"
    assert re.fullmatch(r"<[^<>@\s]+@[^<>\s]+>", follow_up["in_reply_to"])
    assert follow_up["subject"].strip()


def test_no_sample_draft_has_an_odd_greeting_warning(kit):
    kit.quickstart()
    for row in kit.read_index(kit.workdir / "drafts"):
        assert "odd_greeting" not in [i["code"] for i in row["issues"]], row["contact_id"]



# ======================================================================================================================
# SPEC section 20
# ======================================================================================================================

def err_lines(res):
    return [ln for ln in res.err.splitlines() if ln.strip()]


# ----- 20.1 multi-line errors keep their newlines ---------------------------------------------------------------------------------

def test_a_contacts_error_with_several_problems_prints_one_problem_per_physical_line(kit):
    rows = [person(kit, i, f"Person {i.upper()}") for i in ("amy", "bob", "cat", "dan", "eve")]
    for index in (0, 2, 4):
        rows[index]["email"] = f"broken-{index}"
    res = run_draft(kit, rows, "--offline")
    assert res.code == 2, res.combined
    lines = err_lines(res)
    assert lines[0].startswith("vcoutreach: "), lines
    assert not any(ln.startswith("vcoutreach: ") for ln in lines[1:]), "only the first line carries the prefix"
    problem_lines = [ln for ln in lines if re.search(r"\brow\s+\d+", ln, re.I)]
    assert len(problem_lines) == 3, lines
    for ln in problem_lines:
        assert len(re.findall(r"\brow\s+\d+", ln, re.I)) == 1, f"two problems on one line: {ln!r}"
    named = sorted(int(re.search(r"\brow\s+(\d+)", ln, re.I).group(1)) for ln in problem_lines)
    assert named == [2, 4, 6]
    assert "Traceback" not in res.combined


def test_following_lines_of_a_multi_line_error_are_not_indented_prefixed_or_joined(kit):
    rows = [person(kit, "amy", "Person AMY"), person(kit, "bob", "Person BOB")]
    rows[0]["email"] = "broken-a"
    rows[1]["email"] = "broken-b"
    res = run_draft(kit, rows, "--offline")
    lines = err_lines(res)
    assert len(lines) >= 2
    assert all("\\n" not in ln and " | " not in ln for ln in lines), "no escaped newlines or joiners"
    for ln in lines[1:]:
        assert not ln.startswith("vcoutreach:")


def test_field_count_problems_each_get_their_own_line_too(kit):
    header = ",".join(kit.CSV_HEADER)
    path = kit.write_raw("shape.csv", "\n".join([
        header,
        "ga,Gina A,gina@example.com,Org,,,,,,,",
        "gb,Gina B,ginab@example.com,Acme, Inc.,,,,,,,",
        "gc,Gina C,ginac@example.com",
    ]) + "\n")
    sender = kit.write_sender()
    res = kit.cli("draft", "--contacts", path.name, "--sender", sender.name, "--offline")
    assert res.code == 2, res.combined
    lines = [ln for ln in err_lines(res) if "fields but the header has" in ln]
    assert len(lines) == 2, err_lines(res)
    assert any("row 3 has 12 fields" in ln for ln in lines) and any("row 4 has 3 fields" in ln for ln in lines)


def test_a_single_line_error_is_still_one_prefixed_line(kit):
    contacts = kit.write_csv([kit.csv_row(id="a", name="A B", email="a@example.com", facts="x")])
    sender = kit.write_raw("sender.json", json.dumps({"title": "Founder"}))
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2, res.combined
    (line,) = err_lines(res)
    assert line.startswith("vcoutreach: ")


# ----- 20.3 stale drafts ---------------------------------------------------------------------------------------------------------------

IDS5 = ("amy", "bob", "cat", "dan", "eve")
STALE_LINE = "removed stale draft {} (no longer in the contacts file)"


def five_people(kit, without=()):
    return [person(kit, i, f"Person {i.upper()}") for i in IDS5 if i not in without]


def draft_names(kit, out="drafts"):
    return sorted(str(p.relative_to(kit.workdir / out)) for p in (kit.workdir / out).rglob("*.md"))


def removed_lines(res):
    return [ln for ln in stripped_lines(res.err) if ln.startswith("removed stale draft")]


def test_a_contact_deleted_from_the_csv_loses_its_draft_on_the_next_run(kit):
    first = run_draft(kit, five_people(kit), "--offline")
    assert first.code == 0 and draft_names(kit) == ["amy.md", "bob.md", "cat.md", "dan.md", "eve.md"]
    assert removed_lines(first) == []
    second = run_draft(kit, five_people(kit, without=("cat",)), "--offline")
    assert second.code == 0, second.combined
    assert draft_names(kit) == ["amy.md", "bob.md", "dan.md", "eve.md"]
    assert removed_lines(second) == [STALE_LINE.format("cat")]
    assert "removed stale" not in second.out
    assert [r["contact_id"] for r in kit.read_index(kit.workdir / "drafts")] == ["amy", "bob", "dan", "eve"]
    assert summary_of(second)[1:] == (4, 0, "template")


def test_the_send_plan_no_longer_lists_a_removed_contact(kit):
    run_draft(kit, five_people(kit), "--offline")
    before = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert before.code == 0 and before.out.count("me@mail.test") >= 5
    run_draft(kit, five_people(kit, without=("cat",)), "--offline")
    after = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert after.code == 0, after.combined
    assert 4 <= after.out.count("me@mail.test") < before.out.count("me@mail.test"), "one planned message fewer"
    assert "cat@example.com" not in after.out + after.err
    assert "skipped cat" not in after.out + after.err


def test_a_stale_blocked_draft_is_removed_from_the_blocked_folder_and_reported(kit):
    rows = five_people(kit)
    rows[2] = person(kit, "cat", "Person CAT", fact="")
    run_draft(kit, rows, "--offline")
    assert "blocked/cat.md" in draft_names(kit)
    res = run_draft(kit, five_people(kit, without=("cat",)), "--offline")
    assert draft_names(kit) == ["amy.md", "bob.md", "dan.md", "eve.md"]
    assert removed_lines(res) == [STALE_LINE.format("cat")]


def test_every_removed_contact_gets_its_own_line(kit):
    run_draft(kit, five_people(kit), "--offline")
    res = run_draft(kit, five_people(kit, without=("bob", "dan", "eve")), "--offline")
    assert sorted(removed_lines(res)) == sorted(STALE_LINE.format(i) for i in ("bob", "dan", "eve"))
    assert draft_names(kit) == ["amy.md", "cat.md"]


def test_stale_drafts_are_pruned_inside_a_custom_out_directory_only(kit):
    run_draft(kit, five_people(kit), "--offline", "--out", "mine")
    run_draft(kit, five_people(kit), "--offline")
    res = run_draft(kit, five_people(kit, without=("amy",)), "--offline", "--out", "mine")
    assert removed_lines(res) == [STALE_LINE.format("amy")]
    assert "amy.md" not in draft_names(kit, "mine") and "amy.md" in draft_names(kit, "drafts")


def test_a_rerun_with_the_same_contacts_prints_no_removal_lines(kit):
    run_draft(kit, five_people(kit), "--offline")
    res = run_draft(kit, five_people(kit), "--offline")
    assert removed_lines(res) == []


def test_hand_written_files_are_left_alone_by_the_cleanup(kit):
    run_draft(kit, five_people(kit), "--offline")
    (kit.workdir / "drafts" / "hand.md").write_text(kit.md_text(to="h@example.com", subject="s", body="mine"), encoding="utf-8")
    (kit.workdir / "drafts" / "blocked" / "hand2.md").parent.mkdir(exist_ok=True)
    (kit.workdir / "drafts" / "blocked" / "hand2.md").write_text(kit.md_text(to="h2@example.com", subject="s", body="mine", status="blocked"), encoding="utf-8")
    res = run_draft(kit, five_people(kit, without=("amy",)), "--offline")
    assert removed_lines(res) == [STALE_LINE.format("amy")]
    assert (kit.workdir / "drafts" / "hand.md").exists() and (kit.workdir / "drafts" / "blocked" / "hand2.md").exists()


def test_a_provider_abort_on_the_second_run_deletes_nothing(kit):
    run_draft(kit, five_people(kit), "--offline")
    env = {"PUBLIK_API_KEY": KEY, "PUBLIK_API_BASE_URL": "http://127.0.0.1:9/api/v1", "PUBLIK_MAX_RETRIES": "0"}
    res = run_draft(kit, five_people(kit, without=("cat",)), "--provider", "publik", env=env)
    assert res.code == 3, res.combined
    assert removed_lines(res) == []
    assert draft_names(kit) == ["amy.md", "bob.md", "cat.md", "dan.md", "eve.md"]


def test_a_missing_index_means_nothing_is_pruned(kit):
    run_draft(kit, five_people(kit), "--offline")
    (kit.workdir / "drafts" / "index.jsonl").unlink()
    res = run_draft(kit, five_people(kit, without=("cat",)), "--offline")
    assert res.code == 0, res.combined
    assert removed_lines(res) == [] and "cat.md" in draft_names(kit)
    assert "Traceback" not in res.combined


@pytest.mark.parametrize("garbage", [b"\x00\xff\xfe", b"not json\n", b"[1, 2]\n", b'{"contact_id": 5}\n{"contact_id": ["cat"]}\n', b""],
                         ids=["binary", "text", "array", "odd-ids", "empty"])
def test_a_garbage_index_means_nothing_is_pruned_and_nothing_crashes(kit, garbage):
    run_draft(kit, five_people(kit), "--offline")
    (kit.workdir / "drafts" / "index.jsonl").write_bytes(garbage)
    res = run_draft(kit, five_people(kit, without=("cat",)), "--offline")
    assert res.code == 0, res.combined
    assert removed_lines(res) == [] and "cat.md" in draft_names(kit)
    assert "Traceback" not in res.combined


def test_a_tampered_index_cannot_delete_files_outside_the_output_directory(kit):
    run_draft(kit, five_people(kit), "--offline")
    victim = kit.workdir / "victim.md"
    victim.write_text("precious", encoding="utf-8")
    absolute = kit.workdir / "abs-victim.md"
    absolute.write_text("precious too", encoding="utf-8")
    lines = [{"contact_id": cid} for cid in ("../victim", str(kit.workdir / "abs-victim"), "sub/inner", "..")]
    (kit.workdir / "drafts" / "index.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    res = run_draft(kit, five_people(kit), "--offline")
    assert res.code == 0, res.combined
    assert victim.read_text(encoding="utf-8") == "precious" and absolute.read_text(encoding="utf-8") == "precious too"
    assert not [ln for ln in removed_lines(res) if "victim" in ln or ".." in ln]


# ----- 20.4 em dash in the sender file ----------------------------------------------------------------------------------------------

def bad_sender(kit, **fields):
    data = {"name": "Sam Rivera", "product_lines": ["We build a tool " + kit.EM + " for founders."]}
    data.update(fields)
    return kit.write_raw("bad-sender.json", json.dumps(data, ensure_ascii=False))


DASH_LINE = r"^(?:vcoutreach: )?sender file .+: field 'product_lines' contains an em dash or en dash$"


def test_draft_rejects_a_sender_file_with_an_em_dash_and_names_the_field(kit):
    contacts = kit.write_csv([kit.csv_row(id="a", name="A B", email="a@example.com", facts="your essay")])
    sender = bad_sender(kit)
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2, res.combined
    assert re.search(DASH_LINE, res.combined, re.M), res.combined
    assert sender.name in res.combined and "Traceback" not in res.combined
    assert not (kit.workdir / "drafts").exists() or list((kit.workdir / "drafts").rglob("*.md")) == []


def test_an_en_dash_in_the_signature_is_named_too(kit):
    contacts = kit.write_csv([kit.csv_row(id="a", name="A B", email="a@example.com", facts="your essay")])
    sender = bad_sender(kit, product_lines=["fine"], signature="Sam " + kit.EN + " Founder")
    res = kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline")
    assert res.code == 2 and "field 'signature'" in res.combined


@pytest.mark.parametrize("command", ["lint", "send"])
def test_lint_and_send_reject_the_same_sender_file(kit, command):
    kit.quickstart()
    sender = bad_sender(kit)
    res = kit.cli(command, "--drafts", "drafts", "--sender", sender.name, env=SEND_ENV)
    assert res.code == 2, res.combined
    assert re.search(DASH_LINE, res.combined, re.M), res.combined


# ----- 20.5 the safeguards come before the optional Google libraries ------------------------------------------------------------------

LIVE_WITHOUT_FLAG = (
    "SEND_MODE=live is set but --live was not passed. Nothing was sent. To preview without live risk use "
    "SEND_MODE=test (the default); to send for real you also need --live, --execute and --confirm-live CONFIRM_LIVE_SEND."
)
EXECUTE_ENV = {"SENDER_EMAIL": "sam@lumenlist.example.com"}

GATE_CASES = {
    "no-test-recipient": (["--execute"], dict(EXECUTE_ENV, SEND_MODE="test"), "TEST_RECIPIENT_EMAIL"),
    "blank-test-recipient": (["--execute"], dict(EXECUTE_ENV, SEND_MODE="test", TEST_RECIPIENT_EMAIL="   "), "TEST_RECIPIENT_EMAIL"),
    "live-flag-in-test-mode": (["--live", "--execute", "--confirm-live", "CONFIRM_LIVE_SEND"], dict(SEND_ENV), None),
    "live-mode-without-the-live-flag": (["--execute", "--confirm-live", "CONFIRM_LIVE_SEND"], dict(SEND_ENV, SEND_MODE="live"), LIVE_WITHOUT_FLAG),
    "live-without-confirm": (["--live", "--execute"], dict(SEND_ENV, SEND_MODE="live"), "CONFIRM_LIVE_SEND"),
    "live-with-wrong-confirm": (["--live", "--execute", "--confirm-live", "nope"], dict(SEND_ENV, SEND_MODE="live"), "CONFIRM_LIVE_SEND"),
    "invalid-send-mode": (["--execute"], dict(SEND_ENV, SEND_MODE="production"), "SEND_MODE"),
}


@pytest.mark.parametrize("no_google", [False, True], ids=["google-as-installed", "google-missing"])
@pytest.mark.parametrize("case", list(GATE_CASES))
def test_every_gate_condition_shows_its_safeguard_message_before_the_library_check(kit, case, no_google):
    args, env, expected = GATE_CASES[case]
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", *args, env=env, no_google=no_google)
    assert res.code == 2, res.combined
    assert "requirements-gmail.txt" not in res.combined and "pip install" not in res.combined
    assert "Traceback" not in res.combined
    if expected is not None:
        assert expected in res.combined, res.combined
    else:
        assert "SEND_MODE" in res.combined or "--live" in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


def test_the_live_without_flag_message_is_the_exact_text_of_the_spec(kit):
    kit.quickstart()
    args, env, _ = GATE_CASES["live-mode-without-the-live-flag"]
    res = kit.cli("send", "--drafts", "drafts", *args, env=env, no_google=True)
    assert res.code == 2
    assert LIVE_WITHOUT_FLAG in res.combined
    assert re.search(r"^(?:vcoutreach: )?" + re.escape(LIVE_WITHOUT_FLAG) + r"$", res.combined, re.M)


def test_once_every_gate_passes_a_missing_library_is_exit_3_with_the_install_hint(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--execute", env=SEND_ENV, no_google=True)
    assert res.code == 3, res.combined
    assert "requirements-gmail.txt" in res.combined and "Traceback" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists(), "nothing is sent"


def test_a_plan_never_needs_the_google_libraries(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV, no_google=True)
    assert res.code == 0, res.combined
    assert "requirements-gmail.txt" not in res.combined


# ----- 20.7 files that are not CSV ----------------------------------------------------------------------------------------------------

def draft_with(kit, contacts_name):
    sender = kit.write_sender()
    return kit.cli("draft", "--contacts", contacts_name, "--sender", sender.name, "--offline")


def test_a_json_contacts_file_exits_2_with_the_json_hint(kit):
    kit.write_raw("contacts.json", '{"contacts": [{"name": "Ada"}]}')
    res = draft_with(kit, "contacts.json")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?contacts\.json looks like JSON, expected a CSV file with a header row$", res.combined, re.M), res.combined
    assert "Traceback" not in res.combined


@pytest.mark.parametrize("delimiter", [";", "\t"], ids=["semicolon", "tab"])
def test_a_semicolon_or_tab_contacts_file_exits_2_with_the_single_column_hint(kit, delimiter):
    kit.write_raw("european.csv", delimiter.join(["id", "name", "email"]) + "\n" + delimiter.join(["a", "Ada", "ada@example.com"]) + "\n")
    res = draft_with(kit, "european.csv")
    assert res.code == 2, res.combined
    expected = r"^(?:vcoutreach: )?european\.csv has a single column; is it separated by semicolons or tabs\? Save it as a comma-separated UTF-8 CSV$"
    assert re.search(expected, res.combined, re.M), res.combined


def test_a_zero_byte_contacts_file_says_it_is_empty(kit):
    kit.write_raw("zero.csv", b"")
    res = draft_with(kit, "zero.csv")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?zero\.csv is empty: expected a header row$", res.combined, re.M), res.combined


def test_zero_byte_and_header_only_files_print_different_messages(kit):
    kit.write_raw("zero.csv", b"")
    kit.write_csv([], name="header-only.csv")
    zero = draft_with(kit, "zero.csv")
    header_only = draft_with(kit, "header-only.csv")
    assert zero.code == 2 and header_only.code == 2
    assert "is empty: expected a header row" in zero.combined and "no contacts found in" not in zero.combined
    assert "no contacts found in" in header_only.combined and "expected a header row" not in header_only.combined


# ----- 20.8 send on a directory without top-level drafts -----------------------------------------------------------------------------------

def test_send_with_only_blocked_drafts_exits_2_and_points_at_the_blocked_folder(kit):
    run_draft(kit, [person(kit, "zed", "Zed Zero", fact=""), person(kit, "amy", "Amy Zip", fact="")], "--offline")
    assert ok_drafts(kit) == []
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 2, res.combined
    pattern = r"^(?:vcoutreach: )?no drafts found in drafts; 2 blocked draft\(s\) are in drafts/blocked$"
    assert re.search(pattern, res.combined, re.M), res.combined
    assert "Traceback" not in res.combined


def test_send_with_one_blocked_draft_counts_one(kit):
    run_draft(kit, [person(kit, "zed", "Zed Zero", fact="")], "--offline")
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 2
    assert "no drafts found in drafts; 1 blocked draft(s) are in drafts/blocked" in res.combined


def test_send_on_an_empty_directory_exits_2_without_a_blocked_suffix(kit):
    (kit.workdir / "nothing").mkdir()
    res = kit.cli("send", "--drafts", "nothing", env=SEND_ENV)
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?no drafts found in \S*nothing$", res.combined, re.M), res.combined


def test_an_empty_blocked_folder_adds_no_suffix(kit):
    (kit.workdir / "nothing" / "blocked").mkdir(parents=True)
    res = kit.cli("send", "--drafts", "nothing", env=SEND_ENV)
    assert res.code == 2
    assert "blocked draft(s)" not in res.combined


def test_send_on_a_missing_directory_exits_2_without_a_traceback(kit):
    res = kit.cli("send", "--drafts", "no-such-dir", env=SEND_ENV)
    assert res.code == 2, res.combined
    assert "Traceback" not in res.combined


def test_send_with_both_ok_and_blocked_drafts_still_plans_the_ok_ones(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0 and "no drafts found" not in res.combined


# ----- 20.9 one trailing newline on every draft file -------------------------------------------------------------------------------------

def test_every_ok_draft_file_ends_with_exactly_one_newline(kit):
    kit.quickstart()
    for path in ok_drafts(kit):
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n") and not text.endswith("\n\n"), (path.name, repr(text[-15:]))


def test_blocked_draft_files_end_with_a_newline_too(kit):
    kit.quickstart()
    for path in (kit.workdir / "drafts" / "blocked").glob("*.md"):
        assert path.read_text(encoding="utf-8").endswith("\n"), path.name


def test_drafts_survive_a_lint_round_trip_after_the_newline_change(kit):
    kit.quickstart()
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 0 and lint_summary(res)[:2] == (4, 0)


# ----- 20.10 send tells you when the hooks are placeholders ---------------------------------------------------------------------------------

def template_note(n):
    return f"note: {n} draft(s) were written by the offline template provider (placeholder hooks); read them before sending."


def hand_drafts(kit, providers):
    out = kit.workdir / "drafts"
    out.mkdir(exist_ok=True)
    for index, provider in enumerate(providers):
        ident = f"p{chr(97 + index)}"
        text = kit.md_text(to=f"{ident}@example.com", subject="a note for Ada", body=kit.clean_body(first_name="Ada"), provider=provider)
        (out / f"{ident}.md").write_text(text + "\n", encoding="utf-8")


def test_send_warns_on_stderr_when_the_drafts_came_from_the_template_provider(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert stripped_lines(res.err).count(template_note(4)) == 1
    assert template_note(4) not in res.out
    assert "me@mail.test" in res.out, "the note never blocks the plan"


def test_the_template_note_counts_only_template_drafts(kit):
    hand_drafts(kit, ["template", "publik", "template", "openai", ""])
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert stripped_lines(res.err).count(template_note(2)) == 1


def test_one_template_draft_gets_the_same_note_with_a_one(kit):
    hand_drafts(kit, ["template"])
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert template_note(1) in stripped_lines(res.err)


@pytest.mark.parametrize("providers", [["publik", "publik"], ["openai"], ["anthropic", "publik"], [""]], ids=["publik", "openai", "mixed-byo", "no-provider"])
def test_no_template_note_when_no_draft_came_from_the_template_provider(kit, providers):
    hand_drafts(kit, providers)
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "offline template provider" not in res.combined and "placeholder hooks" not in res.combined


def test_the_template_note_also_appears_when_a_sender_file_is_given(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--sender", "sender.sample.json", env=SEND_ENV)
    assert res.code == 0 and template_note(4) in stripped_lines(res.err)


def test_the_template_note_does_not_depend_on_the_test_recipient(kit):
    kit.quickstart()
    env = {"SEND_MODE": "test", "SENDER_EMAIL": "sam@lumenlist.example.com"}
    res = kit.cli("send", "--drafts", "drafts", env=env)
    assert res.code == 0 and template_note(4) in stripped_lines(res.err)


# ----- packaging smoke ---------------------------------------------------------------------------------------------------------------------

def test_every_public_module_imports_without_any_third_party_package(kit):
    code = (
        "import sys, importlib\n"
        "names = ['vcoutreach', 'vcoutreach.types', 'vcoutreach.config', 'vcoutreach.contacts', 'vcoutreach.sender',\n"
        "         'vcoutreach.templates', 'vcoutreach.lint', 'vcoutreach.grounding', 'vcoutreach.journal',\n"
        "         'vcoutreach.drafts_io', 'vcoutreach.pipeline', 'vcoutreach.prompts', 'vcoutreach.providers',\n"
        "         'vcoutreach.providers.http', 'vcoutreach.send', 'vcoutreach.send.gate', 'vcoutreach.send.mime',\n"
        "         'vcoutreach.send.gmail', 'vcoutreach.cli']\n"
        "for n in names:\n"
        "    importlib.import_module(n)\n"
        "banned = {'google', 'googleapiclient', 'google_auth_oauthlib', 'oauthlib', 'httplib2', 'requests', 'httpx',\n"
        "          'urllib3', 'openai', 'anthropic', 'dotenv', 'yaml', 'pydantic', 'click', 'rich', 'certifi'}\n"
        "found = sorted(m for m in sys.modules if m.split('.')[0] in banned)\n"
        "print(found)\n"
        "sys.exit(1 if found else 0)\n"
    )
    proc = kit.run_python(code)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ======================================================================================================================
# SPEC section 21 (third review)
# ======================================================================================================================

def code_tokens(line):
    """The comma separated codes of a ``blocked <id>: a, b`` line."""
    return [c.strip() for c in line.partition(": ")[2].split(",") if c.strip()]


def header_issue_tokens(path):
    head = path.read_text(encoding="utf-8").split("\n\n", 1)[0]
    (line,) = [ln for ln in head.split("\n") if ln.startswith("issues:")]
    return [c.strip() for c in line.partition(":")[2].split(";") if c.strip()]


def unique(items):
    return list(dict.fromkeys(items))


# ----- 21.2 issue codes are listed once --------------------------------------------------------------------------------------------------

REPEATING_FACT = "your post on how to leverage queues, delve into logs and touch base with users"


def run_repeating(kit):
    sender = kit.write_sender(forbidden_phrases=["queues", "logs", "users"])
    return run_draft(kit, [person(kit, "bob", "Bob Byte", fact=REPEATING_FACT)], "--offline", sender=sender)


def test_a_blocked_line_lists_each_issue_code_once(kit):
    res = run_repeating(kit)
    assert res.code == 0, res.combined
    (line,) = blocked_lines(res)
    tokens = code_tokens(line)
    assert {"filler", "forbidden_phrase"} <= set(tokens), line
    assert tokens == unique(tokens), f"a code is repeated: {line}"


def test_the_header_of_the_blocked_draft_lists_each_issue_code_once_and_in_the_console_order(kit):
    res = run_repeating(kit)
    (blocked,) = (kit.workdir / "drafts" / "blocked").glob("*.md")
    header = header_issue_tokens(blocked)
    assert {"filler", "forbidden_phrase"} <= set(header)
    assert header == unique(header), header
    assert header == code_tokens(blocked_lines(res)[0])


def test_the_warnings_note_and_the_headers_list_each_code_once_however_many_drafts_have_it(kit):
    short = kit.write_sender(product_lines=[], traction_lines=[], ask_line="")
    rows = [person(kit, "jay", "J Smith"), person(kit, "kay", "K Jones"), person(kit, "lee", "L Wu")]
    res = run_draft(kit, rows, "--offline", sender=short)
    assert res.code == 0, res.combined
    (note,) = warnings_note(res)
    match = re.fullmatch(r"note: (\d+) draft\(s\) have warnings \(([^)]*)\)\. See the issues: line at the top of each draft, or (\S+index\.jsonl)\.", note)
    assert match, note
    tokens = re.split(r",\s*", match.group(2))
    assert match.group(1) == "3" and tokens == unique(tokens) and set(tokens) == {"odd_greeting", "word_count"}, note
    for path in ok_drafts(kit):
        header = header_issue_tokens(path)
        assert header == unique(header), (path.name, header)


# ----- 21.3 a markdown file that is not a draft stops send and lint --------------------------------------------------------------

NOT_A_DRAFT = re.compile(
    r"draft (\S+) is not a vcoutreach draft \(missing header line\(s\): ([^)]*)\); "
    r"move it out of the drafts folder or fix its header"
)
STRAY_TEXT = "Remember to read every hook before sending.\nThen send on Tuesday.\n"


def put_stray(kit, name, text=STRAY_TEXT, sub=""):
    folder = kit.workdir / "drafts" / sub if sub else kit.workdir / "drafts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def missing_header_lines(res):
    match = NOT_A_DRAFT.search(res.combined)
    assert match, res.combined
    return match, [t for t in re.split(r"[,\s]+", match.group(2)) if t]


@pytest.mark.parametrize("name", ["AAA-notes.md", "pam-notes.md", "zzz-notes.md"], ids=["sorts-first", "sorts-between", "sorts-last"])
def test_a_stray_markdown_note_stops_send_with_exit_2_and_plans_nothing(kit, name):
    hand_drafts(kit, ["publik", "publik"])
    put_stray(kit, name)
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 2, res.combined
    match, missing = missing_header_lines(res)
    assert match.group(1) in (name, name[:-3])
    assert {"to", "subject"} <= set(missing), missing
    assert "Traceback" not in res.combined
    assert "me@mail.test" not in res.out and "@example.com" not in res.out, "nothing may be planned"
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()
    assert res.net_attempts == ""


def test_a_stray_markdown_note_stops_lint_with_exit_2_too(kit):
    hand_drafts(kit, ["publik", "publik"])
    put_stray(kit, "notes.md")
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 2, res.combined
    match, missing = missing_header_lines(res)
    assert match.group(1) in ("notes.md", "notes")
    assert "Traceback" not in res.combined
    assert lint_summary(res) is None, "nothing may be reported as linted"


@pytest.mark.parametrize("command", ["send", "lint"])
def test_a_draft_whose_to_line_was_deleted_by_hand_is_refused_and_the_missing_line_is_named(kit, command):
    kit.quickstart()
    victim = ok_drafts(kit)[0]
    head, sep, body = victim.read_text(encoding="utf-8").partition("\n\n")
    victim.write_text("\n".join(ln for ln in head.split("\n") if not ln.startswith("to:")) + sep + body, encoding="utf-8")
    res = kit.cli(command, "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 2, res.combined
    match, missing = missing_header_lines(res)
    assert match.group(1) in (victim.name, victim.stem)
    assert "to" in missing and "subject" not in missing, missing
    assert "me@mail.test" not in res.out
    assert "Traceback" not in res.combined


def test_other_files_in_the_drafts_folder_are_still_ignored(kit):
    kit.quickstart()
    put_stray(kit, "notes.md", sub="blocked")
    put_stray(kit, "notes.txt")
    put_stray(kit, "README")
    sent = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert sent.code == 0, sent.combined
    assert "not a vcoutreach draft" not in sent.combined
    linted = kit.cli("lint", "--drafts", "drafts")
    assert linted.code == 0 and lint_summary(linted)[:2] == (4, 0), linted.combined


# ----- 21.1 ids that differ only by case -------------------------------------------------------------------------------------------------------

CASE_NOTE = "(ids that differ only by case count as duplicates, because each draft is a file)"


def test_ids_that_differ_only_by_case_are_refused_at_load_time_and_both_rows_are_named(kit):
    rows = [person(kit, "Ann", "Ann Lee"), person(kit, "bob", "Bob Byte"), kit.csv_row(id="ann", name="Ann Two", email="ann2@example.com", facts="your talk")]
    res = run_draft(kit, rows, "--offline")
    assert res.code == 2, res.combined
    kit.assert_names_rows(res.combined, [1, 3], 3)
    assert CASE_NOTE in res.combined
    assert "Traceback" not in res.combined
    assert not (kit.workdir / "drafts").exists() or list((kit.workdir / "drafts").rglob("*.md")) == [], "nothing may be written"
    assert res.net_attempts == ""


# ----- 21.4 greetings ---------------------------------------------------------------------------------------------------------------------------------

def greeting_of(path):
    return kit_body(path).split("\n", 1)[0]


def kit_body(path):
    return path.read_text(encoding="utf-8").partition("\n\n")[2]


def test_a_degree_after_a_comma_and_a_last_first_name_are_greeted_by_the_first_name(kit):
    rows = [person(kit, "alice", "Alice Brown, PhD"), person(kit, "taylor", "Smith, Taylor"),
            person(kit, "dana", "Dana Lee, Jr."), person(kit, "mia", "Dr. Mia Wong, MD")]
    res = run_draft(kit, rows, "--offline")
    assert res.code == 0, res.combined
    drafts = {p.stem: p for p in ok_drafts(kit)}
    assert sorted(drafts) == ["alice", "dana", "mia", "taylor"]
    assert greeting_of(drafts["alice"]) == "Hi Alice,"
    assert greeting_of(drafts["taylor"]) == "Hi Taylor,"
    assert greeting_of(drafts["dana"]) == "Hi Dana,"
    assert greeting_of(drafts["mia"]) == "Hi Mia,"
    assert warnings_note(res) == [], "none of these greetings is odd"


def test_the_known_imperfect_smith_jr_john_case_still_gets_a_non_empty_greeting(kit):
    res = run_draft(kit, [person(kit, "sj", "Smith, Jr., John")], "--offline")
    assert res.code == 0, res.combined
    assert "Traceback" not in res.combined
    greeting = greeting_of(kit.workdir / "drafts" / "sj.md") if (kit.workdir / "drafts" / "sj.md").exists() else greeting_of(kit.workdir / "drafts" / "blocked" / "sj.md")
    assert re.fullmatch(r"Hi \S+,", greeting), greeting


def test_a_team_name_gets_an_odd_greeting_warning_but_is_not_blocked(kit):
    res = run_draft(kit, [person(kit, "team", "The Smith Team"), person(kit, "ada", "Ada Lovelace")], "--offline")
    assert res.code == 0, res.combined
    assert "team.md" in [p.name for p in ok_drafts(kit)], "a warning must not block the draft"
    (note,) = warnings_note(res)
    assert note.startswith("note: 1 draft(s) have warnings (odd_greeting)"), note
    assert "odd_greeting" in header_issue_tokens(kit.workdir / "drafts" / "team.md")


# ----- 21.5 the offline template provider and sentence facts ------------------------------------------------------------------------

NOUN_PHRASE = 'the offline template needs facts written as noun phrases (for example "your 2024 essay on X"), not sentences'


def test_a_sentence_style_fact_is_blocked_with_the_noun_phrase_message(kit):
    res = run_draft(kit, [person(kit, "bob", "Bob Byte", fact="You wrote a great essay on queues")], "--offline")
    assert res.code == 0, res.combined
    assert blocked_lines(res) == ["blocked bob: insufficient_facts"]
    assert (kit.workdir / "drafts" / "blocked" / "bob.md").exists() and not (kit.workdir / "drafts" / "bob.md").exists()
    (row,) = kit.read_index(kit.workdir / "drafts")
    messages = [i["message"] for i in row["issues"] if i["code"] == "insufficient_facts"]
    assert messages and all(NOUN_PHRASE in m for m in messages), messages


def test_a_fact_that_starts_with_a_capital_your_reads_naturally_in_the_hook(kit):
    res = run_draft(kit, [person(kit, "bob", "Bob Byte", fact="Your essay on queue design.")], "--offline")
    assert res.code == 0, res.combined
    text = (kit.workdir / "drafts" / "bob.md").read_text(encoding="utf-8")
    assert "I came across your essay on queue design, and I found it interesting." in text
    assert "Your essay" not in text


# ----- 21.6 no default model ---------------------------------------------------------------------------------------------------------------------------

OPENAI_ONLY = {"OPENAI_API_KEY": "ok_fake_bbbbbbbbbbbbbbbbbbbbZ222"}
ANTHROPIC_ONLY = {"ANTHROPIC_API_KEY": "ak_fake_ccccccccccccccccccccZ333"}
MODEL_CASES = [("openai", OPENAI_ONLY, "OPENAI_MODEL"), ("anthropic", ANTHROPIC_ONLY, "ANTHROPIC_MODEL")]


def missing_model_message(variable):
    return f"{variable} is not set. Set it to the model name you want to use (see your provider's model list)."


@pytest.mark.parametrize("provider,env,variable", MODEL_CASES, ids=["openai", "anthropic"])
def test_an_explicit_provider_with_a_key_but_no_model_exits_3_naming_the_model_variable(kit, provider, env, variable):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--provider", provider, env=env)
    assert res.code == 3, res.combined
    assert "Traceback" not in res.combined
    message = nonblank(res.err) or nonblank(res.out)
    assert len(message) == 1, message
    assert missing_model_message(variable) in message[0]
    assert list(env.values())[0] not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / "drafts").exists() or list((kit.workdir / "drafts").rglob("*.md")) == []


@pytest.mark.parametrize("extra", [[], ["--provider", "auto"]], ids=["default", "explicit-auto"])
@pytest.mark.parametrize("provider,env,variable", MODEL_CASES, ids=["openai", "anthropic"])
def test_auto_with_a_key_but_no_model_surfaces_the_error_instead_of_using_the_template(kit, extra, provider, env, variable):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", *extra, env=env)
    assert res.code == 3, res.combined
    assert missing_model_message(variable) in res.combined
    assert summary_of(res) is None, "no drafts may be written by a silent fallback"
    assert "offline template provider" not in res.combined and "placeholder hooks" not in res.combined
    assert "Traceback" not in res.combined
    assert res.net_attempts == ""
    assert not (kit.workdir / "drafts").exists() or list((kit.workdir / "drafts").rglob("*.md")) == []


@pytest.mark.parametrize("provider,env,variable", MODEL_CASES, ids=["openai", "anthropic"])
def test_providers_shows_configured_no_and_the_missing_variable_for_a_key_without_a_model(kit, provider, env, variable):
    res = kit.cli("providers", env=env)
    assert res.code == 0, res.combined
    line = line_for(res.out, provider)
    assert line and "configured=no" in line and variable in line, line
    assert list(env.values())[0] not in res.combined


@pytest.mark.parametrize("provider,env,variable", MODEL_CASES, ids=["openai", "anthropic"])
def test_providers_no_longer_flags_a_provider_once_its_model_is_set(kit, provider, env, variable):
    without = line_for(kit.cli("providers", env=env).out, provider)
    res = kit.cli("providers", env={**env, variable: "my-model-name"})
    assert res.code == 0, res.combined
    line = line_for(res.out, provider)
    assert line and "configured=no" not in line, line
    assert line != without


def test_a_model_without_a_key_is_not_configured_either(kit):
    res = kit.cli("providers", env={"OPENAI_MODEL": "my-model-name"})
    line = line_for(res.out, "openai")
    assert line and "configured=no" in line, line


def test_the_publik_provider_still_needs_only_its_key(kit):
    res = kit.cli("providers", env={"PUBLIK_API_KEY": KEY})
    assert res.code == 0, res.combined
    line = line_for(res.out, "publik")
    assert line and "configured=no" not in line, line


# ----- 21.7 messages ------------------------------------------------------------------------------------------------------------------------------------

WROTE_LINE = re.compile(r"^wrote (\S+)/ \((\d+) drafts, (\d+) blocked, index\.jsonl\)$")


def wrote_counts(res):
    lines = [ln for ln in stripped_lines(res.err) if ln.startswith("wrote ")]
    assert len(lines) == 1, res.err
    match = WROTE_LINE.match(lines[0])
    assert match, lines[0]
    return match.group(1), int(match.group(2)), int(match.group(3))


def test_the_wrote_line_counts_ok_and_blocked_drafts_separately(kit):
    rows = [person(kit, "ada", "Ada Lovelace"), person(kit, "zed", "Zed Zero", fact=""), person(kit, "bob", "Bob Byte"),
            person(kit, "amy", "Amy Zip", fact=""), person(kit, "cy", "Cy Young")]
    assert wrote_counts(run_draft(kit, rows, "--offline")) == ("drafts", 3, 2)


def test_the_wrote_line_with_nothing_blocked_says_zero_blocked(kit):
    res = run_draft(kit, [person(kit, "ada", "Ada Lovelace"), person(kit, "bob", "Bob Byte")], "--offline")
    assert wrote_counts(res) == ("drafts", 2, 0)


def test_the_wrote_line_with_everything_blocked_says_zero_drafts(kit):
    res = run_draft(kit, [person(kit, "zed", "Zed Zero", fact=""), person(kit, "amy", "Amy Zip", fact="")], "--offline")
    assert res.code == 0, res.combined
    assert wrote_counts(res) == ("drafts", 0, 2)


def test_the_wrote_line_describes_this_run_and_not_the_folder_history(kit):
    people_3 = [person(kit, "ada", "Ada Lovelace"), person(kit, "bob", "Bob Byte"), person(kit, "cy", "Cy Young")]
    assert wrote_counts(run_draft(kit, people_3, "--offline")) == ("drafts", 3, 0)
    assert wrote_counts(run_draft(kit, people_3[:2], "--offline")) == ("drafts", 2, 0)


def test_a_strict_run_writes_every_draft_before_it_exits_2(kit):
    res = run_draft(kit, [person(kit, "ada", "Ada Lovelace"), person(kit, "zed", "Zed Zero", fact="")], "--offline", "--strict")
    assert res.code == 2, res.combined
    out = kit.workdir / "drafts"
    assert (out / "ada.md").exists() and (out / "blocked" / "zed.md").exists() and (out / "index.jsonl").exists()
    lines = [ln for ln in stripped_lines(res.err) if ln.startswith("wrote ")]
    if lines:
        assert lines == ["wrote drafts/ (1 drafts, 1 blocked, index.jsonl)"]


def test_init_dir_pointing_at_a_file_exits_2_with_the_exact_message_shape(kit):
    kit.write_raw("afile", "i am a file")
    res = kit.cli("init", "--dir", "afile")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?cannot use --dir \S*afile: \S.*$", res.combined, re.M), res.combined
    assert "Traceback" not in res.combined
    assert (kit.workdir / "afile").read_text(encoding="utf-8") == "i am a file"
    assert not (kit.workdir / "contacts.sample.csv").exists()


def test_init_dir_below_a_file_cannot_be_created_and_exits_2(kit):
    kit.write_raw("afile", "x")
    res = kit.cli("init", "--dir", "afile/sub")
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?cannot use --dir \S*afile/sub: \S.*$", res.combined, re.M), res.combined
    assert "Traceback" not in res.combined


def test_init_dir_creates_a_missing_directory(kit):
    res = kit.cli("init", "--dir", "fresh/nested")
    assert res.code == 0, res.combined
    target = kit.workdir / "fresh" / "nested"
    assert sorted(p.name for p in target.iterdir()) == [".env.example", "contacts.sample.csv", "sender.sample.json"]


def test_lint_of_two_blocked_drafts_counts_two(kit):
    run_draft(kit, [person(kit, "zed", "Zed Zero", fact=""), person(kit, "amy", "Amy Zip", fact="")], "--offline")
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 2, res.combined
    pattern = r"^(?:vcoutreach: )?no drafts found in drafts; 2 blocked draft\(s\) are in drafts/blocked$"
    assert re.search(pattern, res.combined, re.M), res.combined


def test_lint_names_the_blocked_folder_under_a_custom_directory(kit):
    run_draft(kit, [person(kit, "zed", "Zed Zero", fact="")], "--offline", "--out", "mine")
    res = kit.cli("lint", "--drafts", "mine")
    assert res.code == 2, res.combined
    assert re.search(r"no drafts found in mine; 1 blocked draft\(s\) are in mine/blocked$", res.combined, re.M), res.combined


def test_lint_with_an_empty_blocked_folder_adds_no_suffix(kit):
    (kit.workdir / "nothing" / "blocked").mkdir(parents=True)
    res = kit.cli("lint", "--drafts", "nothing")
    assert res.code == 2, res.combined
    assert "no drafts found" in res.combined and "blocked draft(s)" not in res.combined


def test_lint_with_ok_and_blocked_drafts_has_no_no_drafts_line(kit):
    kit.quickstart()
    res = kit.cli("lint", "--drafts", "drafts")
    assert res.code == 0 and "no drafts found" not in res.combined and "blocked draft(s)" not in res.combined


def test_send_with_no_drafts_prints_no_missing_sender_note(kit):
    (kit.workdir / "nothing").mkdir()
    res = kit.cli("send", "--drafts", "nothing", env=SEND_ENV)
    assert res.code == 2, res.combined
    assert re.search(r"^(?:vcoutreach: )?no drafts found in \S*nothing$", res.combined, re.M), res.combined
    assert NO_SENDER_NOTE not in res.combined and "no --sender given" not in res.combined


def test_send_with_only_blocked_drafts_prints_no_missing_sender_note_either(kit):
    run_draft(kit, [person(kit, "zed", "Zed Zero", fact="")], "--offline")
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 2, res.combined
    assert "no drafts found in drafts; 1 blocked draft(s) are in drafts/blocked" in res.combined
    assert NO_SENDER_NOTE not in res.combined and "no --sender given" not in res.combined


def test_send_with_drafts_still_prints_the_missing_sender_note_once(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert stripped_lines(res.err).count(NO_SENDER_NOTE) == 1


def test_send_limit_zero_plans_nothing(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--limit", "0", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "me@mail.test" not in res.out, "a limit of 0 must not be read as no limit"
    assert "Traceback" not in res.combined
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()
    assert res.net_attempts == ""
