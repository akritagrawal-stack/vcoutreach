"""Standard library command line interface for vcoutreach."""
from __future__ import annotations

import argparse
from importlib import resources
import os
from pathlib import Path
import sys
import traceback

from vcoutreach import __version__
from vcoutreach.types import OutreachError, ProviderError, SenderProfile


def _settings():
    from vcoutreach.config import load_settings

    return load_settings(
        dotenv_path=Path.cwd() / ".env",
        warn=lambda message: print(f"warning: {_one_line(message)}", file=sys.stderr),
    )


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _init(args: argparse.Namespace) -> int:
    directory = args.dir
    if directory.is_file():
        raise OutreachError(f"cannot use --dir {directory}: it is a file, not a folder")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        reason = exc.strerror or str(exc) or type(exc).__name__
        raise OutreachError(f"cannot use --dir {directory}: {reason}") from exc
    examples = resources.files("vcoutreach").joinpath("data").joinpath("examples")
    for source, target in (
        ("contacts.sample.csv", "contacts.sample.csv"),
        ("sender.sample.json", "sender.sample.json"),
        ("env.example", ".env.example"),
    ):
        destination = directory / target
        if destination.exists() or destination.is_symlink():
            print(f"skipped {destination} (already exists)")
            continue
        data = examples.joinpath(source).read_bytes()
        try:
            with destination.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            print(f"skipped {destination} (already exists)")
        else:
            print(f"created {destination}")
    return 0


def _draft(args: argparse.Namespace) -> int:
    from vcoutreach.contacts import load_contacts
    from vcoutreach.pipeline import draft_all
    from vcoutreach.providers import PROVIDER_NAMES, resolve_provider
    from vcoutreach.sender import load_sender

    choice = "template" if args.offline else (args.provider or "auto").strip().lower()
    if choice not in PROVIDER_NAMES:
        raise OutreachError(
            f"Unknown provider {args.provider[:40]!r}. Valid names: " + ", ".join(PROVIDER_NAMES)
        )
    settings = _settings()
    contacts = load_contacts(args.contacts)
    if not contacts:
        raise OutreachError(f"no contacts found in {args.contacts}")

    class ProgressContacts(list):
        finished = 0

        def __iter__(self):
            for contact in super().__iter__():
                yield contact
                # The pipeline resumes iteration after writing this contact's draft.
                self.finished += 1

    contacts = ProgressContacts(contacts)
    sender = load_sender(args.sender)
    provider = resolve_provider(settings, choice)
    if choice == "auto" and provider.name == "template":
        print(
            "warning: no provider key found (set PUBLIK_API_KEY, OPENAI_API_KEY or "
            "ANTHROPIC_API_KEY); using the offline template provider, which writes placeholder hooks.",
            file=sys.stderr,
        )
    try:
        drafts = draft_all(
            contacts, sender, provider, args.out,
            on_prune=lambda contact_id: print(
                f"removed stale draft {contact_id} (no longer in the contacts file)",
                file=sys.stderr,
            ),
        )
    except ProviderError:
        if contacts.finished:
            print(
                f"kept {contacts.finished} finished draft(s) in {_one_line(args.out)}/",
                file=sys.stderr,
            )
        raise
    except OSError as exc:
        reason = exc.strerror or str(exc) or type(exc).__name__
        raise OutreachError(f"cannot write to --out {args.out}: {reason}") from exc
    drafted = sum(draft.status == "ok" for draft in drafts)
    blocked = sum(draft.status == "blocked" for draft in drafts)
    print(f"drafted={drafted} blocked={blocked} provider={provider.name}")
    for draft in drafts:
        if draft.status == "blocked":
            codes = ", ".join(dict.fromkeys(issue.code for issue in draft.issues))
            print(f"blocked {_one_line(draft.contact_id)}: {_one_line(codes)}")
    for draft in drafts:
        if draft.status == "blocked":
            message = next((issue.message for issue in draft.issues if issue.message), None)
            if message:
                print(
                    f"note: {_one_line(draft.contact_id)} blocked: {_one_line(message)}",
                    file=sys.stderr,
                )
    warned = [
        draft for draft in drafts
        if draft.status == "ok" and any(issue.severity == "warning" for issue in draft.issues)
    ]
    if warned:
        codes = ", ".join(dict.fromkeys(
            issue.code for draft in warned for issue in draft.issues if issue.severity == "warning"
        ))
        print(
            f"note: {len(warned)} draft(s) have warnings ({codes}). "
            "See the issues: line at the top of each draft, or drafts/index.jsonl.",
            file=sys.stderr,
        )
    print(f"wrote {args.out}/ ({drafted} drafts, {blocked} blocked, index.jsonl)", file=sys.stderr)
    return 2 if args.strict and blocked else 0


