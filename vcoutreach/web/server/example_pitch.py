"""The shipped example pitch: a complete, worked cold email, shown as labeled parts.

This module is pure (no Flask, no workspace access) so the web app, the MCP server and the
settings defaults all read the same text. It is a redacted copy of a real email that got replies.
Only the most personal details are replaced by placeholders (booking link, email, phone); the
company details and the founder framing are kept on purpose, because a believable example teaches
more than a blank form.

Rules for anyone editing the text below:
* Numbers come only from the approved facts file. The figures now are 25M+ views on the open source
  videos, 10K users and 2,400 Discord members. Keep that wording exactly.
* No credibility line such as "50+ meetings" or a raise amount. That belongs only in the app panel.
* No em dash or en dash anywhere.
"""
from __future__ import annotations

import json
import re

EXAMPLE_LABEL = "Example from Publik, replace with your own"

EXAMPLE_SUBJECT = "Texas A&M dropout working on Publik"
EXAMPLE_HOOK = ("I liked Example Capital's April take on helping nontechnical people actually adopt AI. "
                "That gap is exactly what we noticed with open source, and why we're building Publik.")
EXAMPLE_FOUNDER_LINE = ("My cofounder and I started by open-sourcing alternatives to Cluely, Wispr Flow and "
                        "Perplexity.")
EXAMPLE_PROBLEM = ("Our videos reached 25M+ views, but people still struggled to install, maintain and "
                   "customize the software.")
EXAMPLE_SOLUTION = ("Publik makes those apps accessible through our product Iris, which handles setup, fixes "
                    "bugs and adds features around each user's needs.")
EXAMPLE_ONE_LINER = ("We're building a marketplace where software adapts to people, rather than people adapting "
                     "to fixed subscriptions.")
EXAMPLE_TRACTION = ("We've crossed 10K users and have 2,400 Discord members. "
                    "We're raising a pre-seed round.")
BOOKING_PLACEHOLDER = "[your booking link]"
EXAMPLE_ASK = f"Deck attached. Open to a quick chat? Here's my booking link: {BOOKING_PLACEHOLDER}"
EXAMPLE_DECK_LABEL = "Publik pitch deck (example)"
EXAMPLE_SIGNATURE = "Akrit Agrawal, Co-founder and COO, Publik"

# The blurb the Settings page edits. Placeholders are filled from the other settings fields.
EXAMPLE_BLURB_TEMPLATE = (
    "Hi {first_name},\n\n"
    "{hook}\n\n"
    "{founder_line} {problem_statement}\n\n"
    "{solution_statement} {one_liner}\n\n"
    "{traction_line}\n\n"
    "{ask_line}\n\n"
    "{operator_name}, {operator_role}, {company_name}"
)

# One entry per labeled part: (key, label, text, why this works).
_PARTS = (
    ("subject", "Subject", EXAMPLE_SUBJECT,
     "Short and specific. A human fact about the sender gets opened more than a generic introduction."),
    ("hook", "Hook", EXAMPLE_HOOK,
     "One specific thing the recipient said or funded, then one true sentence linking it to what you build. "
     "Never flattery, never a check size."),
    ("story", "Story", f"{EXAMPLE_FOUNDER_LINE} {EXAMPLE_PROBLEM}",
     "Say what you built first and what you learned from the people who tried it."),
    ("product", "Product", f"{EXAMPLE_SOLUTION} {EXAMPLE_ONE_LINER}",
     "Say what your product does for the person you just described, then the bigger idea in one sentence."),
    ("traction", "Traction", EXAMPLE_TRACTION,
     "Only numbers you can prove, each attached to what earned it. Say you are raising only when you are."),
    ("ask", "Ask", EXAMPLE_ASK,
     "One small ask with one link. Mention the attachment so nobody hunts for it."),
    ("signature", "Signature", EXAMPLE_SIGNATURE,
     "Name, role, company. Nothing else, so the email still reads like a person wrote it."),
)

WRITING_GUIDANCE = (
    "Open with one verified fact about the recipient, then say why it matters to them.",
    "Keep the whole email under about 150 words and use short paragraphs.",
    "Say what you built and what you learned, then what your product does, in plain words.",
    "Use only numbers you can prove, and attach each to what earned it.",
    "End with one small ask and one link. Mention the attached deck.",
    "Never invent facts, quotes or mutual connections. Leave a placeholder if unsure.",
)


