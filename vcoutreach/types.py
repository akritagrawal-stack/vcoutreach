from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Protocol, Union

class Lane(str, Enum):
    FIRST_TOUCH = "first_touch"
    FOLLOW_UP = "follow_up"

@dataclass(frozen=True)
class Contact:
    id: str
    name: str
    email: str
    org: str = ""
    role: str = ""
    facts: tuple[str, ...] = ()          # verified statements about the recipient
    source_urls: tuple[str, ...] = ()
    lane: Lane = Lane.FIRST_TOUCH
    thread_subject: str = ""             # follow_up only: original subject
    in_reply_to: str = ""                # follow_up only: RFC822 Message-ID of the original
    references: str = ""                 # follow_up only
    preferred_name: str = ""             # optional greeting name (CSV column first_name)
    @property
    def first_name(self) -> str:
        preferred = self.preferred_name.strip()
        if preferred:
            return preferred
        name = self.name.strip()
        if "," in name:
            before, after = name.split(",", 1)
            after = after.strip()
            if after:
                suffixes = ("phd", "md", "mba", "jr", "sr", "ii", "iii", "iv", "cpa", "cfa", "esq", "jd", "dds")
                lead = after.split()[0].replace(".", "").strip(",;:").casefold()
                name = before.strip() if lead in suffixes else after
        titles = ("dr", "mr", "mrs", "ms", "mx", "prof", "professor", "sir", "madam")
        tokens = name.split()
        while tokens and tokens[0].lower().rstrip(".") in titles:
            tokens.pop(0)
        if not tokens:
            return ""
        token = tokens[0].rstrip(".,;:")
        if token and (token.isupper() or token.islower()):
            token = token.capitalize()
        return token

@dataclass(frozen=True)
class SenderProfile:
    name: str
    title: str = ""
    org: str = ""
    website: str = ""
    signature: str = ""                  # full signature block, multi line
    product_lines: tuple[str, ...] = ()  # approved one-sentence descriptions of what the sender does
    traction_lines: tuple[str, ...] = () # approved traction sentences
    ask_line: str = ""                   # approved closing ask, may contain a calendar link
    approved_claims: Mapping[str, str] = field(default_factory=dict)
    forbidden_phrases: tuple[str, ...] = ()

@dataclass(frozen=True)
class PersonalizationRequest:
    contact: Contact
    sender: SenderProfile
    instructions: str = ""

MetaValue = Union[str, int]

@dataclass(frozen=True)
class Hook:
    text: str
    provider: str
    model: str = ""
    meta: Mapping[str, MetaValue] = field(default_factory=dict)

@dataclass(frozen=True)
class LintIssue:
    code: str
    message: str
    severity: str = "error"              # "error" or "warning"

@dataclass(frozen=True)
class Draft:
    contact_id: str
    to: str
    subject: str
    body: str
    status: str                          # "ok" or "blocked"
    hook: Hook | None = None
    issues: tuple[LintIssue, ...] = ()
    lane: Lane = Lane.FIRST_TOUCH
    in_reply_to: str = ""
    references: str = ""

class OutreachError(Exception): ...
class ContactsError(OutreachError): ...
class SenderProfileError(OutreachError): ...
class ProviderError(OutreachError): ...
class ProviderConfigError(ProviderError): ...      # missing or malformed settings
class ProviderAuthError(ProviderError): ...        # 401, 403
class ProviderCreditError(ProviderError):          # 402
    def __init__(self, message: str, top_up_url: str = ""):
        super().__init__(message); self.top_up_url = top_up_url
class ProviderRateLimitError(ProviderError): ...   # 429 after retries exhausted
class ProviderTransportError(ProviderError): ...   # timeouts, connection errors, 5xx after retries
class ProviderResponseError(ProviderError): ...    # unparseable or empty response body
class InsufficientFacts(ProviderError): ...        # model answered NO_HOOK or contact has no facts

class PersonalizationProvider(Protocol):
    name: str
    def personalize(self, request: PersonalizationRequest) -> Hook: ...
