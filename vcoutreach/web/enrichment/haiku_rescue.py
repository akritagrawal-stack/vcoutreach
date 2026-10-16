"""
Haiku Rescue Layer  -  Tier 3 email finder.

Given a partner whose email_verification_status is inconclusive/risky/null,
runs a lightweight Haiku agent loop (max 3 tool turns) to find a better email
address using web search, GitHub commits, and Twitter/Nitter bios.

LinkedIn is intentionally skipped in v1  -  the Playwright session is too heavy
for a per-partner rescue call and captcha risk is high.  Log presence and move on.

Cost target: ~$0.0006/call (Haiku 4.5 at $1/$5 per MTok, ~3-4k input + 300 output).
Hard-fail guard: aborts and returns found=False if cost estimate exceeds $0.005.

Usage:
    from vcoutreach.web.enrichment.haiku_rescue import rescue_partner_email
    result = rescue_partner_email(partner_id=15)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from vcoutreach.web.vendor import workspace_path, setting_value, validated_url, fixed_urlopen, require_dependency
from typing import Optional

logger = logging.getLogger(__name__)

# ── Model + cost constants ─────────────────────────────────────────────────────

_HAIKU_MODEL = 'claude-haiku-4-5-20251001'
_INPUT_COST_PER_TOK = 1.0 / 1_000_000   # $1/MTok
_OUTPUT_COST_PER_TOK = 5.0 / 1_000_000  # $5/MTok
_COST_HARD_LIMIT = 0.005                 # abort if we'd exceed this per-partner
_MAX_TOOL_TURNS = 3                      # cap to control costs

# ── Email regex ────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')

# ── Confidence band for verified states ───────────────────────────────────────

_VERIFIED_CONF = {
    'valid':        0.95,
    'risky':        0.50,
    'invalid':      0.00,
    'inconclusive': 0.30,
}

# ── Rate-limit: 1.5 s between GitHub or external HTTP calls ───────────────────

_last_http_call: float = 0.0


def _throttle(min_interval: float = 1.5) -> None:
    global _last_http_call
    elapsed = time.time() - _last_http_call
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_http_call = time.time()


# ── Tool implementations ───────────────────────────────────────────────────────

def _tool_web_search(query: str, count: int = 5) -> list[dict]:
    """Search SearXNG/Brave; return up to `count` {url,title,snippet} dicts."""
    try:
        from vcoutreach.web.enrichment.web_search import search
        results = search(query, count=count)
        # Truncate snippets to control token cost
        return [
            {'url': r.get('url', ''), 'title': r.get('title', '')[:120],
             'snippet': (r.get('snippet') or '')[:200]}
            for r in results[:count]
        ]
    except Exception as exc:
        logger.debug('web_search failed: %s', exc)
        return []


def _tool_github_email(name: str, firm: str) -> list[str]:
    """
    Search GitHub for commit author emails linked to name/firm.
    Uses the public GH search API  -  no auth required (60 req/hr unauthenticated).
    Returns list of unique email strings found.
    """
    try:
        import urllib.request, urllib.parse, urllib.error
        found_emails: list[str] = []

        # Build a GitHub user/org search query
        query = f'{name} {firm}'.strip()
        params = urllib.parse.urlencode({'q': query, 'type': 'users', 'per_page': 5})
        url = f'https://api.github.com/search/users?{params}'
        req = urllib.request.Request(url, headers={
            'User-Agent': 'outreach-signal.local/1.0',
            'Accept': 'application/vnd.github+json',
        })
        _throttle(1.5)
        with fixed_urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        users = [u.get('login', '') for u in data.get('items', [])[:3] if u.get('login')]

        for login in users:
            # Get user profile (may expose email if public)
            user_url = f'https://api.github.com/users/{login}'
            user_req = urllib.request.Request(user_url, headers={
                'User-Agent': 'outreach-signal.local/1.0',
                'Accept': 'application/vnd.github+json',
            })
            _throttle(1.0)
            try:
                with fixed_urlopen(user_req, timeout=10) as resp2:
                    user_data = json.loads(resp2.read().decode())
                email = user_data.get('email') or ''
                if email and '@' in email:
                    found_emails.append(email)
                    continue  # don't need commits if profile has email
            except Exception:
                pass

            # Fallback: scan commits from their recent public repos
            repos_url = f'https://api.github.com/users/{login}/repos?per_page=5&sort=pushed'
            repos_req = urllib.request.Request(repos_url, headers={
                'User-Agent': 'outreach-signal.local/1.0',
                'Accept': 'application/vnd.github+json',
            })
            _throttle(1.0)
            try:
                with fixed_urlopen(repos_req, timeout=10) as resp3:
                    repos = json.loads(resp3.read().decode())
                for repo in repos[:2]:
                    full_name = repo.get('full_name', '')
                    if not full_name:
                        continue
                    commits_url = f'https://api.github.com/repos/{full_name}/commits?per_page=5'
                    commits_req = urllib.request.Request(commits_url, headers={
                        'User-Agent': 'outreach-signal.local/1.0',
                        'Accept': 'application/vnd.github+json',
                    })
                    _throttle(1.0)
                    try:
                        with fixed_urlopen(commits_req, timeout=10) as resp4:
                            commits = json.loads(resp4.read().decode())
                        for commit in commits:
                            author = commit.get('commit', {}).get('author', {})
                            email = author.get('email', '')
                            if (email and '@' in email
                                    and 'noreply' not in email
                                    and 'users.noreply' not in email):
                                found_emails.append(email)
                    except Exception:
                        pass
            except Exception:
                pass

        return list(dict.fromkeys(found_emails))  # deduplicate, preserve order

    except Exception as exc:
        logger.debug('github_email failed for %r: %s', name, exc)
        return []


def _tool_twitter_bio(handle: str) -> str:
    """
    Fetch Twitter bio via Nitter (public, no auth).
    Returns bio text or empty string on failure.
    """
    nitter_instances = [
        'https://nitter.poast.org',
        'https://nitter.privacydev.net',
    ]
    try:
        import urllib.request, urllib.error
        handle = handle.lstrip('@')
        if not re.fullmatch(r'[A-Za-z0-9_]{1,30}', handle):
            return ''
        for base in nitter_instances:
            url = f'{base}/{handle}'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; rescue-bot/1.0)',
            })
            _throttle(1.5)
            try:
                with fixed_urlopen(req, timeout=8) as resp:
                    html = resp.read().decode('utf-8', errors='replace')
                # Extract bio from <div class="profile-bio"><p>...</p></div>
                m = re.search(r'<div[^>]*class="profile-bio"[^>]*>\s*<p[^>]*>(.*?)</p>', html, re.DOTALL)
                if m:
                    bio = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                    if bio:
                        return bio[:300]
            except Exception:
                continue
        return ''
    except Exception as exc:
        logger.debug('twitter_bio failed for %r: %s', handle, exc)
        return ''


def _tool_verify_email(email: str) -> dict:
    """
    Verify a candidate email via Emailable.
    Returns minimal dict: {state, internal_status, confidence}.
    Returns inconclusive dict on API unavailability.
    """
    try:
        from vcoutreach.web.enrichment.emailable_validator import EmailableValidator
        v = EmailableValidator()
        result = v.verify(email)
        return {
            'state':           result['state'],
            'internal_status': result['internal_status'],
            'confidence':      result['confidence'],
            'score':           result.get('score', 0),
            'reason':          result.get('reason', ''),
        }
    except ValueError:
        # EMAILABLE_API_KEY not set  -  return inconclusive
        logger.debug('Emailable not configured  -  skipping verification')
        return {
            'state': 'unknown', 'internal_status': 'inconclusive',
            'confidence': 0.30, 'score': 0, 'reason': 'api_key_missing',
        }
    except RuntimeError as exc:
        logger.warning('Emailable fatal error: %s', exc)
        return {
            'state': 'unknown', 'internal_status': 'inconclusive',
            'confidence': 0.30, 'score': 0, 'reason': str(exc),
        }
    except Exception as exc:
        logger.debug('Emailable error: %s', exc)
        return {
            'state': 'unknown', 'internal_status': 'inconclusive',
            'confidence': 0.30, 'score': 0, 'reason': str(exc),
        }


# ── Tool definitions for the Haiku agent ──────────────────────────────────────

_TOOLS = [
    {
        'name': 'web_search',
        'description': (
            'Search the web for information about a VC partner. '
            'Use targeted queries like \'[name] [firm] email contact\' or '
            '\'[name] personal site\'. Returns list of {url, title, snippet}.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Search query string'},
                'count': {'type': 'integer', 'description': 'Max results (1-5)', 'default': 5},
            },
            'required': ['query'],
        },
    },
    {
        'name': 'github_email',
        'description': (
            'Search GitHub for public commit emails linked to a person. '
            'Useful when the partner is technical or has a public GitHub presence.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'name':  {'type': 'string', 'description': 'Partner full name'},
                'firm':  {'type': 'string', 'description': 'Firm name'},
            },
            'required': ['name', 'firm'],
        },
    },
    {
        'name': 'twitter_bio',
        'description': (
            'Fetch the Twitter/X bio for a given handle via Nitter. '
            'Some founders list email in their bio.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'handle': {'type': 'string', 'description': 'Twitter handle (with or without @)'},
            },
            'required': ['handle'],
        },
    },
    {
        'name': 'verify_email',
        'description': (
            'Verify whether a candidate email address is deliverable via Emailable.com. '
            'Use this once you have a specific candidate to check.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'email': {'type': 'string', 'description': 'Email address to verify'},
            },
            'required': ['email'],
        },
    },
]


# ── Tool dispatcher ────────────────────────────────────────────────────────────

def _dispatch_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool call and return a string result for the agent."""
    if tool_name == 'web_search':
        results = _tool_web_search(
            query=tool_input.get('query', ''),
            count=min(int(tool_input.get('count', 5)), 5),
        )
        if not results:
            return 'No results found.'
        lines = [f"[{i+1}] {r['url']}\n  {r['title']}\n  {r['snippet']}" for i, r in enumerate(results)]
        return '\n'.join(lines)[:1500]  # hard cap to limit tokens

    elif tool_name == 'github_email':
        emails = _tool_github_email(
            name=tool_input.get('name', ''),
            firm=tool_input.get('firm', ''),
        )
        if not emails:
            return 'No GitHub commit emails found.'
        return 'Found commit emails: ' + ', '.join(emails[:5])

    elif tool_name == 'twitter_bio':
        bio = _tool_twitter_bio(handle=tool_input.get('handle', ''))
        if not bio:
            return 'Could not fetch Twitter bio (Nitter unavailable or empty bio).'
        return f'Bio: {bio}'

    elif tool_name == 'verify_email':
        result = _tool_verify_email(email=tool_input.get('email', ''))
        return json.dumps(result)

    else:
        return f'Unknown tool: {tool_name}'


