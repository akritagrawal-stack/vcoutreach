"""Reading and writing draft files and the drafts index."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from .types import Draft, Hook, Lane, LintIssue, OutreachError

HEADER_KEYS = ("to", "subject", "status", "lane", "provider", "in_reply_to", "references", "issues")
_REQUIRED_KEYS = ("to", "subject", "status")


def _lane_value(lane: object) -> str:
    return lane.value if isinstance(lane, Lane) else str(lane)


def _safe_name(contact_id: str) -> str:
    """File stem for a contact id. Ids from load_contacts already pass through unchanged."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", contact_id)[:150]
    name = re.sub(r"^\.+", lambda m: "_" * len(m.group(0)), name)
    return name or "_"


def _unique_codes(issues: Iterable[LintIssue]) -> List[str]:
    """Issue codes with repeats removed, keeping first-seen order."""
    codes: List[str] = []
    for issue in issues:
        if issue.code not in codes:
            codes.append(issue.code)
    return codes


def _one_line(value: str) -> str:
    return " ".join(str(value).splitlines())


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp, path)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _remove_siblings(folder: Path, name: str, *, keep_exact: bool) -> None:
    """Delete files in folder whose name equals name ignoring case.

    With keep_exact the entry spelled exactly like name stays (it is about to be replaced
    atomically); without it every spelling goes. Best effort: a missing folder or a file
    that cannot be removed is ignored. Ids that differ only by case are rejected as
    duplicates when contacts load, so no other contact's file can match.
    """
    try:
        names = os.listdir(folder)
    except OSError:
        return
    wanted = name.casefold()
    for found in names:
        # The length test is a cheap filter: ids are ASCII, so equal-ignoring-case names
        # have equal length, and most entries in a large folder are rejected here.
        if len(found) != len(name) or found.casefold() != wanted:
            continue
        if keep_exact and found == name:
            continue
        target = os.path.join(folder, found)
        try:
            if not os.path.isdir(target) or os.path.islink(target):
                os.unlink(target)
        except OSError:
            pass


def write_draft(out_dir: Path, draft: Draft) -> Path:
    """Write one draft file. ok drafts go to out_dir, blocked ones to out_dir/blocked.

    The file name always has the exact case of the id. A differently-cased copy left by an
    earlier run (``ines-marlowe.md`` when the id is now ``Ines-Marlowe``) is removed BEFORE
    the write: on a case-insensitive disk the old and new names are the same file, so
    writing first and deleting after would delete the new file or keep the old spelling.
    """
    out_dir = Path(out_dir)
    stem = _safe_name(draft.contact_id) + ".md"
    ok_path = out_dir / stem
    blocked_path = out_dir / "blocked" / stem
    path, stale = (ok_path, blocked_path) if draft.status == "ok" else (blocked_path, ok_path)

    provider = draft.hook.provider if draft.hook is not None else ""
    header = [
        ("to", draft.to),
        ("subject", draft.subject),
        ("status", draft.status),
        ("lane", _lane_value(draft.lane)),
        ("provider", provider),
        ("in_reply_to", draft.in_reply_to),
        ("references", draft.references),
        ("issues", "; ".join(_unique_codes(draft.issues))),
    ]
    lines = []
    for key, value in header:
        value = _one_line(value)
        lines.append("%s: %s" % (key, value) if value else "%s:" % key)
    # One blank line, the body, then exactly one trailing newline (read_draft strips it again).
    text = "\n".join(lines) + "\n\n" + draft.body + "\n"
    _remove_siblings(path.parent, path.name, keep_exact=True)
    _atomic_write(path, text)

    # A re-run must not leave an older copy of this contact in the other folder
    # (for example a stale "ok" draft after the contact became blocked), in any case.
    _remove_siblings(stale.parent, stale.name, keep_exact=False)
    return path