KINDS = ("fundraising", "job_search")
DEFAULT_KIND = "fundraising"

# Job search example: placeholders only. No numbers, no claims, nothing that could be sent as a fact.
JOB_LABEL = "Placeholder example, replace every [slot] with your own details"
JOB_SUBJECT = "[Role you want] at [Company]"
JOB_HOOK = "[One verified fact about the company or the hiring manager, and why it made you write to them.]"
JOB_FOUNDER_LINE = "I'm [your name], [one plain sentence about who you are and what you do]."
JOB_ONE_LINER = "I'm looking for a [role] where I can [what you want to do for them]."
JOB_STORY = ("[Your strongest result, in one sentence: what you did, for whom, and what changed.] "
             "[One more sentence on why that experience fits this team.]")
JOB_RESULTS = "[Another result you can prove, attached to the thing that earned it.]"
JOB_ASK = f"I attached my resume. Open to a quick chat? Here's my booking link: {BOOKING_PLACEHOLDER}"
JOB_RESUME_LABEL = "[your resume file]"
JOB_SIGNATURE = "[Your name], [your current role or school]"

_JOB_PARTS = (
    ("subject", "Subject", JOB_SUBJECT,
     "Short and specific. Name the role and the company so the reader knows at once why you wrote."),
    ("hook", "Hook", JOB_HOOK,
     "One verified fact about them, tied to why this email is for them. Never flattery."),
    ("intro", "Intro", JOB_FOUNDER_LINE,
     "Who you are in one plain sentence, so the reader knows who is writing before the ask starts."),
    ("one_liner", "One-liner", JOB_ONE_LINER,
     "The role you want and what you would do for them, in one sentence."),
    ("story", "Story", JOB_STORY,
     "Lead with your strongest result, then say why it fits. Concrete names beat adjectives."),
    ("results", "Results", JOB_RESULTS,
     "Only results you can prove, each attached to the thing that earned it."),
    ("ask", "Ask", JOB_ASK,
     "One small ask with one link. Mention the attached resume so nobody hunts for it."),
    ("signature", "Signature", JOB_SIGNATURE,
     "Name and current role or school. Nothing else, so the email still reads like a person wrote it."),
)

# Default editable note for the job search kind: placeholders only, no Publik claims. The {tokens} are
# filled by the Settings preview; the [slots] are for the owner to replace with their own details.
JOB_BLURB_TEMPLATE = (
    "Hi {first_name},\n\n"
    "{hook}\n\n"
    "I'm [your name], [one plain sentence about who you are and what you do]. "
    "I'm looking for a [role] where I can [what you want to do for them].\n\n"
    "[Your strongest result, in one sentence: what you did, for whom, and what changed.] "
    "[One more sentence on why that experience fits this team.]\n\n"
    "[Another result you can prove, attached to the thing that earned it.]\n\n"
    "I attached my resume. Open to a quick chat? Here's my booking link: [your booking link]\n\n"
    "{operator_name}, {operator_role}"
)

JOB_GUIDANCE = (
    "Open with one verified fact about the company or the person, then say why it matters to you.",
    "Keep the whole email under about 150 words and use short paragraphs.",
    "Say the role you want in one sentence.",
    "Lead with your strongest result and use only results you can prove.",
    "End with one small ask and one link. Mention the attached resume.",
    "Never invent facts, quotes or mutual connections. Leave a placeholder if unsure.",
)


def normalize_kind(kind: object) -> str:
    """Return a known kind; anything else (None, '', unknown) means the default fundraising example."""
    return str(kind) if str(kind or "") in KINDS else DEFAULT_KIND


def example_parts(kind: str = DEFAULT_KIND) -> list[dict]:
    parts = _JOB_PARTS if normalize_kind(kind) == "job_search" else _PARTS
    return [{"key": k, "label": l, "text": t, "why": w} for k, l, t, w in parts]


