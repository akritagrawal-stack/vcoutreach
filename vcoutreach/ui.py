"""Loopback-only browser workspace. Email sending is never exposed here."""
from __future__ import annotations

import csv
import errno
import io
import json
import os
import re
import secrets
import tempfile
import threading
import webbrowser
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import Settings, load_settings, parse_dotenv
from .contacts import _header_name, _ID_RE, _MESSAGE_ID_RE, load_contacts
from .drafts_io import _safe_name, read_draft, read_index_ids, write_draft, write_index
from .journal import Journal
from .lint import _before_signature, lint_draft
from .pipeline import draft_all
from .providers import PROVIDER_NAMES, configured_providers, missing_variable, resolve_provider
from .providers.http import clean_api_key, normalize_base_url
from .send.gate import _header_problem, plan_sends
from .sender import load_sender
from .types import ContactsError, Draft, Hook, Lane, LintIssue, OutreachError, ProviderError, SenderProfile

MAX_BODY = 2_000_000
CONTACT_HEADER = (
    "id", "name", "email", "org", "role", "facts", "source_urls", "lane",
    "thread_subject", "in_reply_to", "references", "first_name",
)
KEY_NAMES = (
    "PUBLIK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_MODEL", "ANTHROPIC_MODEL",
)
NOTICE = "Nothing is sent from this screen. To send, use the command line (see README)."
GENERATION_CODES = frozenset({
    "no_facts", "insufficient_facts", "empty_hook", "false_parallel",
    "unapproved_number", "ungrounded_claim", "odd_greeting", "blocked",
})
CSP = (
    "default-src 'self'; script-src 'nonce-UI_NONCE'; style-src 'nonce-UI_NONCE'; "
    "connect-src 'self'; img-src 'none'; font-src 'none'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


def _one_line(value: object) -> str:
    return " ".join(str(value).replace("\u2014", ",").replace("\u2013", "-").split())


class APIError(OutreachError):
    def __init__(self, message: str, status: int = 400, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.details = details


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise APIError("Expected a JSON object.")
    return value


def _text(data: dict[str, Any], name: str, *, required: bool = False) -> str:
    value = data.get(name, None if required else "")
    if not isinstance(value, str):
        raise APIError(f"{name} must be text.")
    return value


def _row_errors(exc: OutreachError) -> list[dict[str, Any]]:
    errors = []
    for line in str(exc).splitlines():
        match = re.search(r"rows? (\d+)(?: and (\d+))?", line)
        rows = [int(n) for n in match.groups() if n] if match else [None]
        for row in rows:
            errors.append({"row": row, "message": _one_line(line)})
    return errors


class Workspace:
    def __init__(self, directory: Path) -> None:
        self.root = directory.expanduser().resolve()
        if not self.root.is_dir():
            raise OutreachError(f"workspace directory not found: {directory}")
        self.lock = threading.RLock()
        self.secret_values: set[str] = set()
        self.remember_secrets()

    def path(self, relative: str) -> Path:
        candidate = self.root / relative
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.root)
            current = candidate
            while current != self.root:
                if current.is_symlink():
                    raise APIError("Workspace files must not be symbolic links.")
                current = current.parent
        except (ValueError, RuntimeError):
            raise APIError("Path must stay inside the workspace.") from None
        return candidate

    def guard_drafts(self) -> Path:
        folder = self.path("drafts")
        self.path("drafts/blocked")
        if folder.exists() and not folder.is_dir():
            raise APIError("drafts must be a directory.")
        for parent, directories, files in os.walk(folder, followlinks=False):
            for name in directories + files:
                self.path(str((Path(parent) / name).relative_to(self.root)))
        return folder

    def remember_secrets(self) -> None:
        values = dict(os.environ)
        path = self.path(".env")
        if path.exists():
            saved = parse_dotenv(path.read_text(encoding="utf-8-sig"))
            values = {**saved, **values}
            # Remember file values even when an environment variable takes precedence.
            for name, value in saved.items():
                if name.endswith("_API_KEY") and value:
                    self.secret_values.add(value)
        self.secret_values.update(
            value for name, value in values.items() if name.endswith("_API_KEY") and value
        )

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for key in sorted(self.secret_values, key=len, reverse=True):
                value = value.replace(key, "[redacted]")
            return value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        return value

    def settings(self) -> Settings:
        self.remember_secrets()
        return replace(load_settings(dotenv_path=self.path(".env")), workdir=self.root)

    def provider_state(self) -> dict[str, Any]:
        settings = self.settings()
        auto = next((name for name in ("publik", "openai", "anthropic")
                     if getattr(settings, name + "_api_key")), "template")
        configured = configured_providers(settings)
        missing = missing_variable(settings, auto)
        reason = (
            "No API key is set. The free template writes placeholder text for you to review."
            if auto == "template" else
            "Automatic choice uses the first key in this order: Publik, OpenAI, Anthropic."
        )
        if missing:
            reason += f" Set {missing} before writing drafts with this provider."
        return {
            "auto_choice": auto,
            "configured": {name: name in configured for name in PROVIDER_NAMES if name != "auto"},
            "keys": {name: "set" if getattr(settings, name.lower()) else "not set"
                     for name in KEY_NAMES},
            "reason": reason,
        }

    def validate(self, filename: str, text: str, loader: Any) -> Any:
        with tempfile.TemporaryDirectory(prefix=".vcoutreach-ui-", dir=self.root) as scratch:
            path = Path(scratch) / filename
            path.write_text(text, encoding="utf-8")
            return loader(path)

    def write_text(self, relative: str, text: str) -> None:
        target = self.path(relative)
        fd, name = tempfile.mkstemp(prefix=".vcoutreach-ui-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            self.path(relative)
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)

    def sender(self) -> dict[str, Any]:
        path = self.path("sender.json")
        return asdict(load_sender(path)) if path.exists() else {}

    def save_sender(self, data: Any) -> dict[str, Any]:
        text = json.dumps(_object(data), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        try:
            sender = self.validate("sender.json", text, load_sender)
        except OutreachError as exc:
            message = _one_line(exc)
            match = re.search(r"field '([^']+)'|'([^']+)'", message)
            field = next((part for part in match.groups() if part), "") if match else ""
            if "approved_claims" in message:
                field = "approved_claims"
            raise APIError(message, 422, errors=[{"field": field, "message": message}]) from None
        self.write_text("sender.json", text)
        return asdict(sender)

    def parse_contacts(self, text: str) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        try:
            self.validate("contacts.csv", text, load_contacts)
        except ContactsError as exc:
            errors = _row_errors(exc)
        try:
            records = list(csv.reader(io.StringIO(text.lstrip("\ufeff"), newline="")))
        except csv.Error:
            return {"rows": [], "errors": errors, "row_numbers": []}
        start = next((i for i, row in enumerate(records) if any(c.strip() for c in row)), None)
        rows, numbers = [], []
        if start is not None:
            header = [_header_name(cell) for cell in records[start]]
            for number, cells in enumerate(records[start + 1:], start + 2):
                if any(cell.strip() for cell in cells):
                    rows.append({key: dict(zip(header, cells)).get(key, "") for key in CONTACT_HEADER})
                    numbers.append(number)
        return {"rows": rows, "errors": errors, "row_numbers": numbers}

    def contacts(self) -> dict[str, Any]:
        path = self.path("contacts.csv")
        if not path.exists():
            return {"rows": [], "errors": [], "row_numbers": []}
        return self.parse_contacts(path.read_text(encoding="utf-8-sig"))

    def contact_rows(self) -> list[dict[str, str]]:
        result = self.contacts()
        if result["errors"]:
            raise APIError(result["errors"][0]["message"], 422, **result)
        return result["rows"]

    def save_contacts(self, data: Any) -> list[dict[str, str]]:
        if not isinstance(data, list):
            raise APIError("Expected an array of contact rows.")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=CONTACT_HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in data:
            _object(row)
            if any(not isinstance(value, str) for value in row.values()):
                raise APIError("Every contact field must be text.")
            writer.writerow(row)
        result = self.parse_contacts(output.getvalue())
        if result["errors"]:
            raise APIError(result["errors"][0]["message"], 422, errors=result["errors"])
        self.write_text("contacts.csv", output.getvalue())
        return result["rows"]

    def save_keys(self, data: Any) -> dict[str, Any]:
        data = _object(data)
        for name, value in data.items():
            if name.endswith("_API_KEY") and isinstance(value, str) and value:
                self.secret_values.add(value)
        if not data or any(name not in KEY_NAMES for name in data):
            raise APIError("Use only the supported provider setting names.")
        for name, value in data.items():
            if not isinstance(value, str):
                raise APIError(f"{name} must be text.")
            if not re.fullmatch(r"[A-Za-z0-9_./:@%+=,?&~\[\]-]*", value):
                raise APIError(f"{name} contains unsafe .env characters; use letters, digits or URL punctuation without whitespace, quotes or shell syntax.")
            if value and name.endswith("_API_KEY"):
                clean_api_key(value, name)
            if value and name == "OPENAI_BASE_URL":
                try:
                    normalize_base_url(value, name)
                except (ProviderError, ValueError):
                    raise APIError(f"{name} must be a full http(s) URL with a valid host.") from None
        path = self.path(".env")
        lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True) if path.exists() else []
        remaining = dict(data)
        updated = []
        for line in lines:
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            name = match.group(1) if match else ""
            if name not in data:
                updated.append(line)
                continue
            # Preserve inline comments, including comments on duplicate assignments.
            tail = line[match.end():].rstrip("\r\n")
            comment = re.search(r"\s+#.*$", tail)
            if tail.lstrip().startswith(("'", '"')):
                quote = tail.lstrip()[0]
                end = tail.find(quote, tail.index(quote) + 1)
                comment = re.search(r"\s*#.*$", tail[end + 1:]) if end >= 0 else None
            suffix = comment.group() if comment else ""
            updated.append(f"{name}='{data[name]}'{suffix}\n")
            remaining.pop(name, None)
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated[-1] += "\n"
        updated.extend(f"{name}='{value}'\n" for name, value in remaining.items())
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write("".join(updated))
            handle.truncate()
        return self.provider_state()

    def samples(self) -> dict[str, Any]:
        examples = resources.files("vcoutreach").joinpath("data").joinpath("examples")
        created = []
        for source, target in (("contacts.sample.csv", "contacts.csv"),
                               ("sender.sample.json", "sender.json")):
            destination = self.path(target)
            if destination.exists():
                continue
            try:
                with destination.open("xb") as handle:
                    handle.write(examples.joinpath(source).read_bytes())
            except FileExistsError:
                continue
            created.append(target)
        return {"created": created}

    def drafts(self, directory: Path | None = None) -> list[Draft]:
        folder = self.guard_drafts() if directory is None else directory
        index = folder / "index.jsonl"
        entries: dict[str, Any] = {}
        if index.exists():
            try:
                for line in index.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    if isinstance(row, dict) and isinstance(row.get("contact_id"), str) and _ID_RE.fullmatch(row["contact_id"]):
                        entries[_safe_name(row["contact_id"])] = row
            except (ValueError, UnicodeDecodeError):
                entries = {}
        drafts = []
        for parent in (folder, folder / "blocked"):
            if not parent.is_dir():
                continue
            for path in sorted(parent.glob("*.md")):
                if not _ID_RE.fullmatch(path.stem) or not path.is_file():
                    continue
                draft = read_draft(path)
                row = entries.get(path.stem, {})
                same = all(row.get(key) == getattr(draft, key) for key in ("to", "subject", "body", "status"))
                issues = []
                if same:
                    indexed_issues = row.get("issues", [])
                    issues = [LintIssue(i["code"], i["message"],
                                       "warning" if i.get("severity") == "warning" else "error")
                              for i in (indexed_issues if isinstance(indexed_issues, list) else [])
                              if isinstance(i, dict) and isinstance(i.get("code"), str)
                              and isinstance(i.get("message"), str)]
                    hook = Hook(row.get("hook_text", ""), row.get("hook_provider", ""),
                                row.get("hook_model", ""))
                    draft = replace(draft, contact_id=row["contact_id"], hook=hook)
                if parent.name == "blocked":
                    draft = replace(draft, status="blocked")
                    if not issues:
                        issues = [LintIssue("blocked", "This draft was blocked during generation. Add verified contact facts and write it again, or edit and save it for a fresh check.")]
                drafts.append(replace(draft, issues=tuple(issues)))
        return drafts

    def checked(self, draft: Draft, *, edited: bool = False, saving: bool = False) -> Draft:
        sender = load_sender(self.path("sender.json"))
        issues = list(lint_draft(draft.subject, draft.body, sender, None))
        if not edited:
            for issue in draft.issues:
                if issue.code in GENERATION_CODES and issue not in issues:
                    issues.append(issue)
        problem = _header_problem(draft)
        if problem:
            issues.append(LintIssue("invalid_header", problem))
        if draft.lane == Lane.FOLLOW_UP and not _MESSAGE_ID_RE.fullmatch(draft.in_reply_to):
            issues.append(LintIssue("missing_thread", "A follow-up needs the original message id, such as <id@host>, to stay in its thread."))
        if not draft.subject.strip():
            issues.append(LintIssue("empty_subject", "The subject is empty."))
        if not draft.body.strip():
            issues.append(LintIssue("empty_body", "The draft has no message. Add verified facts to the contact and write it again, or enter a message here."))
        if draft.status != "ok" and not saving and not any(i.severity == "error" for i in issues):
            issues.append(LintIssue("needs_save", "This saved draft is blocked. Save and check it to apply the latest wording rules."))
        status = "blocked" if any(i.severity == "error" for i in issues) else "ok"
        return replace(draft, issues=tuple(issues), status=status)

    def draft_json(self, draft: Draft, *, full: bool = False) -> dict[str, Any]:
        draft = self.checked(draft)
        sender = load_sender(self.path("sender.json"))
        data = {
            "id": draft.contact_id, "to": draft.to, "subject": draft.subject,
            "status": draft.status, "issues": [asdict(i) for i in draft.issues],
            "word_count": sum(len(p.split()) for p in _before_signature(draft.body, sender)),
        }
        if full:
            data.update(body=draft.body, lane=draft.lane.value, in_reply_to=draft.in_reply_to,
                        references=draft.references, provider=draft.hook.provider if draft.hook else "")
        return data

    def lint(self, data: Any) -> dict[str, Any]:
        data = _object(data)
        if not data:
            return {"issues": [{"id": d.contact_id, **asdict(i)} for d in self.drafts()
                               for i in self.checked(d).issues]}
        body = _text(data, "body", required=True)
        subject = _text(data, "subject")
        path = self.path("sender.json")
        sender = load_sender(path) if path.exists() else SenderProfile(name="")
        return {"issues": [asdict(i) for i in lint_draft(subject, body, sender, None)]}

    def draft_id(self, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise APIError("Invalid draft id. Use letters, digits, dot, dash or underscore, starting with a letter or digit.")
        return value

    def edit_draft(self, contact_id: str, data: Any) -> dict[str, Any]:
        data = _object(data)
        drafts = self.drafts()
        draft = next((d for d in drafts if d.contact_id == contact_id), None)
        if draft is None:
            raise APIError("Draft not found.", 404)
        changes = {key: _text(data, key) for key in ("to", "subject", "body", "in_reply_to", "references")
                   if key in data}
        for key, value in changes.items():
            if key != "body" and any(ch in value for ch in "\r\n\x00"):
                raise APIError(f"{key} must be a single line.", 422)
        if "lane" in data:
            try:
                changes["lane"] = Lane(_text(data, "lane"))
            except ValueError:
                raise APIError("lane must be first_touch or follow_up.", 422) from None
        body_changed = "body" in changes and changes["body"] != draft.body
        draft = self.checked(replace(draft, **changes), edited=body_changed, saving=True)
        if body_changed and draft.hook:
            draft = replace(draft, hook=replace(draft.hook, text=""))
        write_draft(self.guard_drafts(), draft)
        write_index(self.guard_drafts(), [draft if d.contact_id == contact_id else d for d in drafts])
        return self.draft_json(draft, full=True)

    def generate(self, data: Any) -> dict[str, Any]:
        data = _object(data)
        choice = _text(data, "provider") or "auto"
        if choice not in PROVIDER_NAMES:
            raise APIError("Choose auto, template, publik, openai or anthropic.")
        contacts = load_contacts(self.path("contacts.csv"))
        if not contacts:
            raise APIError("No contacts found in contacts.csv. Add and save contacts first.", 422)
        stems = [_safe_name(c.id).casefold() for c in contacts]
        if len(set(stems)) != len(stems):
            raise APIError("Contact ids map to the same draft filename. Shorten the ids to make them unique.", 422)
        sender = load_sender(self.path("sender.json"))
        provider = resolve_provider(self.settings(), choice)
        folder = self.guard_drafts()
        selected = data.get("only_ids")
        if selected is not None:
            if not isinstance(selected, list) or not selected or any(not isinstance(i, str) for i in selected):
                raise APIError("only_ids must be a nonempty array of contact ids.")
            for contact_id in selected:
                self.draft_id(contact_id)
            unknown = set(selected) - {c.id for c in contacts}
            if unknown:
                raise APIError("only_ids contains an unknown contact id.")
            contacts = [c for c in contacts if c.id in selected]
            existing = self.drafts()
            # draft_all prunes its output. Stage a subset so unrelated drafts survive.
            with tempfile.TemporaryDirectory(prefix=".vcoutreach-ui-", dir=self.root) as scratch:
                try:
                    results = draft_all(contacts, sender, provider, Path(scratch))
                finally:
                    partial = self.drafts(Path(scratch))
                    if partial:
                        done = {_safe_name(d.contact_id).casefold() for d in partial}
                        for draft in partial:
                            write_draft(folder, draft)
                        write_index(folder, [d for d in existing if _safe_name(d.contact_id).casefold() not in done] + partial)
        else:
            # Validate indexed ids before the pipeline can prune old files.
            for contact_id in read_index_ids(folder):
                self.draft_id(contact_id)
            results = draft_all(contacts, sender, provider, folder)
        return {"drafted": sum(d.status == "ok" for d in results),
                "blocked": sum(d.status == "blocked" for d in results),
                "provider": provider.name, "results": [self.draft_json(d) for d in results]}

    def state(self) -> dict[str, Any]:
        contacts = self.contacts()
        drafts = self.drafts()
        sender_ready = False
        try:
            sender_ready = bool(self.sender())
        except OutreachError:
            pass
        if sender_ready:
            drafts = [self.checked(draft) for draft in drafts]
        return {
            "workspace": str(self.root),
            "files_present": {name: self.path(name).exists() for name in
                              ("contacts.csv", "sender.json", "drafts", ".env")},
            "provider": self.provider_state(),
            "counts": {"contacts": len(contacts["rows"]) if not contacts["errors"] else 0,
                       "drafts": sum(d.status == "ok" for d in drafts),
                       "blocked": sum(d.status == "blocked" for d in drafts)},
            "sender_ready": sender_ready,
        }

    def send_plan(self) -> dict[str, Any]:
        drafts = self.drafts()
        blocked = sum(d.status != "ok" for d in drafts)
        if not drafts:
            return {"planned": [], "skipped": [], "blocked": 0, "notice": NOTICE}
        journal = Journal(self.path(".vcoutreach/journal.jsonl"))
        report = plan_sends([d for d in drafts if d.status == "ok"], self.settings(),
                            load_sender(self.path("sender.json")), journal, execute=False)
        return {
            "planned": [{"action": "plan", "id": item.draft.contact_id, "mode": item.mode,
                         "to": item.to, "subject": item.subject} for item in report.planned],
            "skipped": [{"id": item.draft.contact_id, "reason": item.reason, "detail": item.detail}
                        for item in report.skipped],
            "blocked": blocked, "notice": NOTICE,
        }

    def dispatch(self, method: str, path: str, data: Any) -> Any:
        routes = {
            ("GET", "/api/state"): self.state,
            ("GET", "/api/sender"): self.sender,
            ("GET", "/api/contacts"): self.contact_rows,
            ("GET", "/api/drafts"): lambda: [self.draft_json(d) for d in self.drafts()],
            ("GET", "/api/send-plan"): self.send_plan,
            ("PUT", "/api/sender"): lambda: self.save_sender(data),
            ("PUT", "/api/contacts"): lambda: self.save_contacts(data),
            ("PUT", "/api/keys"): lambda: self.save_keys(data),
            ("POST", "/api/contacts/import"): lambda: self.parse_contacts(_text(_object(data), "csv_text", required=True)),
            ("POST", "/api/samples"): self.samples,
            ("POST", "/api/draft"): lambda: self.generate(data),
            ("POST", "/api/lint"): lambda: self.lint(data),
        }
        handler = routes.get((method, path))
        if handler:
            return handler()
        if path.startswith("/api/drafts/"):
            contact_id = self.draft_id(path[len("/api/drafts/"):])
            if method == "PUT":
                return self.edit_draft(contact_id, data)
            if method == "GET":
                draft = next((d for d in self.drafts() if d.contact_id == contact_id), None)
                if draft:
                    return self.draft_json(draft, full=True)
                raise APIError("Draft not found.", 404)
        known = any(route == path for _, route in routes) or path.startswith("/api/drafts/")
        raise APIError("Method not allowed." if known else "Route not found.", 405 if known else 404)


class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workspace: Workspace) -> None:
        self.workspace = workspace
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, UIHandler)


