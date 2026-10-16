"""Gemini quality and latency probe for workspace outreach drafts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import tempfile
from pathlib import Path
from vcoutreach.web.vendor import workspace_path, setting_value
import re
from time import perf_counter

from vcoutreach.web.server.runtime_secrets import RuntimeSecretError, load_gemini_key


from vcoutreach.web.config import WorkspacePath
STATUS_PATH = WorkspacePath("data", "gemini-quality-status.json")
MODEL = setting_value("GEMINI_MODEL", "gemini-2.5-flash-lite")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s)]+", re.IGNORECASE)


class GeminiQualityError(ValueError):
    """A content-free Gemini benchmark failure."""


def _safe_number(value) -> int:
    try:
        return max(0, min(10000000, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_status(payload: dict | None = None) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    return {
        "configured": bool(source.get("configured", False)),
        "tested": bool(source.get("tested", False)),
        "ok": bool(source.get("ok", False)),
        "model": str(source.get("model") or MODEL)[:80],
        "latency_ms": _safe_number(source.get("latency_ms")),
        "quality_score": _safe_number(source.get("quality_score")),
        "sample_count": _safe_number(source.get("sample_count")),
        "tested_at": str(source.get("tested_at") or "")[:40],
        "error_code": str(source.get("error_code") or "")[:80],
        "raw_content_stored": False,
        "delivery_authority": False,
    }


def load_gemini_status(*, configured: bool) -> dict[str, object]:
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        payload = {}
    status = _safe_status(payload)
    status["configured"] = configured
    return status


def _write_status(payload: dict[str, object]) -> dict[str, object]:
    status = _safe_status(payload)
    STATUS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATUS_PATH.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".gemini-quality-", dir=STATUS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(status, handle, sort_keys=True, separators=(",", ":"))
        os.replace(temporary, STATUS_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return status


def _anonymize_record(record: dict) -> str:
    body = str(record.get("body") or "")
    recipient_name = str(record.get("recipient_name") or "").strip()
    firm_name = str(record.get("firm_name") or "").strip()
    body = _EMAIL.sub("[email hidden]", body)
    body = _URL.sub("[link]", body)
    if firm_name:
        body = re.sub(re.escape(firm_name), "[Firm]", body, flags=re.IGNORECASE)
    if recipient_name:
        body = re.sub(re.escape(recipient_name), "[Recipient]", body, flags=re.IGNORECASE)
        first_name = recipient_name.split()[0]
        body = re.sub(rf"\b{re.escape(first_name)}\b", "[Recipient]", body, flags=re.IGNORECASE)
    return body[:6000]


def load_anonymized_campaign_samples(limit: int = 3) -> list[str]:
    samples = []
    drafts_dir = workspace_path("drafts")
    for path in sorted(drafts_dir.glob("*.md")):
        if len(samples) >= max(1, min(limit, 5)):
            break
        if path.is_symlink() or not path.resolve().is_relative_to(drafts_dir) or path.stat().st_size > 65536:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if body.strip():
            samples.append(_anonymize_record({"body": body}))
    if not samples:
        raise GeminiQualityError("workspace_drafts_unavailable")
    return samples


def _extract_json(text: str) -> dict:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        object_match = re.search(r"(\{.*\})", candidate, re.DOTALL)
        if object_match:
            candidate = object_match.group(1)
    try:
        parsed = json.loads(candidate)
    except (ValueError, json.JSONDecodeError) as exc:
        raise GeminiQualityError("gemini_response_invalid") from exc
    if not isinstance(parsed, dict):
        raise GeminiQualityError("gemini_response_invalid")
    return parsed


def _error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "no longer available" in message or ("404" in message and "model" in message):
        return "gemini_model_unavailable"
    if "prepayment credits are depleted" in message or "manage your project and billing" in message:
        return "gemini_billing_depleted"
    if "401" in message or "403" in message or "api key" in message:
        return "gemini_auth_failed"
    if "429" in message or "quota" in message or "rate" in message:
        return "gemini_rate_limited"
    if "timeout" in message or "deadline" in message:
        return "gemini_timeout"
    return "gemini_request_failed"


def run_quality_probe() -> dict[str, object]:
    try:
        key = load_gemini_key()
    except RuntimeSecretError as exc:
        raise GeminiQualityError(str(exc)) from exc
    if key is None:
        raise GeminiQualityError("gemini_key_missing")

    samples = load_anonymized_campaign_samples(limit=3)
    prompt = (
        "Audit the supplied workspace outreach drafts. Return JSON only with "
        "quality_score as an integer from 0 to 100 and verdict as one of strong, "
        "usable, weak. Score specificity, clarity, concise structure, whether the "
        "personalization paragraph discusses the recipient before introducing the "
        "product, whether the greeting appears only once, and whether claims are "
        "internally consistent. Do not quote, reproduce, name, or summarize any "
        "recipient. Do not include the email text in the output.\n\n"
        + "\n\n--- SAMPLE ---\n\n".join(samples)
    )

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:
        raise GeminiQualityError('Gemini needs google-genai. Run: pip install -e ".[integrations]"') from exc

    started = perf_counter()
    try:
        client = genai.Client(api_key=key, vertexai=False, http_options=genai_types.HttpOptions(base_url="https://generativelanguage.googleapis.com", timeout=30000))
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=256,
            ),
        )
        raw = response.text or ""
    except Exception as exc:
        code = _error_code(exc)
        status = _write_status(
            {
                "configured": True,
                "tested": True,
                "ok": False,
                "model": MODEL,
                "latency_ms": round((perf_counter() - started) * 1000),
                "quality_score": 0,
                "sample_count": len(samples),
                "tested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "error_code": code,
            }
        )
        raise GeminiQualityError(code) from exc

    parsed = _extract_json(raw)
    try:
        score = max(0, min(100, int(parsed.get("quality_score"))))
    except (TypeError, ValueError) as exc:
        raise GeminiQualityError("gemini_response_invalid") from exc
    verdict = str(parsed.get("verdict") or "").lower()
    if verdict not in {"strong", "usable", "weak"}:
        verdict = "usable" if score >= 65 else "weak"
    status = _write_status(
        {
            "configured": True,
            "tested": True,
            "ok": True,
            "model": MODEL,
            "latency_ms": round((perf_counter() - started) * 1000),
            "quality_score": score,
            "sample_count": len(samples),
            "tested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error_code": "",
        }
    )
    status["verdict"] = verdict
    return status


__all__ = [
    "GeminiQualityError",
    "load_anonymized_campaign_samples",
    "load_gemini_status",
    "run_quality_probe",
]