def example_paragraphs(kind: str = DEFAULT_KIND) -> list[str]:
    """The assembled email body as paragraphs, filled with the example values."""
    if normalize_kind(kind) == "job_search":
        return [
            "Hi [first name],",
            JOB_HOOK,
            f"{JOB_FOUNDER_LINE} {JOB_ONE_LINER}",
            JOB_STORY,
            JOB_RESULTS,
            JOB_ASK,
            JOB_SIGNATURE,
        ]
    return [
        "Hi Kimmy,",
        EXAMPLE_HOOK,
        f"{EXAMPLE_FOUNDER_LINE} {EXAMPLE_PROBLEM}",
        f"{EXAMPLE_SOLUTION} {EXAMPLE_ONE_LINER}",
        EXAMPLE_TRACTION,
        EXAMPLE_ASK,
        EXAMPLE_SIGNATURE,
    ]


def example_pitch(kind: str = DEFAULT_KIND) -> dict:
    """Everything the `_example_pitch.html` partial and the MCP tool need. Plain JSON-safe data.

    ``kind`` is 'fundraising' (default, the Publik example) or 'job_search' (placeholders only).
    An unknown kind falls back to fundraising. The result never contains a credibility line such as
    "50+ meetings"; that copy lives in ``workspace_settings.CAMPAIGN_KINDS`` and is for the app panel only.
    """
    kind = normalize_kind(kind)
    job = kind == "job_search"
    paragraphs = example_paragraphs(kind)
    subject = JOB_SUBJECT if job else EXAMPLE_SUBJECT
    return {
        "kind": kind,
        "label": JOB_LABEL if job else EXAMPLE_LABEL,
        "subject": subject,
        "attachment": JOB_RESUME_LABEL if job else EXAMPLE_DECK_LABEL,
        "parts": example_parts(kind),
        "paragraphs": paragraphs,
        "copy_text": f"Subject: {subject}\n\n" + "\n\n".join(paragraphs),
        "guidance": list(JOB_GUIDANCE if job else WRITING_GUIDANCE),
    }


# ---------------------------------------------------------------------------
# Detection: does a draft still carry the shipped example?
# ---------------------------------------------------------------------------

_MIN_FINGERPRINT = 40
_NORMALIZE = re.compile(r"[^a-z0-9]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _norm(text: object) -> str:
    return _NORMALIZE.sub(" ", str(text or "").lower()).strip()


def _sentences(text: str) -> list[str]:
    return [s for s in (_norm(part) for part in _SENTENCE.split(str(text or ""))) if len(s) >= _MIN_FINGERPRINT]


def _sample_sender_lines() -> list[str]:
    """Lines of the shipped sample sender profile, so loaded sample drafts are flagged too."""
    try:
        from importlib.resources import files
        data = json.loads(files("vcoutreach").joinpath("web", "sample", "sender.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    lines: list[str] = []
    for key in ("product_lines", "traction_lines"):
        for item in data.get(key) or []:
            lines.extend(_sentences(str(item)))
    return lines


def _fingerprints() -> list[str]:
    texts = (EXAMPLE_HOOK, EXAMPLE_FOUNDER_LINE, EXAMPLE_PROBLEM, EXAMPLE_SOLUTION, EXAMPLE_ONE_LINER,
             EXAMPLE_TRACTION, EXAMPLE_ASK, JOB_HOOK, JOB_FOUNDER_LINE, JOB_STORY, JOB_RESULTS)
    found: list[str] = []
    for text in texts:
        found.extend(_sentences(text))
    found.extend(_sample_sender_lines())
    return found


def draft_carries_example(body: object, subject: object = "") -> bool:
    """True while the draft still holds a distinctive sentence of the shipped example pitch.

    Matching ignores case, punctuation and spacing, so small edits to the layout do not hide it.
    A draft that keeps the unfilled booking placeholder is flagged as well.
    """
    haystack = _norm(body)
    if not haystack and not str(subject or "").strip():
        return False
    if BOOKING_PLACEHOLDER in str(body or "").lower():
        return True
    if _norm(subject) == _norm(EXAMPLE_SUBJECT) and haystack:
        return True
    return any(fp in haystack for fp in _fingerprints())


EXAMPLE_WARNING = ("Replace the Publik example pitch with your own company details before sending to real "
                   "people. Test sends still work.")