def _lint(args: argparse.Namespace) -> int:
    from vcoutreach.drafts_io import read_draft
    from vcoutreach.lint import lint_draft
    from vcoutreach.sender import load_sender

    if args.drafts.is_file():
        raise OutreachError(f"--drafts expects a directory, not a file: {args.drafts}")
    if not args.drafts.is_dir():
        raise OutreachError(f"drafts directory not found: {args.drafts}")
    paths = sorted(
        (path for path in args.drafts.iterdir() if path.is_file() and path.suffix == ".md"),
        key=lambda path: path.name,
    )
    if not paths:
        raise OutreachError(_no_drafts_message(args.drafts))
    sender = load_sender(args.sender) if args.sender else SenderProfile(name="")
    errors = warnings = 0
    for path in paths:
        draft = read_draft(path)
        issues = lint_draft(draft.subject, draft.body, sender, None)
        for issue in issues:
            print(f"{path}: {issue.severity}: {issue.code}: {_one_line(issue.message)}")
        errors += sum(issue.severity == "error" for issue in issues)
        warnings += sum(issue.severity == "warning" for issue in issues)
    print(f"linted={len(paths)} errors={errors} warnings={warnings}")
    return 1 if errors else 0


def _providers(args: argparse.Namespace) -> int:
    from vcoutreach.providers import configured_providers
    from vcoutreach.providers.http import setting_text

    settings = _settings()
    configured = set(configured_providers(settings))
    for name in ("template", "publik", "openai", "anthropic"):
        missing_model = (
            name in ("openai", "anthropic")
            and setting_text(settings, f"{name}_api_key")
            and not setting_text(settings, f"{name}_model")
        )
        if missing_model:
            print(f"{name}\tconfigured=no (set {name.upper()}_MODEL)")
        else:
            print(f"{name}\tconfigured={'yes' if name in configured else 'no'}")
    return 0


def _no_drafts_message(directory: Path) -> str:
    message = f"no drafts found in {directory}"
    blocked_dir = directory / "blocked"
    if blocked_dir.is_dir():
        count = sum(path.is_file() for path in blocked_dir.iterdir())
        if count:
            message += f"; {count} blocked draft(s) are in {blocked_dir}"
    return message


def _one_line(value: object) -> str:
    return " ".join(str(value).replace("\u2014", "-").replace("\u2013", "-").split())


def _gmail_dependencies() -> None:
    """Check optional libraries without authorizing or opening a connection."""
    from importlib import import_module

    try:
        for module in (
            "google.auth.transport.requests", "google.oauth2.credentials",
            "google_auth_oauthlib.flow", "googleapiclient.discovery",
        ):
            import_module(module)
    except ImportError as exc:
        raise ProviderError(
            "Gmail sending needs optional Google libraries that are not installed. "
            "Run, from the repository folder: pip install -r requirements-gmail.txt "
            '(or pip install -e ".[gmail]")'
        ) from exc


