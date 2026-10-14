"""Draft pipeline: contacts to lint-clean draft files."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .drafts_io import prune_stale, read_index_ids, write_draft, write_index
from .grounding import check_grounding
from .lint import lint_draft
from .templates import render_body, render_subject
from .types import (
    Contact,
    Draft,
    InsufficientFacts,
    Lane,
    LintIssue,
    PersonalizationProvider,
    PersonalizationRequest,
    SenderProfile,
)


def _blocked(contact: Contact, subject: str, issue: LintIssue) -> Draft:
    return Draft(
        contact_id=contact.id,
        to=contact.email,
        subject=subject,
        body="",
        status="blocked",
        hook=None,
        issues=(issue,),
        lane=contact.lane,
        in_reply_to=contact.in_reply_to if contact.lane == Lane.FOLLOW_UP else "",
        references=contact.references if contact.lane == Lane.FOLLOW_UP else "",
    )


def _draft_one(contact: Contact, sender: SenderProfile, provider: PersonalizationProvider) -> Draft:
    subject = render_subject(contact, sender)

    # 1. Without facts only the offline template provider may continue: it reports
    #    InsufficientFacts itself. A paid or remote model is never asked to guess.
    if not contact.facts and getattr(provider, "name", "") != "template":
        return _blocked(
            contact,
            subject,
            LintIssue(
                "no_facts",
                "contact %s has no facts; add verified facts to the facts column" % contact.email,
            ),
        )

    # 2. Ask the provider. InsufficientFacts blocks this contact; every other
    #    ProviderError propagates and aborts the run.
    try:
        hook = provider.personalize(PersonalizationRequest(contact=contact, sender=sender))
    except InsufficientFacts as exc:
        detail = str(exc).strip()
        message = "the provider could not write a hook from the facts"
        if detail:
            message += ": " + detail
        return _blocked(contact, subject, LintIssue("insufficient_facts", message))

    # 3. Assemble the email from the templates.
    hook_text = hook.text if hook.text is not None else ""
    body = render_body(contact, sender, hook_text)

    # 4. Lint and ground. Any error blocks the draft.
    issues: List[LintIssue] = list(lint_draft(subject, body, sender, contact, hook_text))
    issues.extend(check_grounding(hook_text, contact, sender))
    if hook_text == "" and not any(i.code == "empty_hook" for i in issues):
        # lint_draft skips hook checks for an empty string (send time re-lint), so
        # the pipeline reports an empty hook itself.
        issues.append(LintIssue("empty_hook", "the hook is empty"))
    status = "blocked" if any(i.severity == "error" for i in issues) else "ok"

    follow_up = contact.lane == Lane.FOLLOW_UP
    return Draft(
        contact_id=contact.id,
        to=contact.email,
        subject=subject,
        body=body,
        status=status,
        hook=hook,
        issues=tuple(issues),
        lane=contact.lane,
        in_reply_to=contact.in_reply_to if follow_up else "",
        references=contact.references if follow_up else "",
    )


def draft_all(
    contacts: Sequence[Contact],
    sender: SenderProfile,
    provider: PersonalizationProvider,
    out_dir: Path,
    on_prune: Optional[Callable[[str], None]] = None,
) -> List[Draft]:
    """Draft every contact in order, write the files and drafts/index.jsonl.

    Returns all drafts (ok and blocked) in input order. A ProviderError other than
    InsufficientFacts stops the run and propagates. Drafts finished before the failure
    stay on disk and are listed in the index.

    After a complete successful run, draft files of contacts that were listed in the
    previous index.jsonl but are no longer in ``contacts`` are deleted, and ``on_prune``
    (when given) is called once with each removed id. Nothing is pruned after an abort.
    """
    out_dir = Path(out_dir)
    # Read the previous ids before anything is written: write_index replaces the file.
    previous_ids = read_index_ids(out_dir)
    drafts: List[Draft] = []
    try:
        for contact in contacts:
            draft = _draft_one(contact, sender, provider)
            write_draft(out_dir, draft)
            drafts.append(draft)
    except BaseException:
        if drafts:
            try:
                write_index(out_dir, drafts)
            except OSError:
                pass
        raise
    write_index(out_dir, drafts)
    removed = prune_stale(out_dir, previous_ids, [d.contact_id for d in drafts])
    if on_prune is not None:
        for contact_id in removed:
            on_prune(contact_id)
    return drafts


def format_summary(drafts: Sequence[Draft], provider_name: str) -> str:
    """The run summary line: drafted=<ok count> blocked=<blocked count> provider=<name>."""
    drafted = sum(1 for d in drafts if d.status == "ok")
    blocked = len(drafts) - drafted
    return "drafted=%d blocked=%d provider=%s" % (drafted, blocked, provider_name)
