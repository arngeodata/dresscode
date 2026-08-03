from pydantic import BaseModel, field_validator
from typing import Optional
import re


def _none_to_list(v):
    """Coerce a null value into an empty list.

    Claude sometimes returns ``null`` for a list field when a CV has no data
    for it (e.g. no Education or Languages section). Pydantic's list defaults
    only apply when the key is ABSENT — an explicit ``null`` is rejected and
    fails validation, which kills the whole job. This keeps the pipeline robust
    by treating ``null`` the same as an empty list.
    """
    return [] if v is None else v


# ── Postmark inbound webhook payload ──────────────────────────────────────────

class PostmarkAttachment(BaseModel):
    Name: str
    Content: str          # base64-encoded file content
    ContentType: str
    ContentLength: int


class PostmarkInboundPayload(BaseModel):
    From: str
    FromName: Optional[str] = None
    OriginalRecipient: str
    Subject: Optional[str] = ""
    TextBody: Optional[str] = ""
    Attachments: list[PostmarkAttachment] = []
    Headers: list[dict] = []

    def original_message_id(self) -> Optional[str]:
        """The original email's RFC Message-ID (from headers), used for reply threading."""
        for h in self.Headers:
            if (h.get("Name") or "").lower() == "message-id":
                return h.get("Value")
        return None

    def agency_username(self) -> str:
        """Extract the username portion of the recipient address, e.g. 'acme' from 'acme@dresscode.com'."""
        return self.OriginalRecipient.split("@")[0].lower().strip()

    def sender_domain(self) -> str:
        """Extract sender domain, e.g. 'acme.co.uk' from 'jane@acme.co.uk'."""
        match = re.search(r"@([\w.\-]+)", self.From)
        return match.group(1).lower() if match else ""

    def first_cv_attachment(self) -> Optional[PostmarkAttachment]:
        """Return the first PDF, Word, ODT or RTF attachment, or None."""
        for att in self.Attachments:
            ct = att.ContentType.lower()
            name = att.Name.lower()
            if (
                "pdf" in ct
                or "word" in ct
                or "openxmlformats" in ct
                or "opendocument" in ct
                or "rtf" in ct
                or name.endswith(".pdf")
                or name.endswith(".docx")
                or name.endswith(".doc")
                or name.endswith(".odt")
                or name.endswith(".rtf")
            ):
                return att
        return None


# ── Claude structured CV response ─────────────────────────────────────────────

class CandidateContact(BaseModel):
    full_name: Optional[str] = None
    credentials: Optional[str] = None   # post-nominals e.g. "FRICS", "MSc MRICS"
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin: Optional[str] = None


class ExperienceEntry(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    responsibilities: list[str] = []

    _coerce_responsibilities = field_validator("responsibilities", mode="before")(_none_to_list)


class EducationEntry(BaseModel):
    institution: Optional[str] = None
    qualification: Optional[str] = None
    year: Optional[str] = None


class ExtraSection(BaseModel):
    title: str
    items: list[str] = []  # paragraphs and bullet points in order

    _coerce_items = field_validator("items", mode="before")(_none_to_list)


class ParsedCV(BaseModel):
    candidate: CandidateContact = CandidateContact()
    summary: Optional[str] = None
    experience: list[ExperienceEntry] = []
    education: list[EducationEntry] = []
    skills: list[str] = []
    languages: list[str] = []
    extra_sections: list[ExtraSection] = []

    # Claude may return null for any of these when a section is absent; treat
    # null as an empty list so a missing section never fails the whole job.
    _coerce_lists = field_validator(
        "experience", "education", "skills", "languages", "extra_sections",
        mode="before",
    )(_none_to_list)


# ── Internal job record ───────────────────────────────────────────────────────

class JobRecord(BaseModel):
    id: str
    org_id: str
    sender_email: str
    original_filename: Optional[str]
    input_path: str
    output_path: Optional[str]
    status: str
    error_message: Optional[str]


# ── Organisation record ───────────────────────────────────────────────────────

class Organisation(BaseModel):
    id: str
    name: str
    email_username: Optional[str] = None  # kept for reference; not used for lookup
    allowed_domains: list[str]
    tier: str
    cv_limit: Optional[int]
    cv_count: int
    active: bool
    stripe_customer_id: Optional[str] = None  # keys Stripe meter events; None for free/test orgs
    stripe_sub_id: Optional[str] = None
