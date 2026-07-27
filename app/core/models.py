"""Domain models shared across the pipeline.

Two families live here:
  * *Transport* models (EmailMessage, Attachment) — what we pull from Gmail.
  * *Persistence* models (CandidateProfile, StoredResume, CandidateRecord) —
    what we compute and write to MongoDB.

Keeping them explicit (rather than passing dicts around) makes every stage's
input/output contract obvious and gives future extensions — search, ranking,
scoring — a stable schema to build on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
#  Transport: what we read from Gmail
# --------------------------------------------------------------------------- #
class Attachment(BaseModel):
    filename: str
    mime_type: str
    size: int
    attachment_id: str          # Gmail attachment handle (fetched lazily)
    data: Optional[bytes] = None  # populated once downloaded


class EmailMessage(BaseModel):
    message_id: str
    thread_id: str
    from_addr: str
    from_name: Optional[str] = None
    to_addr: Optional[str] = None
    subject: str = ""
    date: Optional[str] = None
    snippet: str = ""
    body_text: str = ""
    attachments: List[Attachment] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
#  Extraction output
# --------------------------------------------------------------------------- #
class ExtractedDocument(BaseModel):
    text: str
    method: str                 # "pdf_text" | "pdf_ocr" | "docx" | "doc" | "image_ocr" | "plain"
    page_count: Optional[int] = None
    ocr_used: bool = False
    char_count: int = 0


# --------------------------------------------------------------------------- #
#  Structured candidate (the AI's job)
# --------------------------------------------------------------------------- #
class WorkExperience(BaseModel):
    company: Optional[str] = None
    designation: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None


class Education(BaseModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    grade: Optional[str] = None


class Project(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    technologies: List[str] = Field(default_factory=list)
    url: Optional[str] = None


class CandidateProfile(BaseModel):
    """The structured key/value profile the AI extracts from resume text."""

    is_resume: bool = True
    confidence: float = 0.0     # 0..1 — the AI's confidence this is a real resume

    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None

    skills: List[str] = Field(default_factory=list)
    technical_skills: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)

    work_experience: List[WorkExperience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    achievements: List[str] = Field(default_factory=list)

    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    current_company: Optional[str] = None
    current_designation: Optional[str] = None
    total_experience_years: Optional[float] = None

    resume_summary: Optional[str] = None
    # Anything the AI found that doesn't fit the schema above.
    additional_info: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #
class StoredResume(BaseModel):
    """Pointer + fingerprint for the original resume file we kept."""

    original_filename: str
    mime_type: str
    size: int
    sha256: str                 # content hash — exact-duplicate detection
    storage_backend: str        # "local" | "s3" | "gcs"
    storage_key: str            # path/key within the backend
    extraction_method: str = ""
    ocr_used: bool = False


class SourceEmail(BaseModel):
    message_id: str
    thread_id: str
    from_addr: str
    from_name: Optional[str] = None
    subject: str = ""
    received_date: Optional[str] = None


class CandidateRecord(BaseModel):
    """The full MongoDB document for one ingested candidate."""

    id: str                     # stored as Mongo _id
    profile: CandidateProfile
    resume: StoredResume
    source_email: SourceEmail

    # Normalised dedup keys (also indexed in Mongo).
    email_key: Optional[str] = None
    phone_key: Optional[str] = None
    resume_hash: str = ""

    status: str = "ingested"    # ingested | duplicate | needs_review | error
    duplicate_of: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def to_mongo(self) -> Dict[str, Any]:
        doc = self.model_dump(mode="python")
        doc["_id"] = doc.pop("id")
        return doc

    @classmethod
    def from_mongo(cls, doc: Dict[str, Any]) -> "CandidateRecord":
        doc = dict(doc)
        doc["id"] = doc.pop("_id")
        return cls.model_validate(doc)
