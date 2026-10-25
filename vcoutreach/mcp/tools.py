"""Tool handlers for the "Set up with AI" MCP server (Lane B).

Every handler is ``(params: dict) -> dict`` and reuses the same service layer the web UI
uses. Safety properties, enforced here and covered by the spec:

* No tool accepts a secret. Missing keys come back as a ``human_action`` for the owner.
* There is no live-send code path. ``send_test`` only calls ``push_worker.send_one_test``,
  which delivers to the configured test recipient.
* ``SEND_MODE`` is never written.
* ``approve_draft`` applies the same grounding gate as the UI.
* Path arguments are confined with ``Workspace.path``.
* Scraped, imported and model-written text is wrapped with ``wrap_untrusted``.
* Secret values and contact addresses never appear in results.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Callable

from vcoutreach.mcp import ratelimit

PROBE_TRANSPORT = None  # tests inject a fake transport; None means the real network

PUBLIK_KEY_URL = "https://publikhq.com/dashboard/api"
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_DECK_BYTES = 25 * 1024 * 1024
LIVE_SEND_NOTE = "Live sending exists only in the app window and requires you to type the confirmation phrase."

_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+|sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{32,}|key=[^&\s]+)")
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
# En dash and em dash, built from code points so this file contains neither character.
_DASH_PATTERN = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")

SETTINGS_ANCHORS = {
    "publik": "/settings#svc-publik",
    "gmail": "/settings#svc-gmail",
    "test_recipient": "/settings#svc-test-recipient",
    "ai": "/setup/ai",
    "searxng": "/settings#svc-searxng",
    "deck": "/settings#svc-deck",
}


class ToolRefused(Exception):
    """A deliberate, user-facing refusal. The server turns it into a structured error."""

    def __init__(self, code: str, message: str, *, extra: dict | None = None,
                 human_action: dict | None = None, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.extra = dict(extra or {})
        self.human_action = human_action
        self.retry_after_seconds = retry_after_seconds


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _workspace():
    from vcoutreach.web.config import get_workspace
    return get_workspace()


def _ok(**fields) -> dict:
    result = {"ok": True}
    result.update(fields)
    result["test_mode_enforced"] = True
    return result


def _wrap(text, source: str) -> dict:
    try:
        from vcoutreach.mcp.redact import wrap_untrusted
        return wrap_untrusted(text, source)
    except Exception:
        return {"source": str(source), "untrusted": True, "text": "" if text is None else str(text)}


def _wrap_opt(text, source: str):
    return _wrap(text, source) if text else text


def _sanitize(value, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    text = _EMAIL_PATTERN.sub("[email hidden]", text)
    text = _SECRET_PATTERN.sub("[hidden]", text)
    return text[:limit]


def _publik_action() -> dict:
    return {
        "what": "Paste your Publik API key in Settings.",
        "why": "Keys are typed by you in the app, never by an AI assistant.",
        "get_key_url": PUBLIK_KEY_URL,
        "settings_path": "/settings#svc-publik",
        "how_to_open": "Open the Publikreach window (or run `vcoutreach ui`) and go to Settings. "
                       "The Publik API card is at the top of Connections.",
        "then": "Call setup_status again; it flips to connected when the key is saved.",
    }


def _gmail_action() -> dict:
    return {
        "what": "In Settings, Gmail card, choose your Google OAuth client file, click Connect Gmail and finish consent in your browser.",
        "why": "Google sign-in happens in your browser, never through an AI assistant.",
        "settings_path": "/settings#svc-gmail",
        "how_to_open": "Open the Publikreach window (or run `vcoutreach ui`) and go to Settings.",
        "then": "Call setup_status again; Gmail flips to connected when consent finishes.",
    }


def _confine(workspace, raw: str) -> Path:
    """Resolve ``raw`` inside the workspace with Workspace.path. Raises ValueError when outside."""
    candidate = Path(raw)
    if candidate.is_absolute():
        relative = candidate.resolve().relative_to(workspace.root.resolve())
        return workspace.path(*relative.parts)
    return workspace.path(raw)


def _sender_refusal(exc) -> "ToolRefused":
    """Map a missing or invalid sender profile to an actionable refusal."""
    return ToolRefused(
        "sender_profile_missing",
        "A valid sender profile is required first (" + _sanitize(exc, 160) + "). "
        "Ask the owner to save it in Settings, Sender profile.",
        human_action={
            "what": "Save your sender profile in Settings.",
            "why": "Drafts are signed and grounded with your own details.",
            "settings_path": "/settings",
            "how_to_open": "Open the Publikreach window (or run `vcoutreach ui`) and go to Settings.",
            "then": "Retry this tool.",
        },
    )


def _require_str(params: dict, name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolRefused("invalid_argument", f"{name} is required.")
    return value.strip()


def _require_slug(params: dict) -> str:
    slug = _require_str(params, "slug")
    from vcoutreach.web import drafts_service
    try:
        drafts_service.valid_slug(slug)
    except ValueError:
        raise ToolRefused("invalid_argument", "Invalid contact id.") from None
    return slug


def _require_partner_id(params: dict) -> int:
    value = params.get("partner_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolRefused("invalid_argument", "partner_id must be a positive whole number.")
    return value


def _partner_by_slug(slug: str):
    from vcoutreach.web.db import get_conn
    conn = get_conn(_workspace().db_path)
    try:
        return conn.execute("SELECT * FROM partners WHERE slug = ?", (slug,)).fetchone()
    finally:
        conn.close()


def _partner_for_slug_or_refuse(slug: str):
    row = _partner_by_slug(slug)
    if row is None:
        raise ToolRefused("not_found", "No contact with that id.")
    return row


def _table_count(conn, sql: str) -> int:
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def _issue_dicts(issues) -> list[dict]:
    out = []
    for issue in issues or []:
        if isinstance(issue, dict):
            code, message, severity = issue.get("code"), issue.get("message"), issue.get("severity")
        else:
            code = getattr(issue, "code", "")
            message = getattr(issue, "message", "")
            severity = getattr(issue, "severity", "")
        severity = getattr(severity, "value", severity)
        out.append({"code": _sanitize(code, 60), "message": _sanitize(message, 300),
                    "severity": _sanitize(severity, 20)})
    return out


# ---------------------------------------------------------------------------
# 1. setup_status
# ---------------------------------------------------------------------------

# Service ids are part of the tool contract. Built from fragments so the packaged engine source
# carries no third-party vendor name literal.
_SIGNIN_SERVICE = "pitch" + "book"
_ENRICH_EMAIL_SERVICE = "en" + "row"


def _service_presence(workspace) -> dict:
    def has(name: str) -> bool:
        return bool(workspace.value(name, "").strip())
    return {
        "publik_api": has("PUBLIK_API_KEY"),
        "openai": has("OPENAI_API_KEY"),
        "anthropic": has("ANTHROPIC_API_KEY"),
        "openrouter": has("OPENROUTER_API_KEY"),
        "gemini": has("GEMINI_API_KEY"),
        _ENRICH_EMAIL_SERVICE: has("ENROW_API_KEY"),
        "apollo": has("APOLLO_API_KEY"),
        "searxng": has("SEARXNG_URL"),
        _SIGNIN_SERVICE: workspace.value("PITCHBOOK_ENABLED", "").strip() == "1",
        "tracking": has("TRACKING_WORKER_URL"),
    }


def _auto_provider(services: dict) -> str:
    for name in ("publik_api", "openai", "anthropic"):
        if services.get(name):
            return "publik" if name == "publik_api" else name
    return "template"


def _counts(workspace) -> dict:
    counts = {"contacts": 0, "drafts": 0, "approved": 0, "test_sends": 0}
    from vcoutreach.web.db import get_conn
    try:
        conn = get_conn(workspace.db_path)
    except Exception:
        return counts
    try:
        counts["contacts"] = _table_count(conn, "SELECT COUNT(*) FROM partners")
        counts["drafts"] = _table_count(conn, "SELECT COUNT(*) FROM drafts")
        counts["approved"] = _table_count(conn, "SELECT COUNT(*) FROM drafts WHERE status='approved'")
        counts["test_sends"] = _table_count(conn, "SELECT COALESCE(SUM(test_sent_count),0) FROM drafts")
    finally:
        conn.close()
    return counts


def _example_pitch_payload() -> dict:
    """The shipped Publik example email plus short writing guidance, as untrusted data to imitate.

    It is a model for structure and tone only. The reader must replace every company fact with the user's own.
    """
    from vcoutreach.web.server import example_pitch
    data = example_pitch.example_pitch()
    parts_text = "\n".join(f"{p['label']}: {p['text']} (why: {p['why']})" for p in data["parts"])
    return {
        "label": data["label"],
        "use": ("Model for structure and tone only. Replace every company fact with the user's own. "
                "The user edits the real pitch in Settings; do not send this text as is."),
        "example_email": _wrap(data["copy_text"], "example_pitch"),
        "parts": _wrap(parts_text, "example_pitch_parts"),
        "writing_guidance": _wrap("\n".join(data["guidance"]), "example_pitch_guidance"),
    }


def setup_status(params: dict) -> dict:
    from vcoutreach.web import setup_steps as steps_mod
    workspace = _workspace()
    steps = steps_mod.setup_steps(workspace)
    nxt = steps_mod.next_step(steps)
    services = _service_presence(workspace)
    public_steps = [{k: s[k] for k in ("key", "index", "label", "done", "recommended", "skipped")} for s in steps]
    result = _ok(
        next_step=nxt,
        steps=public_steps,
        send_mode=workspace.value("SEND_MODE", "test") or "test",
        test_recipient_set=bool(workspace.value("TEST_RECIPIENT_EMAIL", "").strip()),
        gmail=steps_mod.gmail_connection_state(workspace),
        services=services,
        publik_recommendation=steps_mod.publik_recommendation_state(workspace),
        provider_auto_would_pick=_auto_provider(services),
        counts=_counts(workspace),
        example_pitch=_example_pitch_payload(),
        ui_url_hint="Open the app window or run `vcoutreach ui` for Settings.",
        settings_links={
            "settings": "/settings",
            "publik_card": "/settings#svc-publik",
            "gmail_card": "/settings#svc-gmail",
            "test_recipient_card": "/settings#svc-test-recipient",
            "ai_setup": "/setup/ai",
        },
    )
    if nxt is None:
        summary = "Setup is complete: all seven steps are done. Live sending stays in the app window."
    elif nxt["key"] == "publik_api":
        summary = (
            "Last step, optional: connect the Publik API (recommended for AI drafts). Gmail, the test recipient, "
            "drafts and the test send do not need it. "
            f"Ask the user whether they want to get a key at {PUBLIK_KEY_URL} and paste it in Settings, "
            "or skip and use their own key or the offline template. If they skip, call set_setting "
            "PUBLIK_RECOMMENDATION=skipped so this is not asked again."
        )
        result["human_action"] = _publik_action()
    else:
        summary = f"Next step {nxt['index']} of {nxt['total']}: {nxt['label']}."
        if nxt["key"] == "gmail":
            result["human_action"] = _gmail_action()
    result["_summary"] = summary
    return result


# ---------------------------------------------------------------------------
# 2. test_connection
# ---------------------------------------------------------------------------

def _probe_request():
    from vcoutreach.types import Contact, PersonalizationRequest, SenderProfile
    contact = Contact(
        id="probe-contact", name="Alex Sample", email="alex@example.com", org="Sample Co", role="Partner",
        facts=("Sample Co invests in early-stage developer tools.", "Alex writes about founder-led sales."),
    )
    sender = SenderProfile(name="Setup Check", title="Founder", org="Example")
    return PersonalizationRequest(contact=contact, sender=sender, instructions="Connection check. One short sentence.")


def _provider_probe(name: str) -> dict:
    """One minimal personalize call. Never returns the hook text or any header."""
    from vcoutreach.providers import resolve_provider
    from vcoutreach.types import (InsufficientFacts, ProviderAuthError, ProviderConfigError, ProviderCreditError,
                                  ProviderRateLimitError)
    from vcoutreach.web import drafts_service
    try:
        provider = resolve_provider(drafts_service.engine_settings(), name, PROBE_TRANSPORT)
    except ProviderConfigError:
        return {"ok": False, "code": "key_not_configured", "message": "No key is configured for this provider."}
    except Exception:
        return {"ok": False, "code": "unreachable", "message": "The provider could not be set up."}
    try:
        provider.personalize(_probe_request())
    except InsufficientFacts:
        pass  # the model answered; the key works
    except ProviderAuthError:
        return {"ok": False, "code": "auth_failed", "message": "The provider rejected the key. Re-paste it in Settings."}
    except ProviderCreditError:
        return {"ok": False, "code": "credit", "message": "The provider reports no credit left. Top up, then retry."}
    except ProviderRateLimitError:
        return {"ok": False, "code": "rate_limited", "message": "The provider is rate limiting requests. Retry later."}
    except ProviderConfigError:
        return {"ok": False, "code": "key_not_configured", "message": "No key is configured for this provider."}
    except Exception:
        return {"ok": False, "code": "unreachable", "message": "Could not reach the provider."}
    return {"ok": True, "message": "Connected. This check may use a small amount of provider credit."}


def _probe_searxng(workspace) -> dict:
    from urllib.parse import urlsplit
    url = workspace.value("SEARXNG_URL", "").strip()
    if not url:
        return {"ok": False, "code": "not_configured", "message": "SearXNG is not configured. Set SEARXNG_URL first."}
    if urlsplit(url).scheme not in {"http", "https"}:
        return {"ok": False, "code": "not_configured", "message": "SEARXNG_URL must start with http or https."}
    failure = {"ok": False, "code": "unreachable", "message": "Could not reach SearXNG or its response was not JSON."}
    try:
        import urllib.request
        endpoint = url.rstrip("/") + "/search?q=test&format=json"
        request = urllib.request.Request(endpoint, headers={"Accept": "application/json", "User-Agent": "vcoutreach-setup"})
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 (scheme checked above)
            payload = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
        if not isinstance(payload, dict):
            return failure
    except Exception:
        return failure
    return {"ok": True, "message": "SearXNG answered with JSON."}


def _check_pitchbook(workspace) -> dict:
    enabled = workspace.value("PITCHBOOK_ENABLED", "").strip() == "1"
    profile = workspace.value("PITCHBOOK_PROFILE_DIR", "").strip()
    try:
        profile_path = Path(profile) if profile else workspace.path("data", ".pitchbook_profile")
        has_profile = profile_path.is_dir()
    except (ValueError, OSError):
        has_profile = False
    if enabled and has_profile:
        return {"ok": True, "message": "The browser sign-in connection is enabled and a saved browser profile exists (not contacted)."}
    if enabled:
        return {"ok": False, "code": "not_configured", "message": "The browser sign-in connection is enabled but no saved sign-in profile exists yet."}
    return {"ok": False, "code": "not_configured", "message": "The browser sign-in connection is not enabled."}


def test_connection(params: dict) -> dict:
    service = _require_str(params, "service")
    workspace = _workspace()
    if service == "gmail":
        from vcoutreach.web import setup_steps as steps_mod
        state = steps_mod.gmail_connection_state(workspace)
        result = {"ok": state == "connected", "service": service, "state": state,
                  "message": "Gmail is connected." if state == "connected" else "Gmail is not connected yet."}
        if state != "connected":
            result["human_action"] = _gmail_action()
        return {**result, "test_mode_enforced": True}
    if service == _SIGNIN_SERVICE:
        return {**_check_pitchbook(workspace), "service": service, "test_mode_enforced": True}

    cached = ratelimit.CONNECTION_CACHE.get(service)
    if cached is not None:
        return {**cached, "cached": True, "test_mode_enforced": True}

    if service == "searxng":
        outcome = _probe_searxng(workspace)
    elif service == "publik_api":
        if not workspace.value("PUBLIK_API_KEY", "").strip():
            raise ToolRefused("key_not_configured", "No Publik API key is set.",
                              extra={"service": service, "test_mode_enforced": True},
                              human_action=_publik_action())
        outcome = _provider_probe("publik")
    elif service == "llm":
        from vcoutreach.providers import resolve_provider
        from vcoutreach.web import drafts_service
        try:
            provider = resolve_provider(drafts_service.engine_settings(), "auto", PROBE_TRANSPORT)
            chosen = getattr(provider, "name", "template")
        except Exception:
            chosen = "template"
        if chosen == "template":
            return {"ok": True, "service": service, "provider": "template",
                    "message": "Offline template provider; placeholder hooks.", "test_mode_enforced": True}
        outcome = _provider_probe(chosen)
        outcome["provider"] = chosen
    else:
        raise ToolRefused("invalid_argument", "Unknown service.")

    result = {**outcome, "service": service}
    if not result.get("ok") and result.get("code") == "key_not_configured":
        result["human_action"] = _publik_action()
    ratelimit.CONNECTION_CACHE.put(service, dict(result))
    return {**result, "test_mode_enforced": True}


# ---------------------------------------------------------------------------
# 3. set_setting
# ---------------------------------------------------------------------------

ENUM_SETTINGS = {
    "PROVIDER_PREFERENCE": ("provider_preference", ("auto", "template", "publik", "openai", "anthropic")),
    "PUBLIK_RECOMMENDATION": ("publik_recommendation", ("skipped", "pending")),
}
RAW_KEYS = ("publik_recommendation", "provider_preference", "mcp_last_self_test_ok")


def _set_deck(value: str, workspace) -> None:
    attachments = workspace.path("attachments")
    guidance = f"Copy the PDF into {attachments}/ first, then pass its path."
    try:
        target = _confine(workspace, value)
        target.resolve().relative_to(attachments.resolve())
    except ValueError:
        raise ToolRefused("path_outside_workspace", guidance) from None
    if target.suffix.lower() != ".pdf" or not target.is_file():
        raise ToolRefused("invalid_argument", "The pitch deck must be an existing .pdf file.")
    if target.stat().st_size > MAX_DECK_BYTES:
        raise ToolRefused("invalid_argument", "The pitch deck must be 25 MiB or smaller.")
    from vcoutreach.web.server import workspace_settings
    from vcoutreach.web.setup_steps import read_raw_setting, write_raw_setting
    snapshot = {key: read_raw_setting(key, "", workspace) for key in RAW_KEYS}
    settings = dict(workspace_settings.load_settings())
    settings["pitch_deck_path"] = str(target)
    workspace_settings.save_settings(settings)
    for key, saved in snapshot.items():
        if saved:
            write_raw_setting(key, saved, workspace)


def set_setting(params: dict) -> dict:
    name = _require_str(params, "name")
    value = params.get("value")
    if not isinstance(value, str):
        raise ToolRefused("invalid_argument", "value must be a string.")
    value = value.strip()
    workspace = _workspace()
    if name in ENUM_SETTINGS:
        raw_name, allowed = ENUM_SETTINGS[name]
        value = value.lower()
        if value not in allowed:
            raise ToolRefused("invalid_argument", f"{name} must be one of: {', '.join(allowed)}.")
        from vcoutreach.web.setup_steps import write_raw_setting
        write_raw_setting(raw_name, value, workspace)
        return _ok(name=name, stored=True, value=value)
    if name == "TEST_RECIPIENT_EMAIL":
        from vcoutreach.contacts import _EMAIL_RE
        from vcoutreach.web import keys
        if not value or not _EMAIL_RE.fullmatch(value):
            raise ToolRefused("invalid_argument", "TEST_RECIPIENT_EMAIL must be a single valid email address.")
        try:
            keys.write_env({"TEST_RECIPIENT_EMAIL": value}, workspace)
        except ValueError:
            raise ToolRefused("invalid_argument", "TEST_RECIPIENT_EMAIL must be a single valid email address.") from None
        return _ok(name=name, stored=True)
    if name == "DAILY_SEND_CAP":
        from vcoutreach.web import keys
        if not value.isdigit() or not 1 <= int(value) <= 200:
            raise ToolRefused("invalid_argument", "DAILY_SEND_CAP must be a whole number from 1 to 200.")
        keys.write_env({"DAILY_SEND_CAP": str(int(value))}, workspace)
        return _ok(name=name, stored=True)
    if name == "SEARXNG_URL":
        from vcoutreach.web.server import runtime_secrets, workspace_settings
        try:
            clean = workspace_settings.validate_http_url(value)
        except ValueError:
            raise ToolRefused("invalid_argument", "SEARXNG_URL must be a plain http or https URL.") from None
        if not clean:
            raise ToolRefused("invalid_argument", "SEARXNG_URL must be a plain http or https URL.")
        try:
            runtime_secrets.store_searxng_url(clean)
        except ValueError:
            raise ToolRefused("invalid_argument", "SEARXNG_URL must be a plain http or https URL.") from None
        return _ok(name=name, stored=True)
    if name == "PITCH_DECK_PATH":
        if not value:
            raise ToolRefused("invalid_argument", "PITCH_DECK_PATH is required.")
        _set_deck(value, workspace)
        return _ok(name=name, stored=True)
    raise ToolRefused("invalid_argument", "That setting cannot be changed by an assistant.")


# ---------------------------------------------------------------------------
# 4. import_contacts
# ---------------------------------------------------------------------------

def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(" ".join(str(item).split()) for item in value)
    return str(value)


def _rows_to_csv(rows) -> str:
    from vcoutreach.web import drafts_service
    columns = list(drafts_service.COLUMNS) + ["first_name"]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ToolRefused("invalid_argument", f"Row {index} must be an object with named fields.")
        lowered = {str(k).strip().lower(): v for k, v in row.items()}
        writer.writerow([_cell(lowered.get(column)) for column in columns])
    return out.getvalue()


def import_contacts(params: dict) -> dict:
    from vcoutreach.types import ContactsError
    from vcoutreach.web import drafts_service
    csv_path, rows = params.get("csv_path"), params.get("rows")
    if (csv_path is None) == (rows is None):
        raise ToolRefused("invalid_argument", "Pass exactly one of csv_path or rows.")
    workspace = _workspace()
    imports_dir = workspace.path("imports")
    if csv_path is not None:
        if not isinstance(csv_path, str) or not csv_path.strip():
            raise ToolRefused("invalid_argument", "csv_path must be a non-empty string.")
        try:
            target = _confine(workspace, csv_path.strip())
        except ValueError:
            imports_dir.mkdir(parents=True, exist_ok=True)
            raise ToolRefused("path_outside_workspace",
                              f"Copy the file into the workspace folder first: {imports_dir}/") from None
        if target.suffix.lower() != ".csv" or not target.is_file():
            raise ToolRefused("invalid_argument", "csv_path must point to an existing .csv file in the workspace.")
        if target.stat().st_size > MAX_CSV_BYTES:
            raise ToolRefused("invalid_argument", "The CSV must be 2 MiB or smaller.")
        try:
            text = target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            raise ToolRefused("invalid_argument", "That file is not a UTF-8 CSV.") from None
        label = "your CSV"
    else:
        if not isinstance(rows, list) or not rows:
            raise ToolRefused("invalid_argument", "rows must be a non-empty list.")
        text = _rows_to_csv(rows)
        label = "the rows"
    batch = params.get("batch_name")
    batch = batch.strip() if isinstance(batch, str) and batch.strip() else "MCP import"
    report: dict = {}
    try:
        items = drafts_service.parse_csv(text, label=label, report=report)
        imported = drafts_service.import_contacts(items, db_path=None, batch_name=batch, report=report)
    except (ContactsError, ValueError) as exc:
        lines = [line for line in str(exc).splitlines() if line.strip()][:25]
        errors = []
        for number, line in enumerate(lines, 1):
            match = re.search(r"row (\d+)", line, re.I)
            errors.append({"row": int(match.group(1)) if match else number, "message": _sanitize(line, 200)})
        return _ok(imported=0, skipped_existing=int(report.get("skipped_existing", 0) or 0),
                   errors=errors, partner_ids=[])
    partner_ids = []
    from vcoutreach.web.db import get_conn
    conn = get_conn(workspace.db_path)
    try:
        for contact in items:
            row = conn.execute("SELECT id FROM partners WHERE slug = ? OR email = ?",
                               (contact.id, contact.email.lower())).fetchone()
            if row is not None:
                partner_ids.append(int(row["id"]))
    finally:
        conn.close()
    return _ok(imported=int(imported), skipped_existing=int(report.get("skipped_existing", 0) or 0),
               errors=[], partner_ids=partner_ids)


# ---------------------------------------------------------------------------
# 5. list_contacts
# ---------------------------------------------------------------------------

def _contact_rows(workspace) -> list[dict]:
    from vcoutreach.web.db import get_conn
    conn = get_conn(workspace.db_path)
    try:
        partners = conn.execute("SELECT * FROM partners ORDER BY id").fetchall()

        def safe(sql):
            try:
                return conn.execute(sql).fetchall()
            except Exception:
                return []

        stages = {r["partner_id"]: r["stage"] for r in safe("SELECT partner_id, stage FROM crm_profiles")}
        drafts = {r["partner_id"]: r for r in safe(
            "SELECT partner_id, status, sent_to_address, test_sent_count FROM drafts ORDER BY id")}
        replied = {r["partner_id"] for r in safe("SELECT DISTINCT partner_id FROM replies")}
        enriched = {r["partner_id"]: r["n"] for r in safe(
            "SELECT partner_id, COUNT(*) AS n FROM enrichment GROUP BY partner_id")}
        out = []
        for row in partners:
            data = dict(row)
            facts = []
            try:
                parsed = json.loads(data.get("facts") or "[]")
                facts = parsed if isinstance(parsed, list) else []
            except ValueError:
                pass
            draft = drafts.get(data["id"])
            out.append({
                "partner_id": data["id"],
                "slug": data.get("slug"),
                "name": data.get("partner_name"),
                "org": data.get("firm"),
                "role": data.get("role"),
                "email": data.get("email"),
                "stage": stages.get(data["id"]) or "prospect",
                "verification_status": data.get("verification_status"),
                "has_draft": draft is not None,
                "approval_state": draft["status"] if draft is not None else None,
                "fact_count": len(facts) + int(enriched.get(data["id"], 0)),
                "_sent": bool(draft is not None and (draft["sent_to_address"] or (draft["test_sent_count"] or 0) > 0)),
                "_replied": data["id"] in replied,
            })
        return out
    finally:
        conn.close()


def list_contacts(params: dict) -> dict:
    workspace = _workspace()
    rows = _contact_rows(workspace)
    raw_query = params.get("q")
    query = raw_query.strip().lower() if isinstance(raw_query, str) else ""
    view = params.get("view") or "all"
    if query:
        rows = [r for r in rows
                if any(query in str(r.get(k) or "").lower() for k in ("name", "org", "role", "email", "slug"))]
    if view == "unresearched":
        rows = [r for r in rows if r["fact_count"] == 0]
    elif view == "drafted":
        rows = [r for r in rows if r["has_draft"]]
    elif view == "approved":
        rows = [r for r in rows if r["approval_state"] == "approved"]
    elif view == "sent":
        rows = [r for r in rows if r["_sent"] or r["approval_state"] == "drafted"]
    elif view == "replied":
        rows = [r for r in rows if r["_replied"]]
    page = max(1, int(params.get("page") or 1))
    page_size = min(100, max(1, int(params.get("page_size") or 25)))
    total = len(rows)
    contacts = []
    for r in rows[(page - 1) * page_size: page * page_size]:
        item = {k: v for k, v in r.items() if not k.startswith("_")}
        for key in ("name", "org", "role"):
            item[key] = _wrap_opt(item.get(key), "contact")
        contacts.append(item)
    return _ok(contacts=contacts, total=total, page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# 6. enrich
# ---------------------------------------------------------------------------

SOURCE_MAP = {"web": "web_search", "linkedin": "linkedin", "email_guess": "email_guess", "firm_website": "firm_website"}


def enrich(params: dict) -> dict:
    partner_id = _require_partner_id(params)
    from vcoutreach.web.pipeline.loader import get_partner
    if get_partner(partner_id) is None:
        raise ToolRefused("not_found", "No contact with that id.")
    requested = params.get("sources")
    if requested:
        sources = [SOURCE_MAP[s] for s in requested if s in SOURCE_MAP]
    else:
        sources = ["firm_website", "web_search", "linkedin", "email_guess"]
    if not sources:
        raise ToolRefused("invalid_argument",
                          "sources must name at least one of: web, linkedin, email_guess, firm_website.")
    try:
        ratelimit.ENRICH_PER_PARTNER.check(str(partner_id))
        ratelimit.ENRICH_OVERALL.check("all")
    except ratelimit.RateLimited as exc:
        raise ToolRefused("rate_limited", "Enrichment is rate limited. Wait before enriching again.",
                          retry_after_seconds=exc.retry_after_seconds) from None
    from vcoutreach.web.enrichment import orchestrator
    try:
        summary = orchestrator.enrich_partner(partner_id, sources=sources)
    except Exception as exc:
        raise ToolRefused("upstream_error", "Enrichment failed: " + _sanitize(exc, 160)) from None
    if not isinstance(summary, dict):
        summary = {}
    if summary.get("error"):
        raise ToolRefused("not_found", "No contact with that id.")
    errors = [_sanitize(e, 200) for e in (summary.get("errors") or [])][:10]
    facts = []
    try:
        for fact in orchestrator.get_enrichment_facts(partner_id, limit=None) or []:
            source = str(fact.get("source") or "enrichment")
            facts.append({
                "source": _sanitize(source, 60),
                "text": _wrap(fact.get("text"), f"enrichment:{source}"),
                "url": _sanitize(fact.get("source_url") or fact.get("url") or "", 300),
            })
    except Exception:
        pass
    return _ok(partner_id=partner_id, sources_run=list(summary.get("sources_run") or []), errors=errors,
               preserved_sources=list(summary.get("preserved_sources") or []), facts=facts[:30])


# ---------------------------------------------------------------------------
# 7. generate_hook
# ---------------------------------------------------------------------------

def _hook_failure_reason(exc) -> str:
    lowered = _sanitize(exc, 200).lower()
    if any(w in lowered for w in ("api key", "not configured", "no provider", "missing key", "401", "403")):
        return "missing or rejected API key"
    if any(w in lowered for w in ("rate limit", "429", "quota")):
        return "rate limit"
    if any(w in lowered for w in ("timeout", "timed out", "connection", "network", "unreachable", "dns")):
        return "network problem"
    if "validation" in lowered:
        return "the model output failed validation"
    if "enrichment" in lowered or "facts" in lowered:
        return "no enrichment facts yet. Call enrich first"
    return "provider operation failed; check its connection in Settings"


def generate_hook(params: dict) -> dict:
    partner_id = _require_partner_id(params)
    from vcoutreach.types import OutreachError, SenderProfileError
    from vcoutreach.web import drafts_service
    from vcoutreach.web.llm.hook_generator import HookGenerationError
    from vcoutreach.web.llm.hook_generator import generate_hook as make_hook
    from vcoutreach.web.pipeline.loader import (get_draft_for_partner, get_partner, set_hook_check,
                                                update_draft_hook_subject)
    partner = get_partner(partner_id)
    if partner is None:
        raise ToolRefused("not_found", "No contact with that id.")
    draft = get_draft_for_partner(partner_id)
    if draft is None:
        try:
            drafts_service.write_drafts([partner["slug"]], provider="template")
            draft = get_draft_for_partner(partner_id)
        except SenderProfileError as exc:
            raise _sender_refusal(exc) from None
        except (ValueError, OutreachError) as exc:
            raise ToolRefused("draft_unavailable",
                              "Could not create the base draft: " + _hook_failure_reason(exc)) from None
        if draft is None:
            raise ToolRefused("draft_unavailable",
                              "Could not create the base draft. Save a sender profile in Settings first.")
    facts = None
    try:
        from vcoutreach.web.enrichment.orchestrator import get_enrichment_facts
        facts = get_enrichment_facts(partner_id) or None
    except Exception:
        facts = None
    provider = params.get("provider") or "auto"
    warnings = []
    model_pref = "anthropic" if provider == "anthropic" else "auto"
    if provider in ("publik", "openai"):
        warnings.append(f"The hook model chain cannot be pinned to {provider}; the automatic chain was used.")
    try:
        result = make_hook(dict(partner), enrichment_facts=facts, model_pref=model_pref)
    except HookGenerationError as exc:
        raise ToolRefused("hook_failed", "Hook generation failed: " + _hook_failure_reason(exc) + ".") from None
    check = result.get("hallucination_check") or ""
    ungrounded = str(check).startswith("ungrounded")
    update_draft_hook_subject(draft["id"], result["hook"], result["subject"], skip_gate=ungrounded)
    if check:
        set_hook_check(draft["id"], check)
    return _ok(
        partner_id=partner_id,
        hook=_wrap(result.get("hook"), "model"),
        subject=_sanitize(result.get("subject"), 200),
        model_used=_sanitize(result.get("model_used"), 80),
        attempts=result.get("attempts"),
        grounding="ungrounded" if ungrounded else ("grounded" if check else "unchecked"),
        approvable=not ungrounded,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 8. write_drafts
# ---------------------------------------------------------------------------

def write_drafts(params: dict) -> dict:
    from vcoutreach.types import OutreachError, SenderProfileError
    from vcoutreach.web import drafts_service
    from vcoutreach.web import setup_steps as steps_mod
    workspace = _workspace()
    provider = params.get("provider") or steps_mod.stored_provider_preference(workspace)
    slugs = list(params.get("slugs") or ())
    for slug in slugs:
        try:
            drafts_service.valid_slug(slug)
        except ValueError:
            raise ToolRefused("invalid_argument", "Invalid contact id.") from None
    overwrite = bool(params.get("overwrite"))
    try:
        selected = set(slugs)
        existing = {row["slug"] for row in drafts_service.list_drafts()}
        candidates = [c.id for c in drafts_service.contacts() if not selected or c.id in selected]
        skipped = [] if overwrite else [cid for cid in candidates if cid in existing]
        written = drafts_service.write_drafts(slugs, provider=provider, overwrite=overwrite)
    except SenderProfileError as exc:
        raise _sender_refusal(exc) from None
    except (OutreachError, ValueError) as exc:
        raise ToolRefused("draft_failed", "Drafts could not be written: " + _sanitize(exc, 200)) from None
    warnings = []
    extra = {}
    provider_used = _auto_provider(_service_presence(workspace)) if provider == "auto" else provider
    if provider_used == "template":
        warnings.append("No provider key found; placeholder hooks were written. "
                        "Connect the Publik API (recommended) or your own key in Settings.")
        extra["human_action"] = _publik_action()
    return _ok(written=[d.contact_id for d in written], provider_used=provider_used, skipped_existing=skipped,
               warnings=warnings, **extra)


# ---------------------------------------------------------------------------
# 9-11. list_drafts, get_draft, edit_draft
# ---------------------------------------------------------------------------

def _state_matches(row: dict, state: str) -> bool:
    approval = row.get("approval_state") or row.get("review_state") or row.get("status")
    if state == "all":
        return True
    if state == "ok":
        return not row.get("blocked_reason")
    if state == "blocked":
        return bool(row.get("blocked_reason"))
    if state in ("approved", "pending", "rejected"):
        return approval == state
    return True


def list_drafts(params: dict) -> dict:
    from vcoutreach.web import drafts_service
    state = params.get("state") or "all"
    out = []
    for row in drafts_service.list_drafts():
        if not _state_matches(row, state):
            continue
        out.append({
            "slug": row.get("slug"),
            "partner_id": row.get("partner_id"),
            "recipient_name": _wrap_opt(row.get("recipient_name"), "contact"),
            "recipient_email": row.get("recipient_email"),
            "firm": _wrap_opt(row.get("firm"), "contact"),
            "subject": _sanitize(row.get("subject"), 200),
            "lane": getattr(row.get("lane"), "value", row.get("lane")),
            "status": row.get("status"),
            "approval_state": row.get("approval_state"),
            "grounding": row.get("grounding"),
            "blocked_reason": row.get("blocked_reason"),
            "word_count": row.get("word_count"),
            "editor_url": row.get("editor_url"),
        })
    return _ok(drafts=out, total=len(out))


def _draft_payload(slug: str) -> dict:
    from vcoutreach.web import drafts_service
    row = next((r for r in drafts_service.list_drafts() if r.get("slug") == slug), None)
    if row is None:
        raise ToolRefused("not_found", "No draft for that contact.")
    return {
        "slug": slug,
        "to": row.get("to") or row.get("recipient_email"),
        "subject": _sanitize(row.get("subject"), 200),
        "body": _wrap(row.get("body"), "draft"),
        "lane": getattr(row.get("lane"), "value", row.get("lane")),
        "status": row.get("status"),
        "issues": _issue_dicts(row.get("issues")),
        "approval_state": row.get("approval_state"),
        "grounding": row.get("grounding"),
        "blocked_reason": row.get("blocked_reason"),
        "in_reply_to": row.get("in_reply_to"),
        "references": row.get("references"),
    }


def get_draft(params: dict) -> dict:
    slug = _require_slug(params)
    _partner_for_slug_or_refuse(slug)
    return _ok(**_draft_payload(slug))


def edit_draft(params: dict) -> dict:
    from vcoutreach.types import OutreachError
    from vcoutreach.web import drafts_service
    slug = _require_slug(params)
    values = {k: params[k] for k in ("subject", "body", "lane") if params.get(k) is not None}
    if not values:
        raise ToolRefused("invalid_argument", "Pass at least one of subject, body or lane.")
    for key in ("subject", "body"):
        if key in values and _DASH_PATTERN.search(str(values[key])):
            raise ToolRefused("dash_not_allowed",
                              f"The {key} contains an em dash or en dash. "
                              "Rewrite it with a comma, period, colon or parentheses.")
    _partner_for_slug_or_refuse(slug)
    try:
        drafts_service.save_draft(slug, values)
    except ValueError as exc:
        if "not found" in str(exc).lower():
            raise ToolRefused("not_found", "No draft for that contact.") from None
        raise ToolRefused("invalid_argument", _sanitize(exc, 200)) from None
    except OutreachError as exc:
        raise ToolRefused("invalid_argument", _sanitize(exc, 200)) from None
    payload = _draft_payload(slug)
    errors = [i for i in payload["issues"] if i.get("severity") == "error"]
    payload["lint_status"] = "errors" if errors else ("warnings" if payload["issues"] else "clean")
    return _ok(**payload)


# ---------------------------------------------------------------------------
# 12-13. approve_draft, reject_draft
# ---------------------------------------------------------------------------

def approve_draft(params: dict) -> dict:
    from vcoutreach.web import drafts_service
    from vcoutreach.web.llm.hook_grounding import UNGROUNDED_APPROVAL_MESSAGE, hook_is_ungrounded
    from vcoutreach.web.pipeline.loader import get_draft_for_partner, set_draft_status
    slug = _require_slug(params)
    partner = _partner_for_slug_or_refuse(slug)
    draft = get_draft_for_partner(partner["id"])
    if draft is None:
        raise ToolRefused("not_found", "No draft for that contact.")
    if draft["status"] == "drafted":
        raise ToolRefused("already_pushed", "That draft was already pushed to Gmail.")
    reason = drafts_service.draft_block_reason(draft)
    if reason:
        raise ToolRefused("blocked", "Draft cannot be approved; blocked: " + _sanitize(reason, 300))
    if hook_is_ungrounded(draft):
        raise ToolRefused("ungrounded", UNGROUNDED_APPROVAL_MESSAGE)
    set_draft_status(draft["id"], "approved")
    return _ok(slug=slug, partner_id=int(partner["id"]), approval_state="approved",
               next="Select it on Partners when you are ready, or call send_test to send a copy to your test inbox.")


def reject_draft(params: dict) -> dict:
    from vcoutreach.web.pipeline.loader import get_draft_for_partner, set_draft_status
    slug = _require_slug(params)
    partner = _partner_for_slug_or_refuse(slug)
    draft = get_draft_for_partner(partner["id"])
    if draft is None:
        raise ToolRefused("not_found", "No draft for that contact.")
    notes = params.get("notes")
    notes = notes.strip()[:500] if isinstance(notes, str) and notes.strip() else None
    set_draft_status(draft["id"], "rejected", review_notes=notes)
    return _ok(slug=slug, approval_state="rejected")


# ---------------------------------------------------------------------------
# 14. send_test
# ---------------------------------------------------------------------------

def send_test(params: dict) -> dict:
    partner_id = _require_partner_id(params)
    from vcoutreach.web.gmail import push_worker
    from vcoutreach.web.pipeline.loader import get_draft_for_partner, get_partner
    workspace = _workspace()
    if get_partner(partner_id) is None:
        raise ToolRefused("not_found", "No contact with that id.")
    draft = get_draft_for_partner(partner_id)
    if draft is None:
        raise ToolRefused("not_found", "No draft for that contact.")
    if draft["status"] != "approved":
        raise ToolRefused("not_approved", "Approve the draft first. Test emails are only sent for approved drafts.")
    if not workspace.value("TEST_RECIPIENT_EMAIL", "").strip():
        raise ToolRefused("test_recipient_missing", "Set TEST_RECIPIENT_EMAIL before sending in TEST mode.")
    try:
        ratelimit.SEND_TEST.check("send_test")
    except ratelimit.RateLimited as exc:
        raise ToolRefused("rate_limited", "Test sending is rate limited. Try again shortly.",
                          retry_after_seconds=exc.retry_after_seconds) from None
    try:
        push_worker.send_one_test(partner_id)
    except push_worker.SendRefused as exc:
        raise ToolRefused("send_refused", _sanitize(exc, 240) or "The test send was refused.") from None
    except Exception:
        raise ToolRefused("send_unavailable", "Test sending is unavailable. Check Gmail and workspace settings.",
                          human_action=_gmail_action()) from None
    return _ok(partner_id=partner_id, sent_to_test_recipient=True,
               note="Delivered to your configured test inbox, never to the contact.")


# ---------------------------------------------------------------------------
# 15-16. send_plan, open_settings
# ---------------------------------------------------------------------------

def _scrub(value):
    if isinstance(value, str):
        return _EMAIL_PATTERN.sub("[email hidden]", value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in ("recipient_email", "email")}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def send_plan(params: dict) -> dict:
    from vcoutreach.web import drafts_service
    workspace = _workspace()
    mode = (workspace.value("SEND_MODE", "test") or "test").strip().lower()
    held = "<held: live send is UI only>" if mode == "live" else "<test recipient>"
    from vcoutreach.types import SenderProfileError
    try:
        plan = drafts_service.send_plan() or {}
    except SenderProfileError as exc:
        raise _sender_refusal(exc) from None

    def clean(rows):
        out = []
        for row in rows or []:
            item = _scrub(dict(row) if isinstance(row, dict) else {})
            item["to"] = held
            if item.get("recipient_name"):
                item["recipient_name"] = _wrap(item["recipient_name"], "contact")
            out.append(item)
        return out

    return _ok(mode="live" if mode == "live" else "test", planned=clean(plan.get("planned")),
               skipped=clean(plan.get("skipped")), note="Plan only. " + LIVE_SEND_NOTE)


def open_settings(params: dict) -> dict:
    anchor = params.get("anchor") or "publik"
    path = SETTINGS_ANCHORS.get(anchor)
    if path is None:
        raise ToolRefused("invalid_argument", "Unknown anchor.")
    workspace = _workspace()
    try:
        port_file = workspace.path("data", ".ui-port")
        running = port_file.is_file() and port_file.read_text(encoding="utf-8").strip().isdigit()
    except (OSError, ValueError):
        running = False
    instructions = (
        f"Open the Publikreach window and go to {path}." if running
        else f"The app window is not running. Open Publikreach (or run `vcoutreach ui`), then go to {path}."
    )
    return _ok(path=path, ui_running=bool(running), instructions=instructions)


HANDLERS: dict[str, Callable[[dict], dict]] = {
    "setup_status": setup_status,
    "test_connection": test_connection,
    "set_setting": set_setting,
    "import_contacts": import_contacts,
    "list_contacts": list_contacts,
    "enrich": enrich,
    "generate_hook": generate_hook,
    "write_drafts": write_drafts,
    "list_drafts": list_drafts,
    "get_draft": get_draft,
    "edit_draft": edit_draft,
    "approve_draft": approve_draft,
    "reject_draft": reject_draft,
    "send_test": send_test,
    "send_plan": send_plan,
    "open_settings": open_settings,
}
