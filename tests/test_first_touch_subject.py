"""The optional sender field `first_touch_subject` (black-box, through `vcoutreach draft`).

Written from the feature spec and the public README only. Every test runs the real CLI in a
subprocess inside tmp_path, then reads the files it wrote, the way a person would.

No dash characters appear in this file: EM_DASH and EN_DASH are built from code points.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "vcoutreach" / "data" / "examples"
SAMPLE_CSV = EXAMPLES / "contacts.sample.csv"
SAMPLE_SENDER = EXAMPLES / "sender.sample.json"

CSV_HEADER = [
    "id", "name", "email", "org", "role", "facts", "source_urls",
    "lane", "thread_subject", "in_reply_to", "references", "first_name", "subject",
]
FIELD = "first_touch_subject"


# ----- plumbing ---------------------------------------------------------------------------------------------------

def run_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd / "home"),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "LC_ALL": "C.UTF-8",
    }
    return subprocess.run(
        [sys.executable, "-m", "vcoutreach", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def sample_sender() -> Dict[str, Any]:
    return json.loads(SAMPLE_SENDER.read_text(encoding="utf-8"))


def write_sender(path: Path, **extra: Any) -> Path:
    data = sample_sender()
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def row(**kw: str) -> Dict[str, str]:
    base = {k: "" for k in CSV_HEADER}
    base["lane"] = "first_touch"
    base.update(kw)
    return base


def write_contacts(path: Path, rows: List[Dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


ADA = row(id="ada", name="Ada Lovelace", email="ada@example.com", org="Engine Works",
          facts="your 2024 essay on analytical engines", source_urls="https://example.com/ada")
BO = row(id="bo", name="Bo Chen", email="bo@example.com", org="Ledger Co",
         facts="your 2025 talk on double entry ledgers", source_urls="https://example.com/bo")
BOB = row(id="rob", name="Robert Smith", first_name="Bob", email="rob@example.com",
          facts="your 2025 post on message queues", source_urls="https://example.com/rob")
FOLLOW = row(id="cy", name="Cy Dunn", email="cy@example.com", lane="follow_up",
             facts="your 2025 post on job queues", source_urls="https://example.com/cy",
             thread_subject="Re: the earlier thing", in_reply_to="<thread-9@example.com>")
OWN = row(id="own", name="Dee Fox", email="dee@example.com", subject="My own explicit subject",
          facts="your 2025 note on spreadsheets", source_urls="https://example.com/dee")


def header(path: Path, key: str) -> str:
    """Read one header line from the top of a written draft (`key: value`)."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + ":"):
            return line[len(key) + 1:].strip()
        if not line.strip():
            break
    raise AssertionError("no %r header in %s" % (key, path))


def subject_of(out: Path, contact_id: str) -> str:
    return header(out / (contact_id + ".md"), "subject")


def draft(tmp_path: Path, rows: List[Dict[str, str]], sender_extra: Optional[Dict[str, Any]],
          name: str = "run") -> "tuple[subprocess.CompletedProcess, Path]":
    work = tmp_path / name
    work.mkdir()
    contacts = write_contacts(work / "contacts.csv", rows)
    extra = {} if sender_extra is None else sender_extra
    sender = write_sender(work / "sender.json", **extra)
    out = work / "drafts"
    proc = run_cli(work, "draft", "--contacts", str(contacts), "--sender", str(sender),
                   "--offline", "--out", str(out))
    return proc, out


# ----- the field is used -------------------------------------------------------------------------------------------

