"""
Reply classifier - uses call_llm() to classify a VC partner's reply email.

Returns a dict with:
    label:                  one of VALID_LABELS
    confidence:             float 0.0-1.0
    summary:                one-sentence summary of the reply
    suggested_action:       short imperative ('Reply with calendar link', etc.)
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

VALID_LABELS = frozenset([
    'interested',
    'pass',
    'needs_more_info',
    'out_of_office',
    'bounce',
    'auto_reply',
    'other',
])

# Patterns that mark the start of quoted/forwarded text - strip everything below.
_QUOTE_RE = re.compile(
    r'\n[ \t]*>.*',               # "> quoted line" style
    re.DOTALL,
)
_ON_WROTE_RE = re.compile(
    r'\nOn .{5,80}(?:wrote|said)\s*:',
    re.IGNORECASE | re.DOTALL,
)
_FROM_SENT_RE = re.compile(
    r'\n-{3,}.*?Original Message.*?-{3,}',
    re.IGNORECASE | re.DOTALL,
)
_FROM_LINE_RE = re.compile(
    r'\nFrom:\s.+?\nSent:\s.+?\nTo:\s',
    re.IGNORECASE | re.DOTALL,
)


def _strip_quoted_text(text: str) -> str:
    """Remove quoted / forwarded text before passing to the LLM."""
    for pattern in (_ON_WROTE_RE, _FROM_SENT_RE, _FROM_LINE_RE, _QUOTE_RE):
        m = pattern.search(text)
        if m:
            text = text[:m.start()]
    return text.strip()


_CLASSIFY_PROMPT = """\
You are classifying a reply email from a VC partner to a cold outreach email from a startup founder.

Classify the reply into EXACTLY ONE of these labels:
- "interested"       : wants to chat / schedules meeting / asks to send Calendly /
                       expresses real enthusiasm / forwards to a colleague on their team
- "pass"             : declines, "not a fit", "passing for now", "out of scope",
                       "not investing in this space"
- "needs_more_info"  : asks for deck / pitch deck / financials / team info /
                       customer references / more context - engaged but not yet committed.
                       Also use this for "send me the deck" style short replies.
- "out_of_office"    : vacation, OOO, maternity leave, parental leave, or any
                       auto-reply explicitly mentioning absence / limited access
- "bounce"           : delivery failure - postmaster, mailer-daemon, MAILER-DAEMON,
                       "address does not exist", "user unknown", "undeliverable"
- "auto_reply"       : generic "received your message" / ticket confirmation /
                       Calendly scheduling confirmation NOT driven by a human
- "other"            : ambiguous, spam, unrelated, cannot classify with confidence

EXAMPLES:
Reply: "Thanks for reaching out. Not a fit for us right now - passing."
→ label: "pass"

Reply: "Yes! Free Tuesday 3pm CT? Send a Calendly."
→ label: "interested", suggested_action: "Send Calendly link"

Reply: "Can you send over the deck and some traction numbers?"
→ label: "needs_more_info", suggested_action: "Send pitch deck and metrics"

Reply: "I'm out of office until June 2 with limited access to email."
→ label: "out_of_office", suggested_action: "Follow up after June 2"

Reply: "Delivery to the following recipient failed permanently: john@example.com"
→ label: "bounce", suggested_action: "Mark closed, find alternate email"

Reply: "Thanks for contacting us. Your message has been received (ticket #4821)."
→ label: "auto_reply", suggested_action: "No action"

Reply: "Forwarding this to Alex on our team who covers EdTech."
→ label: "interested", suggested_action: "Follow up with Alex"

Now classify this reply:
---
PARTNER NAME (hint): {partner_name}
REPLY TEXT:
{reply_text}
---

Respond with ONLY a JSON object (no markdown, no prose):
{{
  "label": "<one of the 7 labels>",
  "confidence": <float 0.0-1.0>,
  "summary": "<one sentence summarising what the reply says>",
  "suggested_action": "<short imperative, e.g. 'Reply with calendar link' / 'Send deck' / 'Mark closed' / 'No action'>"
}}"""


def classify_reply(reply_text: str, partner_name: str | None = None) -> dict:
    """
    Classify a VC partner reply email.

    Args:
        reply_text:   Plain-text body of the reply (quoted text will be stripped).
        partner_name: Optional name hint for the LLM prompt.

    Returns dict with keys: label, confidence, summary, suggested_action.
    """
    from vcoutreach.web.llm.chain import call_llm, ChainError

    cleaned = _strip_quoted_text(reply_text or '')
    if not cleaned:
        return {
            'label': 'other',
            'confidence': 0.0,
            'summary': 'Empty reply - could not classify.',
            'suggested_action': 'No action',
        }

    prompt = _CLASSIFY_PROMPT.format(
        partner_name=partner_name or 'Unknown',
        reply_text=cleaned[:3000],  # cap to avoid token blow-up on forwarded threads
    )

    try:
        result = call_llm(prompt, json_mode=True)
    except ChainError as exc:
        logger.error('classify_reply: LLM chain failed: %s', exc)
        return {
            'label': 'other',
            'confidence': 0.0,
            'summary': f'Classification failed: {exc}',
            'suggested_action': 'Retry later',
        }

    # Validate and sanitize
    label = result.get('label', 'other')
    if label not in VALID_LABELS:
        logger.warning('classify_reply: unexpected label %r - coercing to "other"', label)
        label = 'other'

    try:
        confidence = float(result.get('confidence', 0.5))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        'label': label,
        'confidence': confidence,
        'summary': str(result.get('summary', '')),
        'suggested_action': str(result.get('suggested_action', 'No action')),
    }