def read_draft(path: Path) -> Draft:
    """Parse a draft file. issues is empty; status comes from the header.

    The body loses exactly one trailing newline (the one write_draft added), so write then
    read returns the original body. hook is None unless the provider header is non-empty;
    then it is Hook(text="", provider=<header value>) so callers can see who wrote it.
    """
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        raise OutreachError("cannot read draft %s: %s" % (path, exc.strerror or exc.__class__.__name__))
    except UnicodeDecodeError:
        raise OutreachError("draft %s is not valid UTF-8 text" % path)

    lines = text.split("\n")
    header: Dict[str, str] = {}
    cursor = 0
    while cursor < len(lines) and lines[cursor].strip() != "":
        key, sep, value = lines[cursor].partition(":")
        if sep:
            header[key.strip().lower()] = value[1:] if value.startswith(" ") else value
        cursor += 1
    body = "\n".join(lines[cursor + 1:]) if cursor < len(lines) else ""
    if body.endswith("\n"):
        body = body[:-1]

    missing = [key for key in _REQUIRED_KEYS if key not in header]
    if missing:
        raise OutreachError(
            "draft %s is not a vcoutreach draft (missing header line(s): %s); "
            "move it out of the drafts folder or fix its header" % (path.name, ", ".join(missing))
        )

    lane_text = header.get("lane", "").strip()
    try:
        lane = Lane(lane_text) if lane_text else Lane.FIRST_TOUCH
    except ValueError:
        raise OutreachError("draft %s has an unknown lane %r" % (path.name, lane_text))

    provider = header.get("provider", "").strip()
    return Draft(
        contact_id=path.stem,
        to=header["to"].strip(),
        subject=header["subject"].strip(),
        body=body,
        status=header["status"].strip(),
        hook=Hook(text="", provider=provider) if provider else None,
        issues=(),
        lane=lane,
        in_reply_to=header.get("in_reply_to", "").strip(),
        references=header.get("references", "").strip(),
    )


def read_drafts(drafts_dir: Path) -> List[Draft]:
    """Read the top level .md drafts (never blocked/), sorted by filename."""
    drafts_dir = Path(drafts_dir)
    if not drafts_dir.is_dir():
        raise OutreachError("drafts directory not found: %s" % drafts_dir)
    paths = sorted(
        (p for p in drafts_dir.iterdir() if p.is_file() and p.suffix == ".md"),
        key=lambda p: p.name,
    )
    return [read_draft(p) for p in paths]


def write_index(out_dir: Path, drafts: Sequence[Draft]) -> Path:
    """Write out_dir/index.jsonl with one JSON object per draft, in the given order."""
    out_dir = Path(out_dir)
    lines = []
    for draft in drafts:
        hook = draft.hook
        row = {
            "contact_id": draft.contact_id,
            "to": draft.to,
            "subject": draft.subject,
            "body": draft.body,
            "status": draft.status,
            "lane": _lane_value(draft.lane),
            "in_reply_to": draft.in_reply_to,
            "references": draft.references,
            "hook_text": hook.text if hook is not None else "",
            "hook_provider": hook.provider if hook is not None else "",
            "hook_model": hook.model if hook is not None else "",
            "issues": [
                {"code": i.code, "message": i.message, "severity": i.severity}
                for i in draft.issues
            ],
        }
        lines.append(json.dumps(row, ensure_ascii=False))
    path = out_dir / "index.jsonl"
    _atomic_write(path, "".join(line + "\n" for line in lines))
    return path


def read_index_ids(out_dir: Path) -> List[str]:
    """The contact ids listed in out_dir/index.jsonl, in file order.

    A missing, unreadable or unparseable file (any line that is not a JSON object with a
    string contact_id) gives an empty list, so a damaged index never leads to a deletion.
    """
    path = Path(out_dir) / "index.jsonl"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw_lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    ids: List[str] = []
    seen = set()
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return []
        if not isinstance(row, dict) or not isinstance(row.get("contact_id"), str):
            return []
        if row["contact_id"] not in seen:
            seen.add(row["contact_id"])
            ids.append(row["contact_id"])
    return ids


def prune_stale(out_dir: Path, previous_ids: Iterable[str], current_ids: Iterable[str]) -> List[str]:
    """Delete <id>.md from out_dir and out_dir/blocked for each previous id not in current_ids.

    Only ids that pass the same safe-name check used for writing (the id is its own file
    stem) are touched. Stems are compared case-folded, because on a case-insensitive file
    system ``ines-marlowe.md`` and ``Ines-Marlowe.md`` are the same file: a previous id is
    stale only if its folded stem differs from the folded stem of every current id, so a
    case-only rename never deletes the file the new run wrote. Returns the ids for which at
    least one file was removed, in the order of previous_ids.
    """
    out_dir = Path(out_dir)
    current_stems = {_safe_name(i).casefold() for i in current_ids if isinstance(i, str)}
    if isinstance(previous_ids, (set, frozenset)):
        ordered: List[str] = sorted(i for i in previous_ids if isinstance(i, str))
    else:
        ordered = []
        listed = set()
        for item in previous_ids:
            if isinstance(item, str) and item not in listed:
                listed.add(item)
                ordered.append(item)

    removed: List[str] = []
    for contact_id in ordered:
        stem = _safe_name(contact_id)
        if stem != contact_id:
            continue
        if stem.casefold() in current_stems:
            continue
        gone = False
        for folder in (out_dir, out_dir / "blocked"):
            target = folder / (contact_id + ".md")
            try:
                if target.is_file():
                    target.unlink()
                    gone = True
            except OSError:
                pass
        if gone:
            removed.append(contact_id)
    return removed