def test_first_touch_subject_replaces_the_default_subject(tmp_path):
    proc, out = draft(tmp_path, [ADA, BO], {FIELD: "An idea for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "ada") == "An idea for Ada"
    assert subject_of(out, "bo") == "An idea for Bo"


def test_subject_without_placeholder_is_used_verbatim_for_everyone(tmp_path):
    proc, out = draft(tmp_path, [ADA, BO], {FIELD: "Thoughts on your week"})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "ada") == "Thoughts on your week"
    assert subject_of(out, "bo") == "Thoughts on your week"


def test_a_forbidden_phrase_in_the_subject_blocks_the_draft_like_any_other_text(tmp_path):
    proc, out = draft(tmp_path, [ADA], {FIELD: "A game changer for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    assert not (out / "ada.md").exists()
    assert (out / "blocked" / "ada.md").exists()
    assert "drafted=0 blocked=1" in proc.stdout


def test_placeholder_uses_the_greeting_first_name_not_the_legal_name(tmp_path):
    proc, out = draft(tmp_path, [BOB], {FIELD: "For {first_name}"})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "rob") == "For Bob"
    body = (out / "rob.md").read_text(encoding="utf-8")
    assert "Hi Bob," in body


def test_placeholder_may_appear_twice(tmp_path):
    proc, out = draft(tmp_path, [ADA], {FIELD: "{first_name}, a note for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "ada") == "Ada, a note for Ada"


def test_non_ascii_first_name_survives_into_the_subject(tmp_path):
    zoe = row(id="zoe", name="Zoë Quill", email="zoe@example.com",
              facts="your 2025 essay on tidy inboxes", source_urls="https://example.com/zoe")
    proc, out = draft(tmp_path, [zoe], {FIELD: "A note for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "zoe") == "A note for Zoë"


def test_subject_in_index_jsonl_matches_the_draft_file(tmp_path):
    proc, out = draft(tmp_path, [ADA, BO], {FIELD: "An idea for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(l) for l in (out / "index.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {r["contact_id"]: r for r in rows}
    assert by_id["ada"]["subject"] == "An idea for Ada"
    assert by_id["bo"]["subject"] == "An idea for Bo"


def test_the_body_is_not_changed_by_the_subject_field(tmp_path):
    _, plain = draft(tmp_path, [ADA, BO], None, name="plain")
    _, custom = draft(tmp_path, [ADA, BO], {FIELD: "An idea for {first_name}"}, name="custom")
    for cid in ("ada", "bo"):
        a = (plain / (cid + ".md")).read_text(encoding="utf-8").split("\n\n", 1)[1]
        b = (custom / (cid + ".md")).read_text(encoding="utf-8").split("\n\n", 1)[1]
        assert a == b


def test_subject_of_exactly_120_characters_is_accepted(tmp_path):
    subject = "a" * 120
    proc, out = draft(tmp_path, [ADA], {FIELD: subject})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "ada") == subject


# ----- what stays the same ----------------------------------------------------------------------------------------

def test_contact_with_an_explicit_subject_keeps_it(tmp_path):
    proc, out = draft(tmp_path, [OWN, ADA], {FIELD: "An idea for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "own") == "My own explicit subject"
    assert subject_of(out, "ada") == "An idea for Ada"


def test_follow_up_keeps_the_thread_subject(tmp_path):
    proc, out = draft(tmp_path, [FOLLOW, ADA], {FIELD: "An idea for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "cy") == "Re: the earlier thing"
    assert header(out / "cy.md", "in_reply_to") == "<thread-9@example.com>"
    assert header(out / "cy.md", "lane") == "follow_up"
    assert subject_of(out, "ada") == "An idea for Ada"


def test_follow_up_is_identical_with_and_without_the_field(tmp_path):
    _, plain = draft(tmp_path, [FOLLOW], None, name="plain")
    _, custom = draft(tmp_path, [FOLLOW], {FIELD: "An idea for {first_name}"}, name="custom")
    assert (plain / "cy.md").read_text(encoding="utf-8") == (custom / "cy.md").read_text(encoding="utf-8")


def test_absent_field_gives_the_generic_subject(tmp_path):
    proc, out = draft(tmp_path, [ADA, BO, BOB], None)
    assert proc.returncode == 0, proc.stderr
    assert subject_of(out, "ada") == "a note for Ada"
    assert subject_of(out, "bo") == "a note for Bo"
    assert subject_of(out, "rob") == "a note for Bob"


def test_empty_field_is_the_same_as_absent(tmp_path):
    _, absent = draft(tmp_path, [ADA, BO], None, name="absent")
    proc, empty = draft(tmp_path, [ADA, BO], {FIELD: ""}, name="empty")
    assert proc.returncode == 0, proc.stderr
    for cid in ("ada", "bo"):
        assert (absent / (cid + ".md")).read_text(encoding="utf-8") == (empty / (cid + ".md")).read_text(encoding="utf-8")
    assert subject_of(empty, "ada") == "a note for Ada"


def test_packaged_sample_sender_does_not_define_the_field():
    assert FIELD not in sample_sender()
    assert FIELD not in SAMPLE_SENDER.read_text(encoding="utf-8")


def test_sample_drafting_is_unchanged(tmp_path):
    out = tmp_path / "out"
    proc = run_cli(tmp_path, "draft", "--contacts", str(SAMPLE_CSV), "--sender", str(SAMPLE_SENDER),
                   "--offline", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert "drafted=4 blocked=1" in proc.stdout
    assert (out / "blocked" / "rhea-castellane.md").exists()
    assert subject_of(out, "ines-marlowe") == "a note for Ines"
    assert subject_of(out, "tobias-wren") == "a note for Tobias"
    assert subject_of(out, "priya-ostrander") == "a note for Priya"
    assert subject_of(out, "odalys-brandt") == "a note for Odalys"


def test_sample_drafting_with_the_field_added_still_gives_four_and_one(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    sender = write_sender(work / "sender.json", **{FIELD: "An idea for {first_name}"})
    out = work / "out"
    proc = run_cli(work, "draft", "--contacts", str(SAMPLE_CSV), "--sender", str(sender),
                   "--offline", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert "drafted=4 blocked=1" in proc.stdout
    assert subject_of(out, "ines-marlowe") == "An idea for Ines"
    assert subject_of(out, "odalys-brandt") == "a note for Odalys"  # sample follow-up keeps its thread subject


# ----- validation ---------------------------------------------------------------------------------------------------

BAD_VALUES = [
    pytest.param("An idea " + EM_DASH + " for {first_name}", id="em_dash"),
    pytest.param("An idea " + EN_DASH + " for {first_name}", id="en_dash"),
    pytest.param("Hi {first_name}!", id="exclamation"),
    pytest.param("Hi\n{first_name}", id="newline"),
    pytest.param("a" * 121, id="121_chars"),
    pytest.param(5, id="integer"),
    pytest.param(["An idea for {first_name}"], id="list"),
]


@pytest.mark.parametrize("value", BAD_VALUES)
def test_invalid_values_exit_2_with_a_one_line_error_naming_the_field(tmp_path, value):
    proc, out = draft(tmp_path, [ADA], {FIELD: value})
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    lines = [l for l in proc.stderr.splitlines() if l.strip()]
    assert len(lines) == 1, proc.stderr
    assert FIELD in lines[0]
    assert "Traceback" not in proc.stderr
    assert not list(out.glob("*.md")) if out.exists() else True


def test_unknown_placeholder_is_rejected_or_never_left_in_a_subject(tmp_path):
    """Either a clear one-line error naming the field (preferred) or no raw `{company}` in any subject."""
    proc, out = draft(tmp_path, [ADA, BO], {FIELD: "About {company} for {first_name}"})
    if proc.returncode == 2:
        lines = [l for l in proc.stderr.splitlines() if l.strip()]
        assert len(lines) == 1, proc.stderr
        assert FIELD in lines[0]
        assert "{first_name}" in lines[0] or "first_name" in lines[0]
        assert "Traceback" not in proc.stderr
        return
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    for cid in ("ada", "bo"):
        subject = subject_of(out, cid)
        assert "{" not in subject and "}" not in subject, subject


# ----- lint on a hand-edited subject --------------------------------------------------------------------------------

def test_lint_flags_an_em_dash_added_to_a_subject_by_hand(tmp_path):
    proc, out = draft(tmp_path, [ADA], {FIELD: "An idea for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    path = out / "ada.md"
    text = path.read_text(encoding="utf-8")
    assert "subject: An idea for Ada" in text
    path.write_text(text.replace("subject: An idea for Ada", "subject: An idea " + EM_DASH + " for Ada", 1),
                    encoding="utf-8")
    sender = tmp_path / "run" / "sender.json"
    lint = run_cli(tmp_path / "run", "lint", "--drafts", str(out), "--sender", str(sender))
    assert lint.returncode == 1, (lint.stdout, lint.stderr)
    assert "em_dash" in lint.stdout
    assert "errors=1" in lint.stdout


def test_lint_is_clean_on_an_unedited_custom_subject_draft(tmp_path):
    proc, out = draft(tmp_path, [ADA, BO], {FIELD: "An idea for {first_name}"})
    assert proc.returncode == 0, proc.stderr
    sender = tmp_path / "run" / "sender.json"
    lint = run_cli(tmp_path / "run", "lint", "--drafts", str(out), "--sender", str(sender))
    assert lint.returncode == 0, (lint.stdout, lint.stderr)
    assert "errors=0" in lint.stdout