class _DeferredGmailTransport:
    """Open the Gmail transport only when the send gate calls send."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.transport = None

    def send(self, message):
        if self.transport is None:
            _gmail_dependencies()
            from vcoutreach.send import GmailTransport

            self.transport = GmailTransport(self.settings)
        return self.transport.send(message)


def _send(args: argparse.Namespace) -> int:
    from vcoutreach.drafts_io import read_drafts
    from vcoutreach.journal import Journal
    from vcoutreach.send import SendGateError, plan_sends
    from vcoutreach.sender import load_sender

    if args.drafts.is_file():
        raise OutreachError(f"--drafts expects a directory, not a file: {args.drafts}")
    settings = _settings()
    drafts = read_drafts(args.drafts)
    blocked_dir = args.drafts / "blocked"
    if not drafts:
        raise OutreachError(_no_drafts_message(args.drafts))
    if args.sender:
        sender = load_sender(args.sender)
    else:
        sender = SenderProfile(name="")
        print(
            "note: no --sender given; sender forbidden phrases are not checked at send time",
            file=sys.stderr,
        )
    template_count = sum(
        draft.hook is not None and draft.hook.provider == "template" for draft in drafts
    )
    if template_count:
        print(
            f"note: {template_count} draft(s) were written by the offline template provider "
            "(placeholder hooks); read them before sending.",
            file=sys.stderr,
        )
    journal = Journal(Path(settings.workdir) / ".vcoutreach" / "journal.jsonl")
    transport = _DeferredGmailTransport(settings) if args.execute else None
    try:
        report = plan_sends(
            drafts, settings, sender, journal,
            live=args.live, execute=args.execute, confirm_live=args.confirm_live,
            limit=args.limit, transport=transport,
        )
    except SendGateError:
        raise
    except OutreachError as exc:
        if args.execute:
            raise ProviderError(str(exc)) from exc
        raise
    print("ACTION\tCONTACT\tMODE\tTO\tSUBJECT / REASON")
    action = "sent" if args.execute else "plan"
    for item in report.sent if args.execute else report.planned:
        print("\t".join(_one_line(value) for value in (
            action, item.draft.contact_id, item.mode, item.to, item.subject,
        )))
    for item in report.skipped:
        print(
            f"skipped {_one_line(item.draft.contact_id)}: {_one_line(item.reason)} "
            f"({_one_line(item.detail)})"
        )
    if blocked_dir.is_dir():
        count = sum(path.is_file() for path in blocked_dir.iterdir())
        if count:
            print(f"blocked drafts not considered: {count} (see {blocked_dir})")
    print(f"planned={len(report.planned)} sent={len(report.sent)} skipped={len(report.skipped)}")
    return 0


def _auth_gmail(args: argparse.Namespace) -> int:
    from vcoutreach.send import authorize

    settings = _settings()
    _gmail_dependencies()
    authorize(settings)
    print("Gmail authorization complete.")
    return 0


def _auth_gmail_web(args: argparse.Namespace) -> int:
    from vcoutreach.web.config import configure_workspace
    from vcoutreach.web.gmail.api import GmailAuthNotReady, run_oauth_flow

    try:
        workspace = configure_workspace(args.workspace)
        workspace.initialize()
        _gmail_dependencies()
        run_oauth_flow()
    except (GmailAuthNotReady, ProviderError) as exc:
        print(f"vcoutreach: {_one_line(exc)}", file=sys.stderr)
        return 3
    except Exception:
        print("vcoutreach: Gmail web authorization failed. Check data/.gmail_credentials.json in the workspace and retry `vcoutreach auth gmail-web --workspace DIR`.", file=sys.stderr)
        return 3
    print("Gmail web authorization complete. Token saved at data/.gmail_token.json (mode 0600).")
    return 0


def _ui(args: argparse.Namespace) -> int:
    from vcoutreach.web import serve

    try:
        return serve(args.workspace, port=args.port, no_browser=args.no_browser)
    except KeyboardInterrupt:
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vcoutreach", description="Draft grounded outreach emails.")
    parser.add_argument("--version", action="version", version=f"vcoutreach {__version__}",
                        help="Show the package version and exit.")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Copy sample inputs without overwriting files.")
    init.add_argument("--dir", type=Path, default=Path("."), help="Destination directory (default: .).")
    init.set_defaults(handler=_init)

    draft = commands.add_parser("draft", help="Generate drafts from contacts and a sender profile.")
    draft.add_argument("--contacts", type=Path, required=True, help="Contacts CSV file.")
    draft.add_argument("--sender", type=Path, required=True, help="Sender profile JSON file.")
    draft.add_argument("--provider", default="auto", metavar="NAME",
                       help="Provider: auto, template, publik, openai or anthropic (default: auto).")
    draft.add_argument("--out", type=Path, default=Path("drafts"),
                       help="Output directory for Markdown drafts and index.jsonl (default: drafts).")
    draft.add_argument("--strict", action="store_true", help="Exit 2 if any draft is blocked.")
    draft.add_argument("--offline", action="store_true", help="Force the template provider with no network.")
    draft.set_defaults(handler=_draft)

    lint = commands.add_parser("lint", help="Re-lint top-level Markdown drafts in a directory.")
    lint.add_argument("--drafts", type=Path, required=True, help="Directory containing Markdown drafts.")
    lint.add_argument("--sender", type=Path, help="Optional sender JSON for forbidden phrase checks.")
    lint.set_defaults(handler=_lint)

    providers = commands.add_parser("providers", help="List providers and configuration status.")
    providers.set_defaults(handler=_providers)

    ui = commands.add_parser("ui", help="Open the local browser workspace, with draft review and guarded sending.")
    ui.add_argument("--workspace", type=Path, default=Path("."),
                    help="Workspace directory (default: current directory).")
    ui.add_argument("--port", type=int, metavar="N",
                    help="Use exactly this port (default: 8765, then the next 20 if busy).")
    ui.add_argument("--no-browser", action="store_true", help="Print the link without opening a browser.")
    ui.set_defaults(handler=_ui)

    send = commands.add_parser("send", help="Plan sends, or execute explicitly through Gmail.")
    send.add_argument("--drafts", type=Path, required=True, help="Directory of eligible top-level drafts.")
    send.add_argument("--sender", type=Path, help="Optional sender JSON for forbidden phrase checks.")
    send.add_argument("--execute", action="store_true", help="Send through Gmail instead of printing a plan.")
    send.add_argument("--live", action="store_true", help="Request live sending; also requires SEND_MODE=live.")
    send.add_argument("--confirm-live", default="", metavar="VALUE",
                      help="Use CONFIRM_LIVE_SEND to confirm live execution.")
    send.add_argument("--limit", type=_nonnegative, metavar="N",
                      help="Maximum number of planned sends after skips.")
    send.set_defaults(handler=_send)

    auth = commands.add_parser("auth", help="Authorize an optional mail transport.")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    gmail = auth_commands.add_parser("gmail", help="Run the Gmail send-only OAuth flow.")
    gmail.set_defaults(handler=_auth_gmail)
    gmail_web = auth_commands.add_parser("gmail-web", help="Authorize the browser app with Gmail read, reply and send access.")
    gmail_web.add_argument("--workspace", type=Path, default=Path(os.environ.get("OUTREACH_HOME") or "."),
                           help="Workspace containing data/.gmail_credentials.json (default: OUTREACH_HOME or current directory).")
    gmail_web.set_defaults(handler=_auth_gmail_web)
    return parser


def _error(exc: Exception) -> None:
    lines = str(exc).splitlines() or [type(exc).__name__]
    print(f"vcoutreach: {_one_line(lines[0]) or type(exc).__name__}", file=sys.stderr)
    for line in lines[1:]:
        print(_one_line(line), file=sys.stderr)
    if os.environ.get("VCOUTREACH_DEBUG") == "1":
        traceback.print_exc()


def main(argv=None) -> int:
    """Run the CLI and return its process exit code."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        return args.handler(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    except ProviderError as exc:
        _error(exc)
        return 3
    except (OutreachError, OSError, ValueError) as exc:
        _error(exc)
        return 2
    except Exception as exc:
        # Unexpected failures stay concise unless debug was explicitly enabled.
        _error(exc)
        return 3