class UIHandler(BaseHTTPRequestHandler):
    server: UIServer
    server_version = "vcoutreach"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.respond(code, {"error": self.responses.get(code, ("Request rejected.", ""))[0]})

    def respond(self, status: int, data: Any, *, html: bool = False) -> None:
        nonce = secrets.token_urlsafe(24)
        if html:
            body = data.replace("UI_NONCE", nonce).encode("utf-8")
        else:
            body = json.dumps(self.server.workspace.redact(data), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP.replace("UI_NONCE", nonce))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def body_json(self, *, allow_empty: bool = False) -> Any:
        if self.headers.get_content_type() != "application/json":
            raise APIError("Content-Type must be application/json.", 415)
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise APIError("A single Content-Length is required.")
        length = int(lengths[0])
        if length >= MAX_BODY:
            raise APIError("JSON body must be under 2 MB.", 413)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise APIError("Incomplete request body.")
        if allow_empty and not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"), parse_constant=lambda value: self.bad_json())
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise APIError("Body must contain valid UTF-8 JSON.") from None

    @staticmethod
    def bad_json() -> Any:
        raise ValueError("Nonfinite JSON number")

    def handle_request(self) -> None:
        try:
            port = self.server.server_port
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                raise APIError("Host is not allowed.", 403)
            parts = urlsplit(self.path)
            if parts.scheme or parts.netloc:
                raise APIError("Use a local request path.")
            path = unquote(parts.path, errors="strict")
            if self.command == "GET" and path == "/api/health":
                self.respond(200, {"ok": True})
                return
            if self.command == "GET" and path == "/":
                page = resources.files("vcoutreach").joinpath("data").joinpath("ui").joinpath("index.html").read_text(encoding="utf-8")
                self.respond(200, page, html=True)
                return
            tokens = self.headers.get_all("X-Vcoutreach-Token", [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0].encode("utf-8"), self.server.token.encode("ascii")):
                raise APIError("Open the link printed by vcoutreach to connect to this workspace.", 403)
            data = (self.body_json(allow_empty=self.command == "POST" and path == "/api/lint")
                    if self.command in ("POST", "PUT") else None)
            with self.server.workspace.lock:
                self.server.workspace.remember_secrets()
                result = self.server.workspace.dispatch(self.command, path, data)
                self.respond(200, result)
        except APIError as exc:
            self.respond(exc.status, {"error": _one_line(exc), **exc.details})
        except ProviderError as exc:
            self.respond(502, {"error": "Draft provider failed. " + _one_line(exc) +
                              " Any completed drafts are saved. Review them, then try again."})
        except ContactsError as exc:
            self.respond(422, {"error": _one_line(exc), "errors": _row_errors(exc)})
        except OutreachError as exc:
            self.respond(422, {"error": _one_line(exc)})
        except (UnicodeError, ValueError, RecursionError):
            self.respond(400, {"error": "Invalid text or JSON. Check the input and try again."})
        except OSError:
            self.respond(500, {"error": "Cannot read or write a workspace file. Check its permissions and try again."})
        except Exception:
            self.respond(500, {"error": "The request could not be completed. Check the workspace files and try again."})

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request
    do_OPTIONS = handle_request
    do_HEAD = handle_request


def serve(workspace: Path, *, port: int | None = None, no_browser: bool = False) -> int:
    """Serve one workspace until Ctrl+C, returning the CLI exit code."""
    if port is not None and not 1 <= port <= 65535:
        raise OutreachError("--port must be between 1 and 65535")
    app = Workspace(workspace)
    ports = range(8765, 8786) if port is None else (port,)
    server = None
    for candidate in ports:
        try:
            server = UIServer(("127.0.0.1", candidate), app)
            break
        except OSError as exc:
            if port is None and exc.errno == errno.EADDRINUSE:
                continue
            reason = "port is already in use" if exc.errno == errno.EADDRINUSE else (exc.strerror or type(exc).__name__)
            raise OutreachError(f"cannot start ui on 127.0.0.1:{candidate}: {reason}") from None
    if server is None:
        raise OutreachError("cannot start ui: ports 8765 through 8785 are already in use")
    with server:
        url = f"http://127.0.0.1:{server.server_port}/#token={server.token}"
        print(f"Open this link: {url}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            if not no_browser:
                try:
                    webbrowser.open(url)
                except (OSError, webbrowser.Error):
                    pass
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
