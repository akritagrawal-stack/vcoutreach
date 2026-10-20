"""Public hook and subject generation with deterministic validation."""
from __future__ import annotations
import json
from vcoutreach.grounding import unsupported_metrics, _METRIC
from vcoutreach.web.llm.chain import call_llm, ChainError
from vcoutreach.web.llm.prompts import build_hook_prompt
from vcoutreach.web.llm.validation import validate_hook, validate_subject, postprocess
from vcoutreach.web.llm.hook_grounding import check_hook_grounding
from vcoutreach.web.vendor import load_sender_context
from vcoutreach.email_copy import clean_opener


class HookGenerationError(Exception):
    """Hook generation failed without publishing a partial draft."""


def _fact_texts(value) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return [value]
        return _fact_texts(decoded) if isinstance(decoded, (dict, list)) else [value]
    if isinstance(value, dict):
        if 'text' in value or 'value' in value:
            return _fact_texts(value.get('text') or value.get('value'))
        return [text for key, item in value.items()
                if key not in {'source_url', 'source_urls', 'url', 'urls', 'confidence', 'forbidden_phrases',
                               'id', 'partner_id', 'provider', 'type', 'created_at', 'updated_at', 'timestamp'}
                for text in _fact_texts(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _fact_texts(item)]
    return [str(value)] if isinstance(value, (int, float)) else []


def generate_hook(partner: dict, enrichment_facts: list[dict] | None = None, max_retries: int = 2, model_pref: str = "auto", allow_no_enrichment: bool = False, mode: str = "outreach", *, sender_profile: dict | None = None, facts: dict | None = None) -> dict:
    if not allow_no_enrichment and not enrichment_facts:
        raise HookGenerationError("Cannot generate a hook without enrichment facts. Enrich the contact first.")
    sender_profile, facts = load_sender_context(sender_profile if sender_profile is not None else partner.get("sender_profile"), facts if facts is not None else partner.get("sender_facts"))
    base = build_hook_prompt(partner, enrichment_facts, mode, sender_profile=sender_profile, facts=facts)
    metric_sources = (_fact_texts(sender_profile.get('approved_claims', {}))
                      + _fact_texts(facts) + _fact_texts(partner.get('facts'))
                      + _fact_texts(enrichment_facts))
    base += (
        '\nNever invent traction numbers, user counts, revenue or view counts. '
        'Only numbers appearing in approved claims or provided facts may be used. '
        'Keep about in about 10M organic views when that claim is supplied. '
        'When the supplied profile and facts have no metric, the hook must contain no number at all. '
        'IDs, source URLs, confidence scores and list indexes are not metric evidence.'
    )
    reason = "no valid output"
    ungrounded = None
    for attempt in range(1, max(0, min(max_retries, 5)) + 2):
        try:
            feedback = ''
            if attempt > 1:
                feedback = '\nPrevious rejection: ' + str(reason)
                if 'too short' in str(reason):
                    feedback += '. Lengthen the opener to 30 to 40 words using only the supplied facts; keep 1 to 2 sentences.'
                feedback += '\nRewrite the rejected output: ' + str(previous)
            raw = call_llm(base + feedback, model_pref=model_pref)
        except ChainError as exc:
            raise HookGenerationError(str(exc)) from exc
        previous = raw
        if not isinstance(raw, dict) or not isinstance(raw.get("hook"), str) or not isinstance(raw.get("subject"), str):
            reason = str(raw.get('reason') or 'insufficient evidence or invalid output') if isinstance(raw, dict) else 'invalid output'
            continue
        first_name = partner.get('first_name') or str(partner.get('partner_name') or partner.get('name') or '').split(' ')[0]
        hook, subject = clean_opener(postprocess(raw["hook"]), first_name), postprocess(raw["subject"])
        valid, reason = validate_hook(hook, sender_profile=sender_profile, facts=facts)
        if not valid:
            continue
        valid, reason = validate_subject(subject, sender_profile=sender_profile, facts=facts)
        if not valid:
            continue
        grounded, why = check_hook_grounding(hook, partner, enrichment_facts, sender_profile, facts)
        unsupported = unsupported_metrics(hook + '\n' + subject, metric_sources)
        if unsupported:
            grounded, why = False, 'unsupported metric or changed qualifier: ' + ', '.join(unsupported)
        elif not grounded and why.startswith('number "'):
            # The legacy gate compares numeric spellings literally. Once the
            # normalized check passes, retain its remaining name/claim checks.
            grounded, why = check_hook_grounding(_METRIC.sub('', hook), partner, enrichment_facts, sender_profile, facts)
        result = {"hook": hook, "subject": subject, "model_used": raw.get("_model_used", "unknown"), "attempts": attempt,
                  "hallucination_check": "grounded" if grounded else "ungrounded: " + why}
        if grounded:
            return result
        ungrounded, reason = result, "ungrounded claim, use only the supplied facts: " + why
    if ungrounded is not None:
        return ungrounded
    raise HookGenerationError("Hook validation failed: " + str(reason))
