"""Creating a candidate that arrived over the API rather than from a mailbox.

This is the WhatsApp bot's only way in. It exists so the route stays a route —
authenticate, parse, delegate — and so the order of operations below is written
down once instead of being re-derived by every future caller.

What it guarantees, in the order it establishes them:

1. **The same submission twice is the same candidate.** Enforced by a unique
   index, not by a lookup, because a lookup loses the race it exists to win.
2. **The CRM decides whether a CV is required.** The bot's claim is recorded
   and compared; it is never the thing that is checked.
3. **A returning candidate cannot cost a recruiter their work.** Re-registering
   refreshes what the candidate said about themselves and touches nothing the
   agency decided about them.
4. **Allocation is the existing allocation.** `assign_candidate` is called
   exactly as the email pipeline calls it; there is no second balancer.

The email pipeline is not routed through here and does not change.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from typing import Any, Dict, List

from app.assignment.balancer import assign_candidate
from app.core.models import (
    CandidateProfile,
    CandidateRecord,
    JobSection,
    RegistrationState,
    StoredResume,
    utcnow,
)
from app.db.dedup import normalize_phone
from app.db.repository import CandidateRepository
from app.logging_config import get_logger
from app.policy.cv_policy import get_policy

log = get_logger(__name__)


class IntakeError(Exception):
    """A submission that cannot become a candidate. Carries an HTTP status."""

    def __init__(self, message: str, status_code: int = 422, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass
class IntakeResult:
    candidate_id: str
    created: bool
    cv_required: bool
    cv_policy_version: str
    #: True when the CRM's answer differed from what the caller claimed. The bot
    #: reads this to know it must reopen the CV step rather than declare the
    #: registration finished.
    policy_overrode_claim: bool = False


def intake_whatsapp_candidate(
    *,
    profile: CandidateProfile,
    idempotency_key: str,
    cv_required_claim: Optional[bool] = None,
    resume: Optional[StoredResume] = None,
    registration: Optional[RegistrationState] = None,
    job: Optional[JobSection] = None,
    identity: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    repo: Optional[CandidateRepository] = None,
) -> IntakeResult:
    """Create or refresh one WhatsApp candidate.

    `profile` is already mapped to the CRM's own field names by the caller —
    this does not know anything about the bot's schema, which is what keeps the
    two systems separable.

    `resume` is a file the caller sent *with* the submission, already written to
    the CRM's storage. It has to be possible to send one here, rather than only
    through `POST /candidates/{id}/resume`, because of a circularity that would
    otherwise have no exit: a candidate the policy requires a CV for cannot be
    created without one, and the upload endpoint needs the id of a candidate
    that does not exist yet. So a résumé that is *required* arrives with the
    submission; a résumé that is merely *offered* can arrive either way.
    """
    repo = repo or CandidateRepository()

    if not idempotency_key or not idempotency_key.strip():
        raise IntakeError("idempotency_key is required", 400, "missing_idempotency_key")
    idempotency_key = idempotency_key.strip()

    #: Whether the caller is telling us about a finished registration or one
    #: still being answered. Absent means finished — every submission that
    #: predates mid-conversation delivery was, by definition, a finished one.
    complete = registration.complete if registration is not None else True

    # ---- 1. Has this exact submission already been accepted? --------------- #
    #
    # Before anything else, and before any policy work.
    #
    # This used to return here and do nothing else, which was right when a
    # submission arrived once. It is wrong now: the bot delivers a registration
    # while it is still being answered, under the same key each time, precisely
    # so the record fills in as the candidate answers. Returning early would
    # have made every delivery after the first a no-op — the CRM would hold
    # whatever the candidate had said in their first ten seconds and nothing
    # after it, and it would look like it was working.
    #
    # So a replay refreshes. What it refreshes is the same allow-list a
    # returning candidate's does — profile facts and the conversation's own
    # sections, never anything the agency decided — so a recruiter's assessment
    # is no more disturbed by the eleventh delivery than by the second
    # registration.
    existing = repo.find_by_idempotency_key(idempotency_key)
    if existing:
        # Read before anything is written. `refresh_whatsapp_sections` replaces
        # the stored registration with the incoming one, so asking afterwards
        # whether this candidate *was* finished returns whether they are
        # finished now — which is always yes on the delivery that finishes them,
        # and would mean nobody was ever allocated.
        was_complete = (
            existing.registration.complete if existing.registration is not None else True
        )

        # The CV policy, applied to the delivery that says the registration is
        # over.
        #
        # This is the moment the rule bites for every candidate the bot now
        # syncs as it goes, and it is the whole reason it is checked here as
        # well as on creation: a candidate created halfway through their
        # conversation was created without the CV check, and if completion did
        # not re-run it the policy would have been bypassed for everybody — the
        # first delivery too early to apply it and every later one waved through
        # as a replay.
        policy = get_policy()
        cv_required = policy.is_cv_required(
            profile.destination_country, profile.job_category
        )
        holds_cv = resume is not None or existing.resume is not None
        refusing = complete and cv_required and not holds_cv

        # Everything the candidate said is kept either way. It is good data, it
        # was already accepted at partial level, and throwing it away because
        # the last delivery was missing a file would lose answers nobody
        # disputes. What the refusal changes is the *claim* that the
        # registration is finished: it is not, as far as this system is
        # concerned, until the CV the policy asks for is here.
        repo.refresh_whatsapp_profile(existing.id, profile)
        repo.refresh_whatsapp_sections(
            existing.id,
            registration=(
                registration.model_copy(update={"complete": False})
                if refusing and registration is not None
                else registration
            ),
            job=job,
        )
        _store_identity_documents(existing.id, identity)

        # A CV from somebody who had none is new information and is kept. One
        # who already has a résumé keeps the one on file: a recruiter may have
        # read it and formed a view, and quietly swapping the document
        # underneath that view is not a refresh, it is a substitution.
        if resume is not None and existing.resume is None:
            repo.attach_resume(existing.id, resume)

        if refusing:
            raise IntakeError(
                "the CV policy requires a resume for this destination and job category",
                422,
                "CV_REQUIRED",
            )

        # Allocated on the delivery that *finishes* the registration, and on no
        # other.
        #
        # The transition is what is tested, not the end state. A record created
        # complete was allocated when it was created; a record created while the
        # candidate was still answering was deliberately not, because allocation
        # starts an SLA clock and puts a name in a recruiter's queue — so the
        # delivery that completes it is the one that places it. Every delivery
        # after that finds a registration that was already complete and does
        # nothing, which is what stops a re-registration moving somebody to a
        # different desk.
        if complete and not was_complete:
            _allocate(existing.id, profile, repo)

        log.info(
            "Replay of %s refreshed candidate %s (complete=%s)",
            idempotency_key,
            existing.id,
            complete,
        )
        return IntakeResult(
            candidate_id=existing.id,
            created=False,
            cv_required=existing.cv_required,
            cv_policy_version=existing.cv_policy_version or "",
        )

    # ---- 2. What does the CRM say about a CV? ------------------------------ #
    #
    # Derived here, from the profile, using the CRM's own table. The caller's
    # `cv_required_claim` is not consulted for the decision — only compared
    # against it afterwards, so a disagreement can be reported back rather than
    # silently obeyed.
    policy = get_policy()
    cv_required = policy.is_cv_required(
        profile.destination_country, profile.job_category
    )
    overrode = cv_required_claim is not None and cv_required_claim != cv_required

    if overrode:
        log.warning(
            "CV policy disagreed with the caller for (%s, %s): claim=%s policy=%s",
            profile.destination_country,
            profile.job_category,
            cv_required_claim,
            cv_required,
        )

    # ---- 3. Is the same person already on file? ---------------------------- #
    #
    # A different question from step 1. That one asked "have I seen this
    # submission?"; this asks "have I seen this person?" — someone who
    # registered months ago and has come back has a new idempotency key and the
    # same phone number.
    phone_key = normalize_phone(profile.phone)
    duplicate = _find_existing_person(repo, profile, phone_key)

    if duplicate:
        # Refresh what they told us; leave every recruiter-owned field alone.
        # See `refresh_whatsapp_profile` — the allow-list there is what stops a
        # re-registration reopening a closed assessment.
        repo.refresh_whatsapp_profile(duplicate.id, profile)
        repo.refresh_whatsapp_sections(duplicate.id, registration=registration, job=job)
        _store_identity_documents(duplicate.id, identity)

        # A CV sent by someone we already know, who had none, is new information
        # and is kept. One who already has a résumé keeps the one on file: a
        # recruiter may have read it and formed a view, and quietly swapping the
        # document underneath that view is not a refresh, it is a substitution.
        if resume is not None and duplicate.resume is None:
            repo.attach_resume(duplicate.id, resume)
        elif resume is not None:
            log.info(
                "Candidate %s re-registered with a resume and already has one; keeping the original",
                duplicate.id,
            )

        log.info(
            "WhatsApp submission %s matched existing candidate %s; profile refreshed",
            idempotency_key,
            duplicate.id,
        )
        return IntakeResult(
            candidate_id=duplicate.id,
            created=False,
            cv_required=cv_required,
            cv_policy_version=policy.version,
            policy_overrode_claim=overrode,
        )

    # ---- 4. Is the résumé the policy asked for actually here? -------------- #
    #
    # Raised as a typed failure rather than left to the model validator, because
    # this is the one rejection the bot can do something about and it has to be
    # able to tell it apart from every other 422. `CV_REQUIRED` travels to the
    # caller as a machine-readable code; the bot reopens its CV step, collects
    # the file, and resends this same submission under this same key.
    #
    # The model validator still runs a few lines below and still enforces the
    # same rule. It is the backstop for every other way a record can be built —
    # this is the one that produces a usable answer.
    #
    # Not applied to a registration still in progress. The candidate has not yet
    # reached the question that would have produced a CV, so refusing them is
    # refusing somebody for not having answered a question nobody has asked —
    # and the effect would be that a half-finished registration only ever
    # appears here once it is finished, which is exactly the candidate a
    # recruiter never needed help remembering. The rule bites in full on the
    # delivery that says the registration is complete.
    if complete and cv_required and resume is None:
        raise IntakeError(
            "the CV policy requires a resume for this destination and job category",
            422,
            "CV_REQUIRED",
        )

    # ---- 5. Build and validate ------------------------------------------- #
    #
    # The record is constructed with the *derived* `cv_required`, so the model
    # validator checks the résumé against the CRM's answer. A caller that said
    # "no CV needed" while the policy says otherwise fails here, which is
    # exactly the bypass this design exists to close.
    #
    # `resume_hash` is the résumé's digest or nothing at all — never "", which
    # a sparse unique index does not skip. See the field's own note in
    # `app/core/models.py`; that empty string is the bug this integration was
    # most likely to ship.
    now = utcnow()
    record = CandidateRecord(
        id=uuid.uuid4().hex,
        source="whatsapp",
        profile=profile,
        resume=resume,
        source_email=None,
        phone_key=phone_key,
        email_key=None,
        resume_hash=resume.sha256 if resume else None,
        cv_required=cv_required,
        cv_policy_version=policy.version,
        idempotency_key=idempotency_key,
        status="ingested",
        ingested_at=now,
        processed_at=now,
        registration=registration,
        job=job,
    )

    # ---- 6. Insert ---------------------------------------------------------- #
    #
    # `insert` resolves a unique-index collision by returning the candidate the
    # winning writer created, so two concurrent retries of the same key end up
    # agreeing on one id instead of one of them erroring.
    candidate_id = repo.insert(record)
    created = candidate_id == record.id

    # ---- 7. Allocate, through the existing balancer ------------------------ #
    #
    # The same call the email pipeline makes. Failing to place someone must not
    # undo an intake that has already succeeded — `assign_candidate` returns a
    # `no_staff` result rather than raising, and anything unexpected is logged
    # and swallowed for the same reason.
    #
    # Only once the registration is finished. Allocation starts an SLA clock and
    # puts a name in a recruiter's queue, and doing that to somebody three
    # questions into a conversation would have the clock running against a
    # profile there is nothing yet to assess. The delivery that completes the
    # registration allocates them — see the replay branch above, which is the
    # path a candidate created mid-conversation actually takes.
    if created and complete:
        _allocate(candidate_id, profile, repo)

    _store_identity_documents(candidate_id, identity)

    return IntakeResult(
        candidate_id=candidate_id,
        created=created,
        cv_required=cv_required,
        cv_policy_version=policy.version,
        policy_overrode_claim=overrode,
    )


def _allocate(candidate_id: str, profile: CandidateProfile, repo: CandidateRepository) -> None:
    """Places a candidate through the existing balancer.

    Failing to place someone must not undo an intake that has already
    succeeded — `assign_candidate` returns a `no_staff` result rather than
    raising, and anything unexpected is logged and swallowed for the same
    reason.
    """
    try:
        assign_candidate(candidate_id, profile, repo=repo)
    except Exception as exc:  # noqa: BLE001 — allocation must not fail intake
        log.error("Allocation failed for candidate %s: %s", candidate_id, exc)


def _store_identity_documents(
    candidate_id: str,
    identity: Optional[Dict[str, List[Dict[str, Any]]]],
) -> None:
    """Files an Aadhaar or a passport the bot read, where the email pipeline files them.

    Not on the candidate record, and that is the whole point. These are
    government identity numbers, and the reads that populate a recruiter's list
    project the candidate document wholesale — so a number stored there is a
    number that reaches a browser the first time somebody adds a field to a
    listing. Their own collections keep them out of that path, and
    `GET /candidates/{id}/identity` is the one endpoint that serves them, masked
    for everybody but an administrator.

    The bot sends each extractor's answer verbatim, which is the same shape the
    email pipeline hands these functions — so the projection that has been in
    front of recruiters for months is the projection that runs, rather than a
    second one written here that would drift from it.

    `record_id` is the bot's upload id, used as the natural key exactly as the
    email side uses the message and attachment it came off. A registration is
    delivered many times as it fills in and the same document arrives with each
    of them; keyed this way, it overwrites its own row instead of accumulating a
    copy per delivery.

    Best-effort on purpose. A candidate who reached the CRM without their
    Aadhaar filed is a far better outcome than one who did not reach it at all,
    and the next delivery carries the same documents again.
    """
    if not identity:
        return

    from app.db.identity_records import store_aadhaar_record, store_passport_record

    handlers = {"aadhaar": store_aadhaar_record, "passport": store_passport_record}

    for kind, store in handlers.items():
        for document in identity.get(kind) or []:
            record_id = (document.get("record_id") or "").strip()
            result = document.get("result")
            if not record_id or not isinstance(result, dict):
                continue

            try:
                store(
                    f"whatsapp:{record_id}",
                    result,
                    candidate_id=candidate_id,
                    provider="whatsapp",
                    # No mailbox and no message. The bot's slot is the nearest
                    # equivalent — which of its questions the file answered —
                    # and it is what tells a reader that this Aadhaar is the
                    # back of the card rather than a second card.
                    account_id="",
                    message_id=document.get("slot") or kind,
                    attachment_id=record_id,
                    filename=document.get("filename") or "",
                    sha256=document.get("sha256") or "",
                    ocr_job_id=None,
                )
            except Exception as exc:  # noqa: BLE001 — never fail an intake over this
                log.error(
                    "Could not file the %s read for candidate %s: %s", kind, candidate_id, exc
                )


def _find_existing_person(
    repo: CandidateRepository,
    profile: CandidateProfile,
    phone_key: Optional[str],
) -> Optional[CandidateRecord]:
    """The candidate this person already is, if the match is safe to act on.

    `normalize_phone` compares the last ten digits, which is deliberately left
    alone — the email pipeline's deduplication rests on it and §12 of the
    integration brief is explicit that it must not change. But last-ten matching
    was designed for one country, and WhatsApp is not one country: a Malaysian
    and an Indian number can end on the same ten digits and are not the same
    person.

    So a phone match is confirmed against the full international number where
    both sides have one. Where they do not, the match still stands — that is the
    behaviour email has always had, and weakening it would start splitting
    candidates who are correctly deduplicated today.
    """
    if not phone_key:
        return None

    candidate = repo.find_by_email_or_phone(None, phone_key)
    if not candidate:
        return None

    incoming_e164 = _e164(profile.phone_e164 or profile.phone)
    stored_e164 = _e164(candidate.profile.phone_e164 or candidate.profile.phone)

    if incoming_e164 and stored_e164 and incoming_e164 != stored_e164:
        log.info(
            "Phone key %s matched a candidate with a different international number; "
            "treating as a different person",
            phone_key,
        )
        return None

    return candidate


def _e164(value: Optional[str]) -> Optional[str]:
    """Digits of an international number, or None when it is not one.

    Not a parser and not trying to be: it only has to tell "+60123456789" from
    "+919876543210" reliably enough to stop a cross-country collision. A number
    with no country context returns None and the caller falls back to the
    existing behaviour rather than guessing at one.
    """
    if not value:
        return None
    raw = value.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    # A leading + is the only unambiguous marker of an international number.
    # Without it, a bare 10-digit local number would be compared against a
    # 12-digit international one and never match.
    if raw.startswith("+") or len(digits) > 10:
        return digits
    return None
