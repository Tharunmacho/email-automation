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
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
class PageText(BaseModel):
    """One page of a document, plus what the page classifier made of it."""

    page_number: int            # 1-based
    text: str = ""
    kind: str = "unknown"       # resume | certificate | experience_letter | id_document | other | blank
    score: float = 0.0          # how résumé-like the page is
    ocr_used: bool = False


class ExtractedDocument(BaseModel):
    text: str
    method: str                 # "pdf_text" | "pdf_ocr" | "docx" | "doc" | "image_ocr" | "plain"
    page_count: Optional[int] = None
    ocr_used: bool = False
    char_count: int = 0

    # Where the résumé lives inside a multi-document bundle. `text` always holds
    # everything that was extracted — nothing is thrown away — while these say
    # which slice of it is the candidate's profile, so OCR and the LLM can be
    # pointed at that slice alone.
    pages: List[PageText] = Field(default_factory=list)
    resume_pages: List[int] = Field(default_factory=list)   # 1-based
    is_resume: Optional[bool] = None                        # None = not classified
    classification_confidence: Optional[float] = None
    classification_reason: str = ""

    # The résumé extraction, already paid for.
    #
    # Locating the résumé sends its pages to the Veris résumé endpoint for a
    # better read, and that job's answer carries the structured fields as well
    # as the text. The parser then used to send the very same pages to the very
    # same endpoint again for the fields alone — one upload, one extraction and
    # one wait, all duplicated, differing only in idempotency key. Keeping the
    # payload here lets the parser use the answer instead of buying it twice.
    #
    # None when nothing was uploaded (no API key, a local read, a synchronous
    # call), and the parser then does exactly what it always did.
    veris_resume_result: Optional[dict] = None

    @property
    def resume_text(self) -> str:
        """Only the pages carrying candidate profile data (all of it if unknown)."""
        if not self.pages or not self.resume_pages:
            return self.text
        wanted = set(self.resume_pages)
        parts = [p.text.strip() for p in self.pages if p.page_number in wanted and p.text.strip()]
        return "\n\n".join(parts) or self.text


# --------------------------------------------------------------------------- #
#  Structured candidate (the AI's job)
# --------------------------------------------------------------------------- #
class WorkExperience(BaseModel):
    model_config = ConfigDict(extra="allow")

    company: Optional[str] = None
    designation: Optional[str] = None
    title: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    location: Optional[str] = None
    country: Optional[str] = None
    is_overseas: Optional[bool] = None
    duration_human: Optional[str] = None
    duration_months: Optional[int] = None
    description: Optional[str] = None


class Education(BaseModel):
    model_config = ConfigDict(extra="allow")

    institution: Optional[str] = None
    # Indian and Gulf résumés name the awarding board separately from the school
    # ("SSLC, Govt. Higher Secondary School, Tamil Nadu State Board").
    board_or_university: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    passing_year: Optional[str] = None
    grade: Optional[str] = None


