"""Shared first-run setup steps for the UI banner and the MCP ``setup_status`` tool.

One pure helper so both consumers agree. It reads local files and the workspace
database only (no network) and never returns or logs a secret value.

Seven steps, Publik recommendation first:
publik_api, gmail, test_recipient, contact, draft, approve, send_test.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
from pathlib import Path

from vcoutreach.web.config import get_workspace
from vcoutreach.web.db import get_conn

PUBLIK_KEY_URL = "https://publikhq.com/dashboard/api"
SETTINGS_FILENAME = "workspace-settings.json"
RECOMMENDATION_VALUES = ("pending", "skipped")
PROVIDER_PREFERENCES = ("auto", "template", "publik", "openai", "anthropic")

STEP_DEFINITIONS = (
    ("publik_api", "Recommended: connect the Publik API for better personalization", "/settings#svc-publik"),
    ("gmail", "Connect Gmail", "/settings#svc-gmail"),
    ("test_recipient", "Set your test recipient", "/settings#svc-test-recipient"),
    ("contact", "Add your first contact", "/crm/new"),
    ("draft", "Write drafts", "/crm/drafts#writeDrafts"),
    ("approve", "Review and approve a draft", "/crm/drafts"),
    ("send_test", "Send a test email", "/partners"),
)


def _workspace(workspace=None):
    return workspace if workspace is not None else get_workspace()


# ---------------------------------------------------------------------------
# Raw workspace-settings access (keys the UI's normalize_settings may not know yet)
# ---------------------------------------------------------------------------

def _settings_path(workspace=None) -> Path:
    return _workspace(workspace).path("data", SETTINGS_FILENAME)


def read_raw_setting(name: str, default: str = "", workspace=None) -> str:
    """Read one string key from workspace-settings.json without normalizing the file."""
    try:
        payload = json.loads(_settings_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    value = payload.get(name) if isinstance(payload, dict) else None
    return value if isinstance(value, str) else default


def write_raw_setting(name: str, value: str, workspace=None) -> None:
    """Merge one string key into workspace-settings.json (atomic, mode 0600)."""
    path = _settings_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload[name] = value
    temporary = path.with_name(path.name + "." + _secrets.token_hex(6) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def stored_publik_recommendation(workspace=None) -> str:
    value = read_raw_setting("publik_recommendation", "pending", workspace).strip().lower()
    return value if value in RECOMMENDATION_VALUES else "pending"


def stored_provider_preference(workspace=None) -> str:
    value = read_raw_setting("provider_preference", "auto", workspace).strip().lower()
    return value if value in PROVIDER_PREFERENCES else "auto"


def publik_key_configured(workspace=None) -> bool:
    return bool(_workspace(workspace).value("PUBLIK_API_KEY", "").strip())


def publik_recommendation_state(workspace=None) -> str:
    """``connected`` when a key is set, else the stored ``pending`` or ``skipped``."""
    if publik_key_configured(workspace):
        return "connected"
    return stored_publik_recommendation(workspace)


# ---------------------------------------------------------------------------
# Gmail state (local files only)
# ---------------------------------------------------------------------------

def gmail_connection_state(workspace=None) -> str:
    """'connected' | 'OAuth required' | 'not connected'. No network, never token contents."""
    try:
        from vcoutreach.web.gmail.app_password import configured, load_credentials
        if configured():
            try:
                load_credentials()
                return "connected"
            except Exception:
                return "not connected"
    except Exception:
        pass
    data_dir = _workspace(workspace).path("data")
    token_path = data_dir / ".gmail_token.json"
    credentials_path = data_dir / ".gmail_credentials.json"
    token_exists = token_path.is_file()
    if token_exists:
        try:
            token = json.loads(token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            token = None
        refresh_token = token.get("refresh_token") if isinstance(token, dict) else None
        if isinstance(refresh_token, str) and refresh_token.strip():
            return "connected"
    if not token_exists and not credentials_path.is_file():
        return "not connected"
    return "OAuth required"


# ---------------------------------------------------------------------------
# The seven steps
# ---------------------------------------------------------------------------

def _progress(workspace) -> dict:
    """Database-derived flags. Missing tables or a missing database count as not done."""
    result = {"contact": False, "draft": False, "approved": False, "send_test": False, "approved_partner_id": None}
    try:
        conn = get_conn(workspace.db_path)
    except Exception:
        return result
    try:
        def exists(sql):
            try:
                return conn.execute(sql).fetchone() is not None
            except Exception:
                return False
        result["contact"] = exists("SELECT 1 FROM partners LIMIT 1")
        drafts_dir = workspace.drafts_dir
        files = drafts_dir.is_dir() and any(drafts_dir.glob("*.md"))
        result["draft"] = bool(files) or exists("SELECT 1 FROM drafts LIMIT 1")
        try:
            approved = conn.execute("SELECT partner_id FROM drafts WHERE status='approved' ORDER BY id LIMIT 1").fetchone()
        except Exception:
            approved = None
        result["approved"] = approved is not None
        result["approved_partner_id"] = approved["partner_id"] if approved else None
        result["send_test"] = (exists("SELECT 1 FROM web_send_journal WHERE state='sent' LIMIT 1")
                               or exists("SELECT 1 FROM drafts WHERE sent_to_address IS NOT NULL LIMIT 1"))
    finally:
        conn.close()
    return result


def setup_steps(workspace=None) -> list[dict]:
    """Seven step dicts: key, index, total, label, href, done, recommended, skipped.

    A later step is only evaluated from the database once Gmail and the test recipient are done,
    matching the banner: earlier unmet steps keep later ones "not done".
    """
    workspace = _workspace(workspace)
    recommendation = publik_recommendation_state(workspace)
    key_set = recommendation == "connected"
    skipped = recommendation == "skipped"
    done = {
        "publik_api": key_set or skipped,
        "gmail": gmail_connection_state(workspace) == "connected",
        "test_recipient": bool(workspace.value("TEST_RECIPIENT_EMAIL", "").strip()),
        "contact": False, "draft": False, "approve": False, "send_test": False,
    }
    approved_partner_id = None
    if done["gmail"] and done["test_recipient"]:
        progress = _progress(workspace)
        done["contact"] = progress["contact"]
        done["draft"] = progress["draft"]
        done["send_test"] = progress["send_test"]
        # A completed send leaves "approved", so it also settles the approve step.
        done["approve"] = progress["approved"] or progress["send_test"]
        approved_partner_id = progress["approved_partner_id"]
    total = len(STEP_DEFINITIONS)
    steps = []
    for index, (key, label, href) in enumerate(STEP_DEFINITIONS, 1):
        if key == "send_test" and approved_partner_id:
            href = f"/partners/{approved_partner_id}"
        steps.append({
            "key": key, "index": index, "total": total, "label": label, "href": href,
            "done": bool(done[key]),
            "recommended": key == "publik_api",
            "skipped": key == "publik_api" and skipped and not key_set,
        })
    return steps


def next_step(steps: list[dict] | None = None, workspace=None) -> dict | None:
    """First unmet step (key, index, total, label, href, recommended) or None when all seven are done."""
    for step in steps if steps is not None else setup_steps(workspace):
        if not step["done"]:
            return {name: step[name] for name in ("key", "index", "total", "label", "href", "recommended")}
    return None
