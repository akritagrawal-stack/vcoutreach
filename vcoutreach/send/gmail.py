"""Gmail transport using installed-app OAuth, with lazy imports.

Safety rules for this module:

* The only OAuth scope ever requested or accepted is gmail.send. No code path reads mail.
* The token file lives at settings.gmail_token_path and is written with mode 0600.
* Tokens are never logged, printed or placed in an exception message.
* The Google libraries are optional and imported only inside functions.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vcoutreach.config import Settings
from vcoutreach.send.gate import SendReceipt
from vcoutreach.types import OutreachError

__all__ = ["GMAIL_SEND_SCOPE", "GmailTransport", "authorize"]

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_INSTALL_HINT = "pip install -r requirements-gmail.txt"


def _require_google() -> SimpleNamespace:
    """Import the optional Google libraries, or raise OutreachError with the install hint."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise OutreachError(
            "Gmail sending needs optional Google libraries that are not installed. "
            f"Run: {_INSTALL_HINT}"
        ) from exc
    return SimpleNamespace(
        Request=Request,
        Credentials=Credentials,
        InstalledAppFlow=InstalledAppFlow,
        build=build,
    )


def _token_path(settings: Settings) -> Path:
    return Path(settings.gmail_token_path).expanduser()


def _write_token(path: Path, text: str) -> None:
    """Atomically write the token file, readable by the owner only (mode 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".token-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _check_token_scopes(path: Path) -> None:
    """Refuse a token file that was granted anything beyond gmail.send."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OutreachError(
            f"The Gmail token file at {path} is unreadable. Run `vcoutreach auth gmail` again."
        ) from None
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if scopes is None:
        return
    if isinstance(scopes, str):
        scopes = scopes.split()
    if set(scopes) != {GMAIL_SEND_SCOPE}:
        raise OutreachError(
            "The Gmail token file has scopes other than gmail.send. "
            "Delete it and run `vcoutreach auth gmail` again."
        )


def _describe_send_error(exc: Exception) -> str:
    """A short, secret free description of a failed Gmail API call."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = getattr(exc, "reason", "")
    reason = reason[:200] if isinstance(reason, str) else ""
    detail = f": {reason}" if reason else ""
    if isinstance(status, int) and 400 <= status < 500:
        return f"Gmail rejected the message (HTTP {status}){detail}. Nothing was sent."
    where = f" (HTTP {status})" if isinstance(status, int) else ""
    return (
        f"Gmail send failed{where}{detail}. The message may or may not have been delivered: "
        "check the Gmail Sent folder before running again."
    )


class GmailTransport:
    """MailTransport that sends through the Gmail API with the gmail.send scope only."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service: Any = None

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        google = _require_google()
        path = _token_path(self._settings)
        if not path.is_file():
            raise OutreachError(f"No Gmail token found at {path}. Run `vcoutreach auth gmail` first.")
        _check_token_scopes(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        credentials = google.Credentials.from_authorized_user_file(str(path), [GMAIL_SEND_SCOPE])
        if not credentials.valid:
            if not getattr(credentials, "refresh_token", None):
                raise OutreachError("The Gmail token cannot be refreshed. Run `vcoutreach auth gmail` again.")
            try:
                credentials.refresh(google.Request())
            except Exception:
                raise OutreachError(
                    "Refreshing the Gmail token failed. Run `vcoutreach auth gmail` again."
                ) from None
            _write_token(path, credentials.to_json())
        self._service = google.build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return self._service

    def send(self, message: EmailMessage) -> SendReceipt:
        service = self._get_service()
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            response = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except OutreachError:
            raise
        except Exception as exc:
            raise OutreachError(_describe_send_error(exc)) from exc
        if not isinstance(response, dict):
            response = {}
        return SendReceipt(
            message_id=str(response.get("id", "")),
            thread_id=str(response.get("threadId", "")),
        )


def authorize(settings: Settings) -> None:
    """Run the installed-app OAuth flow (gmail.send only) and store the token with mode 0600."""
    google = _require_google()
    secrets = Path(settings.gmail_client_secrets_path).expanduser()
    if not secrets.is_file():
        raise OutreachError(
            f"Gmail OAuth client file not found at {secrets}. In Google Cloud Console enable the "
            "Gmail API, create an OAuth client of type Desktop app, and save its JSON there."
        )
    flow = google.InstalledAppFlow.from_client_secrets_file(str(secrets), [GMAIL_SEND_SCOPE])
    credentials = flow.run_local_server(port=0, prompt="consent")
    _write_token(_token_path(settings), credentials.to_json())