class Project(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    description: Optional[str] = None
    technologies: List[str] = Field(default_factory=list)
    url: Optional[str] = None


class TradeLicense(BaseModel):
    """A licence or trade certificate, with the number that makes it checkable."""

    model_config = ConfigDict(extra="allow")

    name: Optional[str] = None
    number: Optional[str] = None
    issuer: Optional[str] = None
    issue_date: Optional[str] = None
    expiry_date: Optional[str] = None


class JobAnswer(BaseModel):
    """One screening question, as it was asked and as it was answered.

    The question text is stored alongside the id rather than looked up at read
    time, and that is deliberate: an admin rewords a question the week after a
    candidate answered it, and a profile that renders today's wording against
    last week's answer is a record of a conversation that never happened.
    """

    model_config = ConfigDict(extra="allow")

    #: The `job_questions` row this answers, when it came from one. Free-form
    #: questions the bot asked outside the taxonomy have no id and are still
    #: worth keeping.
    question_id: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    #: "text" or "choice" — what the candidate was offered, not what they said.
    kind: Optional[str] = None
    asked_at: Optional[str] = None


class CandidateProfile(BaseModel):
    """The structured key/value profile the AI extracts from resume text."""
    model_config = ConfigDict(extra="allow")

    is_resume: bool = True
    confidence: float = 0.0     # 0..1 — the AI's confidence this is a real resume

    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    # Trade résumés routinely carry a home number and a mobile in the Gulf; the
    # single `phone` above is the primary one, this keeps the rest.
    phone_numbers: List[str] = Field(default_factory=list)
    # The candidate's number in international form, kept alongside `phone`.
    #
    # `normalize_phone` compares the last ten digits, which is right for a
    # single-country mailbox and gets less safe the moment candidates arrive
    # from WhatsApp in any country code. Rather than change that function — the
    # email pipeline's deduplication rests on it — the full E.164 number is
    # stored here so a country-aware comparison has something to work from.
    phone_e164: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    # Where the candidate *lives*. Read off a résumé for email candidates, and
    # asked directly by the WhatsApp bot. Never where they want to work — see
    # `destination_country` below, which exists precisely so these two cannot be
    # confused. A recruiter filtering on residence must not get Malaysia back
    # for someone sitting in Tamil Nadu.
    country: Optional[str] = None

    # Where the candidate wants to work.
    #
    # One actual country, never a region or a pair: "Singapore", not
    # "Singapore/Malaysia" and not "GCC". The CV policy keys on this, and a
    # policy that cannot tell Singapore from Malaysia cannot express a rule
    # about either.
    destination_country: Optional[str] = None
    # What they want to do there, in their own words. Free text, for a person to
    # read.
    job_preference: Optional[str] = None
    # The same thing as a controlled value, for machines to read. This is what
    # the CV policy keys on; `job_preference` is never consulted for a decision
    # because free text cannot be matched reliably.
    job_category: Optional[str] = None

    # ---- the job they actually applied for --------------------------------- #
    # The `job_designations` row the candidate picked, id and title both. The id
    # is what the CV rules and any later report join on; the title is what a
    # recruiter reads, kept here so a job retired or reworded months later still
    # renders as the job this person applied for.
    job_id: Optional[str] = None
    job_title: Optional[str] = None
    # The trade qualification behind the application — "ITI Electrician",
    # "Diploma in Mechanical Engineering". Distinct from `education`, which is
    # the schooling history; this is the one line a client asks for.
    course_or_trade: Optional[str] = None
    # Where inside the destination they want to be — a state, an emirate, a
    # city. Below `destination_country` and never a substitute for it: the CV
    # policy reads the country and would not know what to do with "Kerala".
    state_preference: Optional[str] = None
    # When they can start, in their own words: "Immediately", "after 2 months",
    # "2026-03-01". Free text on purpose — a date field would force the bot to
    # invent one for every candidate who answered with a duration.
    available_from: Optional[str] = None
    # What they said to the screening questions attached to that job.
    job_answers: List[JobAnswer] = Field(default_factory=list)

    skills: List[str] = Field(default_factory=list)
    technical_skills: List[str] = Field(default_factory=list)
    # Machinery and trades ("EOT Crane", "TIG Welding", "CNC Lathe") — the part
    # of a blue-collar profile that a generic skills list flattens away.
    trade_skills: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)

    work_experience: List[WorkExperience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    licenses: List[TradeLicense] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    achievements: List[str] = Field(default_factory=list)

    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    current_company: Optional[str] = None
    current_designation: Optional[str] = None
    total_experience_years: Optional[float] = None
    # Experience as the candidate actually gave it.
    #
    # The WhatsApp bot offers bands ("2-5 years") rather than a number, because
    # that is how people answer the question out loud. Coercing a band into
    # `total_experience_years` above would mean inventing a figure — "3_5"
    # becoming 3.0, or 4.0, neither of which the candidate said — so the band is
    # kept as a band and the numeric field is left empty. A résumé that states
    # a figure still fills `total_experience_years`; the two coexist and neither
    # is derived from the other.
    total_experience_band: Optional[str] = None

    # ---- identity ---------------------------------------------------------- #
    # Passport only, and deliberately only passport.
    #
    # Overseas placement turns on it: a recruiter has to know whether a passport
    # expires inside the deployment window, and that is a question the CRM is
    # asked. Aadhaar and PAN are not stored here at all — no screen or workflow
    # in this system uses them, and copying an identifier into a second database
    # for no reason is exposure bought with nothing.
    #
    # Never logged. See `app.logging_config` and the intake service.
    passport_number: Optional[str] = None
    passport_expiry: Optional[str] = None

    resume_summary: Optional[str] = None
    # Anything the AI found that doesn't fit the schema above.
    additional_info: Optional[Dict[str, Any]] = Field(default_factory=dict)
    # Full unredacted raw OCR extraction JSON payload
    raw_ocr: Optional[Dict[str, Any]] = None


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
    to_addr: Optional[str] = None
    subject: str = ""
    received_date: Optional[str] = None



# The verdicts a reviewer can record. "pending" is the state every allocation
# starts in and the one the SLA clock runs against. Mirrored in
# frontend/src/types/index.ts.
EVALUATION_STATUSES = (
    "pending",
    "shortlisted",
    "interviewing",
    "rejected",
    "on_hold",
    "hired",
)


# Where a candidate came from.
#
# "email" is everything this system did before: a résumé pulled out of a
# mailbox, which is why a CV is not optional there and never becomes optional.
# "whatsapp" is the recruitment bot, where a CV is required for some
# destination/job combinations and not for others — a question the CV policy
# answers, not the caller.
CANDIDATE_SOURCES = ("email", "whatsapp", "manual", "upload")


#: Where a contact number came from. Not a preference order — a WhatsApp number
#: is not "better" than the one on the CV, it is a different fact about the same
#: person, and a documentation officer ringing them needs to know which is which.
CONTACT_SOURCES = ("whatsapp", "resume", "email", "manual")


class CandidateContact(BaseModel):
    """One way to reach a candidate, and where we learned it.

    A person has more than one number: the handset they message from, and the
    one printed on the CV they wrote two years ago. Both are theirs. Storing
    only the first loses the number a documentation officer will actually get
    an answer on; treating the second as a *different person* is how one
    candidate becomes two records with half a registration each.

    So contacts are a list on the record, deduplicated on `key` — the same
    last-ten-digit normalisation the rest of the system compares phones with —
    with every source that produced the number kept alongside it. One number
    both sources gave is one entry naming both, not two entries.
    """

    #: As it was given, because that is what a person reads back to a candidate.
    value: str
    #: International form where one could be derived. Never invented.
    e164: Optional[str] = None
    #: The comparable form. `normalize_phone`, so it lines up with `phone_key`.
    key: str
    #: Every source that produced this number. See `CONTACT_SOURCES`.
    sources: List[str] = Field(default_factory=list)
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None


class IdentityReview(BaseModel):
    """Two records that look like one person, left for a human to settle.

    Raised, never acted on. Merging two candidates means choosing which
    allocation survives, which evaluation survives and which documents are
    thrown away, and every one of those is a decision with somebody's work
    inside it. A passport number appearing on a second record is strong
    evidence and it is still evidence — an OCR misread of one character
    produces exactly this, and so does a genuine data-entry error on a shared
    handset.

    So the conflict is written down, both records keep everything they have,
    and the submission that exposed it lands on the passport holder.
    """

    #: Machine-readable. `duplicate_passport` is the only one so far.
    reason: str
    passport_key: Optional[str] = None
    #: Every record the conflict spans, this one included, oldest first.
    candidate_ids: List[str] = Field(default_factory=list)
    flagged_at: Optional[datetime] = None
    #: Set when somebody has dealt with it. The flag is kept either way — that
    #: two records once collided is a fact about the data worth keeping.
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    note: Optional[str] = None


class CandidateRecord(BaseModel):
    """The full MongoDB document for one ingested candidate."""

    id: str                     # stored as Mongo _id
    # Human-facing CRM identifier. The database id remains private and keeps
    # powering every route; this is what staff quote, search and see on screen.
    candidate_code: Optional[str] = None

    # Defaulted to "email" so every document written before this field existed
    # reads back as what it actually is. There is no backfill to run.
    source: Literal["email", "whatsapp", "manual", "upload"] = "email"

    profile: CandidateProfile
    # Optional on the type, conditional in practice — see `_check_source_rules`
    # below, which is where "email always has a CV" is actually enforced. The
    # annotation had to loosen so a CV-less WhatsApp candidate can exist at all;
    # the rule did not.
    resume: Optional[StoredResume] = None
    source_email: Optional[SourceEmail] = None

    # Normalised dedup keys (also indexed in Mongo).
    email_key: Optional[str] = None
    phone_key: Optional[str] = None

    # ---- identity --------------------------------------------------------- #
    #: The normalised passport number, and the strongest identity this system
    #: has. One person, one passport: a submission carrying a number already on
    #: file belongs to that candidate whatever handset it came from, whichever
    #: of the agency's lines it arrived on, and however far through registration
    #: either record is.
    #:
    #: Absent until a passport turns up, which is usually late — documents are
    #: the last thing a registration collects. A candidate without one is not
    #: broken, they are unresolved, and they are matched the way they always
    #: were until the passport lands. See `app/services/candidate_intake`.
    passport_key: Optional[str] = None
    #: Which of the two things that can produce it did: an OCR'd passport page,
    #: or a number the candidate typed. Kept because they disagree, and when
    #: they do it is the scan that is worth believing.
    passport_key_source: Optional[str] = None

    #: Every number that reaches this person, with its source. See
    #: `CandidateContact` — a phone is a contact detail, not an identity.
    contacts: List[CandidateContact] = Field(default_factory=list)

    #: Set when this record's passport number is also on another record.
    #: Nothing is merged and nothing is deleted; a human decides.
    identity_review: Optional[IdentityReview] = None
    # None, never "".
    #
    # `resume_hash` carries a unique sparse index (`app/db/mongo.py`), and
    # "sparse" excludes documents where the field is *missing* — not documents
    # where it is empty. `to_mongo` drops None and keeps "", so the old default
    # wrote an empty string into a unique index: the first CV-less candidate
    # would insert cleanly and the second would collide with them. That is the
    # worst shape a bug can have, because the feature appears to work.
    #
    # With None the field is absent, the sparse index skips it, and any number
    # of CV-less candidates coexist while real résumé hashes stay unique.
    resume_hash: Optional[str] = None

    status: str = "ingested"    # ingested | duplicate | needs_review | error
    duplicate_of: Optional[str] = None
    auto_reply_sent: bool = False
    raw_ocr: Optional[Dict[str, Any]] = None

    # ---- CV requirement --------------------------------------------------- #
    # What the policy said when this candidate registered, and which version of
    # the policy said it.
    #
    # A snapshot, not a live derivation. Rules change — a country opens up, a
    # client starts demanding CVs for a role that never needed one — and when
    # they do, every candidate already on file must go on reporting the
    # requirement that actually applied to them. Re-deriving would turn a
    # complete record into a non-compliant one overnight and leave nobody able
    # to answer "why has this candidate no CV?".
    #
    # Always True for email: the mailbox pipeline has no other mode.
    cv_required: bool = True
    cv_policy_version: Optional[str] = None

    # ---- idempotency ------------------------------------------------------ #
    # The caller's stable key for this candidate, unique-indexed in Mongo.
    #
    # A lookup followed by an insert is not idempotent: two retries arriving
    # together both find nothing and both insert. The unique index is what makes
    # the second one fail instead, so the intake service can catch the collision
    # and return the candidate the first one created. Absent on email
    # candidates, which are deduplicated by message id and résumé hash.
    idempotency_key: Optional[str] = None

    # ---- lifecycle -------------------------------------------------------- #
    # When the résumé was pulled out of the mailbox (or the Sourcing Hub), and
    # when the parser finished turning it into the profile above. Both are
    # stamped once, at ingestion, and never move again — `created_at` records
    # when the *document* was written, which is the same instant today but is
    # not the same fact, and an SLA clock that has to survive a backfill or an
    # import needs the one about the résumé.
    #
    # Documents written before these existed have neither. Every reader treats
    # a missing value as `created_at` rather than as zero, so an old record
    # reports the arrival time it always had instead of being permanently in
    # breach.
    ingested_at: Optional[datetime] = None
    processed_at: Optional[datetime] = None

    # ---- allocation ------------------------------------------------------- #
    # Who owns this profile. An unassigned candidate is one nobody is
    # accountable for and the SLA sweep cannot report, so ingestion places every
    # record it stores (see app.assignment.balancer).
    assigned_staff_id: Optional[str] = None
    assigned_staff_name: Optional[str] = None
    assigned_at: Optional[datetime] = None

    # ---- evaluation ------------------------------------------------------- #
    # `viewed_at` is stamped once, on the owner's first open; while it is null
    # the SLA clock is running. Reassignment clears both this and the verdict.
    viewed_at: Optional[datetime] = None
    evaluation_status: str = "pending"
    evaluation_score: Optional[int] = None      # 1..5 stars
    evaluation_notes: Optional[str] = None
    evaluated_at: Optional[datetime] = None
    evaluated_by: Optional[str] = None          # staff id that recorded it

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _ensure_candidate_code(self) -> "CandidateRecord":
        # Old documents do not carry this field. Deriving it at the model edge
        # makes detail reads complete immediately; startup also persists the
        # same value so projected list reads and database searches have it.
        if not self.candidate_code:
            from app.core.crm_ids import candidate_code

            self.candidate_code = candidate_code(self.id)
        return self

    @model_validator(mode="after")
    def _check_source_rules(self) -> "CandidateRecord":
        """What each source must carry, enforced on the model itself.

        Here rather than in the route, because a record is built in three places
        — the Gmail pipeline, the intake service, and any future importer — and
        a rule that lives in one caller is a rule the other two can forget. The
        model is the one thing all of them go through.

        Email is unchanged and stays unchanged: a résumé and the message it
        arrived on are both mandatory, exactly as they were before WhatsApp
        existed. Nothing here relaxes that.

        WhatsApp has no email to point at, so `source_email` is not required.
        Whether a résumé is required is decided by `cv_required` — which the CV
        policy computes and the caller does not supply. That is the whole
        mechanism behind "the client cannot bypass the policy": by the time this
        runs, `cv_required` is the CRM's own answer, so a caller claiming no CV
        was needed is checked against a value it never got to influence.
        """
        if self.source == "email":
            if self.resume is None:
                raise ValueError("an email candidate must have a resume")
            if self.source_email is None:
                raise ValueError("an email candidate must have source_email")
            return self

        # Administrator-created records never invent an email source. Legacy
        # manual records remain readable; the current upload path requires the
        # document it was extracted from.
        if self.source in ("manual", "upload"):
            if self.source_email is not None:
                raise ValueError("an administrator-uploaded candidate cannot have source_email")
            if self.source == "upload" and self.resume is None:
                raise ValueError("an uploaded candidate must have a resume")
            return self

        # WhatsApp. No source_email is expected, and inventing one would be a
        # lie on the record about where this person came from.
        if self.cv_required and self.resume is None:
            raise ValueError(
                "the CV policy requires a resume for this destination and job category"
            )
        return self

    def to_mongo(self) -> Dict[str, Any]:
        doc = self.model_dump(mode="python", exclude_none=True)
        doc["_id"] = doc.pop("id")
        return doc

    @classmethod
    def from_mongo(cls, doc: Dict[str, Any]) -> "CandidateRecord":
        doc = dict(doc)
        doc["id"] = doc.pop("_id")
        return cls.model_validate(doc)
