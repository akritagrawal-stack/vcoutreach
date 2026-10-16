"""Public hook and subject generation with deterministic validation."""
from __future__ import annotations
from vcoutreach.web.llm.chain import call_llm, ChainError
from vcoutreach.web.llm.prompts import build_hook_prompt
from vcoutreach.web.llm.validation import validate_hook, validate_subject, postprocess
from vcoutreach.web.vendor import load_sender_context


class HookGenerationError(Exception):
    """Hook generation failed without publishing a partial draft."""


def generate_hook(partner: dict, enrichment_facts: list[dict] | None = None, max_retries: int = 2, model_pref: str = "auto", allow_no_enrichment: bool = False, mode: str = "outreach", *, sender_profile: dict | None = None, facts: dict | None = None) -> dict:
    if not allow_no_enrichment and not enrichment_facts:
        raise HookGenerationError("Cannot generate a hook without enrichment facts. Enrich the contact first.")
    sender_profile, facts = load_sender_context(sender_profile if sender_profile is not None else partner.get("sender_profile"), facts if facts is not None else partner.get("sender_facts"))
    base = build_hook_prompt(partner, enrichment_facts, mode, sender_profile=sender_profile, facts=facts)
    reason = "no valid output"
    for attempt in range(1, max(0, min(max_retries, 5)) + 2):
        try:
            raw = call_llm(base + ("\nPrevious rejection: " + reason if attempt > 1 else ""), model_pref=model_pref)
        except ChainError as exc:
            raise HookGenerationError(str(exc)) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("hook"), str) or not isinstance(raw.get("subject"), str):
            reason = "insufficient evidence or invalid output"
            continue
        hook, subject = postprocess(raw["hook"]), postprocess(raw["subject"])
        valid, reason = validate_hook(hook, sender_profile=sender_profile, facts=facts)
        if not valid:
            continue
        valid, reason = validate_subject(subject, sender_profile=sender_profile, facts=facts)
        if valid:
            return {"hook": hook, "subject": subject, "model_used": raw.get("_model_used", "unknown"), "attempts": attempt}
    raise HookGenerationError("Hook validation failed: " + str(reason))