# ── Cost estimator ────────────────────────────────────────────────────────────

def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return input_tokens * _INPUT_COST_PER_TOK + output_tokens * _OUTPUT_COST_PER_TOK


# ── Main rescue function ───────────────────────────────────────────────────────

def rescue_partner_email(partner_id: int, db_path: Optional[Path] = None) -> dict:
    """
    Run the Haiku rescue agent for a single partner.

    Looks up partner data from DB, runs a multi-turn tool-using Haiku call
    to find a better email address, optionally verifies via Emailable, and
    returns a structured result dict.

    Args:
        partner_id: DB row id in the partners table.
        db_path:    Path to outreach.db (defaults to app.config.DB_PATH).

    Returns:
        {
          partner_id, found, email, source, confidence, verified_state,
          alternates, cost_estimate_usd, log
        }
    """
    log: list[str] = []
    alternates: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0

    # ── 1. Load partner from DB ────────────────────────────────────────────────
    if db_path is None:
        try:
            from vcoutreach.web.config import DB_PATH
            db_path = DB_PATH
        except ImportError:
            db_path = workspace_path('data/outreach.db')

    from vcoutreach.web.db import get_conn
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            'SELECT id, partner_name, firm, firm_domain, email, '
            'linkedin_url, twitter_handle, email_verification_status '
            'FROM partners WHERE id = ?',
            (partner_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return _empty_result(partner_id, 'Partner not found in DB')

    name        = row['partner_name'] or ''
    firm        = row['firm'] or ''
    firm_domain = row['firm_domain'] or ''
    current_email = row['email'] or ''
    linkedin_url  = row['linkedin_url'] or ''
    twitter_handle = row['twitter_handle'] or ''

    log.append(f'Partner: {name} / {firm} | current_email={current_email} | '
               f'status={row["email_verification_status"]}')

    if linkedin_url:
        log.append(f'LinkedIn URL present ({linkedin_url[:60]}...)  -  '
                   'skipped in v1 (Playwright too heavy; captcha risk)')

    # ── 2. Web search for context (1-2 results, bounded timeout) ─────────────
    search_context = ''
    try:
        search_results = _tool_web_search(
            f'"{name}" "{firm}" email contact', count=2
        )
        if search_results:
            lines = []
            for i, r in enumerate(search_results):
                lines.append(f"[{i+1}] {r['url']}\n  {r['title']}\n  {r['snippet']}")
            search_context = '\n'.join(lines)
            log.append(f'Web search: {len(search_results)} result(s)')
        else:
            log.append('Web search: no results')
    except Exception as exc:
        log.append(f'Web search error (proceeding without): {exc}')

    # ── 3. Build single Haiku prompt ──────────────────────────────────────────
    system_prompt = (
        'You are an email-finding research assistant. '
        'Based on the partner information and optional web search results provided, '
        'suggest the single most likely work email address. '
        'Apply common VC firm email patterns (first@example.com, firstname.lastname@example.com, etc.). '
        'Respond ONLY with a valid JSON object  -  no markdown, no explanation outside the JSON. '
        'JSON format: {"email": "address@example.com", "confidence": 0.0-1.0, "reasoning": "brief explanation"}. '
        'If you cannot make a reasonable guess, set email to null and confidence to 0.'
    )

    context_parts = [f'Name: {name}', f'Firm: {firm}']
    if firm_domain:
        context_parts.append(f'Firm domain: {firm_domain}')
    if current_email:
        context_parts.append(f'Current email (may be wrong): {current_email}')
    if linkedin_url:
        context_parts.append(f'LinkedIn: {linkedin_url[:80]} (noted; not fetched)')

    user_prompt = (
        'Find the most likely work email for this VC partner.\n\n'
        + '\n'.join(context_parts)
    )
    if search_context:
        user_prompt += f'\n\nWeb search results:\n{search_context}'
    user_prompt += '\n\nRespond with JSON only.'

    # ── 4. LLM call  -  Vertex Gemini primary, Claude Haiku fallback ───────────
    #    Uses the project's existing chain.py (Vertex works without Anthropic key).
    full_prompt = f'{system_prompt}\n\n{user_prompt}'
    raw_text: Optional[str] = None
    model_used = 'unknown'
    final_answer: Optional[dict] = None

    # Try Vertex Gemini first (uses GCP service account, no Anthropic key needed)
    try:
        from vcoutreach.web.llm.chain import call_llm
        raw_text = call_llm(full_prompt, json_mode=False)
        model_used = "auto"
        log.append(f'LLM call succeeded via {model_used}')
    except Exception as vertex_exc:
        log.append(f'Vertex Gemini failed: {vertex_exc}')
        # Fall back to Claude Haiku if ANTHROPIC_API_KEY is available
        try:
            anthropic = require_dependency("anthropic")
            from vcoutreach.web.server.runtime_secrets import load_provider_key
            api_key = load_provider_key('anthropic')
            if not api_key:
                raise RuntimeError('ANTHROPIC_API_KEY is not configured')
            haiku_client = anthropic.Anthropic(api_key=api_key, base_url="https://api.anthropic.com", timeout=30.0, max_retries=0)
            haiku_resp = haiku_client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=300,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_prompt}],
            )
            usage = getattr(haiku_resp, 'usage', None)
            if usage:
                total_input_tokens  += getattr(usage, 'input_tokens', 0)
                total_output_tokens += getattr(usage, 'output_tokens', 0)
            raw_text = haiku_resp.content[0].text.strip() if haiku_resp.content else ''
            model_used = _HAIKU_MODEL
            log.append(f'LLM call succeeded via Haiku (in={getattr(usage, "input_tokens", "?")} tokens)')
        except Exception as haiku_exc:
            log.append(f'Claude Haiku also failed: {haiku_exc}')
            return _empty_result(
                partner_id,
                f'All LLM calls failed. Vertex: {vertex_exc}; Haiku: {haiku_exc}',
                log=log,
            )

    # Extract JSON from raw_text
    if raw_text:
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            try:
                final_answer = json.loads(json_match.group())
                log.append(f'JSON extracted from {model_used} response')
            except json.JSONDecodeError:
                log.append(f'Could not parse JSON from {model_used}: {raw_text[:200]}')

    # ── 5. Parse LLM answer ───────────────────────────────────────────────────
    cost_usd = _estimate_cost(total_input_tokens, total_output_tokens)

    if final_answer is None:
        log.append(f'No JSON answer from {model_used}')
        return {
            'partner_id':        partner_id,
            'found':             False,
            'email':             None,
            'source':            'haiku_rescue',
            'confidence':        0.0,
            'verified_state':    'inconclusive',
            'alternates':        alternates,
            'cost_estimate_usd': cost_usd,
            'log':               log,
        }

    email      = final_answer.get('email') or None
    confidence = float(final_answer.get('confidence', 0.0))
    reasoning  = final_answer.get('reasoning', '')
    if reasoning:
        log.append(f'LLM reasoning: {reasoning[:200]}')

    # Reject if Haiku just echoed back the current email unchanged
    if email and email.lower() == current_email.lower():
        log.append(f'Haiku returned same email as current  -  treating as not found')
        email = None
        confidence = 0.0

    found = bool(email and confidence > 0.0)

    # ── 6. Verify the candidate via Emailable ────────────────────────────────
    verified_state = 'inconclusive'
    if found and email:
        log.append(f'Verifying candidate via Emailable: {email}')
        ver = _tool_verify_email(email)
        verified_state = ver.get('internal_status', 'inconclusive')
        # Use Emailable confidence if it gives a stronger signal
        if ver.get('state') == 'deliverable':
            confidence = ver.get('confidence', confidence)
        elif ver.get('state') == 'undeliverable':
            confidence = 0.0
            found = False
        log.append(f'Emailable: state={ver.get("state")} / {verified_state} '
                   f'(score={ver.get("score")}, confidence={confidence:.2f})')

    # Collect any extra emails from search context
    extra_emails = [
        e for e in _EMAIL_RE.findall(search_context)
        if 'noreply' not in e and e != current_email and e != email
    ]
    for e in extra_emails[:3]:
        if e not in alternates:
            alternates.append(e)

    log.append(f'Total cost estimate: ${cost_usd:.6f} '
               f'(input_tokens={total_input_tokens}, output_tokens={total_output_tokens})')

    return {
        'partner_id':        partner_id,
        'found':             found,
        'email':             email if found else None,
        'source':            'haiku_rescue',
        'confidence':        confidence,
        'verified_state':    verified_state,
        'alternates':        alternates[:5],
        'cost_estimate_usd': cost_usd,
        'log':               log,
    }


def _empty_result(partner_id: int, reason: str, log: Optional[list] = None) -> dict:
    return {
        'partner_id':        partner_id,
        'found':             False,
        'email':             None,
        'source':            'haiku_rescue',
        'confidence':        0.0,
        'verified_state':    'inconclusive',
        'alternates':        [],
        'cost_estimate_usd': 0.0,
        'log':               (log or []) + [f'Aborted: {reason}'],
    }
