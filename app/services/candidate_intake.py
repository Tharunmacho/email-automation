"""Creating a candidate that arrived over the API rather than from a mailbox.

This is the WhatsApp bot's only way in. It exists so the route stays a route —
authenticate, parse, delegate — and so the order of operations below is written
down once instead of being re-derived by every future caller.

What it guarantees, in the order it establishes them:

1. **One phone number is one candidate.** The normalised WhatsApp number is the
   identity. The idempotency key names a conversation and is checked first
   because it is the more specific claim, but it is not what makes somebody
   themselves — the agency's five or six company numbers are five or six
   sending identities, not five or six people.
2. **Everything a submission carries lands on that one record.** Including the
   twentieth submission of the same conversation. This is the guarantee that
   was missing: the key matched, the function returned, and every answer after
   the first was dropped on the floor.
3. **The CRM decides whether a CV is required.** The bot's claim is recorded
   and compared; it is never the thing that is checked.
4. **A returning candidate cannot cost a recruiter their work.** Re-registering
   refreshes what the candidate said about themselves and touches nothing the
   agency decided about them — including who owns them.
5. **Allocation is the existing allocation, and happens once.** `assign_candidate`
   is called exactly as the email pipeline calls it, on creation only; there is
   no second balancer and no second notification.

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

    # ---- 1. Who is this? --------------------------------------------------- #
    #
    # Before anything else, and before any policy work: a retry must be cheap
    # and must not re-run decisions that were already made and recorded.
    phone_key = normalize_phone(profile.phone)
    was_deleted = getattr(repo, "was_deleted", None)
    if callable(was_deleted) and was_deleted(
        idempotency_key=idempotency_key,
        phone_key=phone_key,
    ):
        raise IntakeError(
            "this candidate was deleted in the CRM and cannot be recreated by a bot retry",
            410,
            "CANDIDATE_DELETED",
        )
    existing, matched_on = _resolve_identity(repo, profile, phone_key, idempotency_key)
    if existing:
        return _refresh_existing(
            repo,
            existing=existing,
            profile=profile,
            resume=resume,
            registration=registration,
            job=job,
            identity=identity,
            complete=complete,
            idempotency_key=idempotency_key,
            matched_on=matched_on,
            cv_required_claim=cv_required_claim,
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

    # ---- 4. Build and validate ------------------------------------------- #
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

    # ---- 5. Insert ---------------------------------------------------------- #
    #
    # `insert` resolves a unique-index collision by returning the candidate the
    # winning writer created, so two concurrent retries of the same key end up
    # agreeing on one id instead of one of them erroring.
    candidate_id = repo.insert(record)
    created = candidate_id == record.id

    # ---- 6. Allocate, through the existing balancer ------------------------ #
    #
    # The same call the email pipeline makes. Failing to place someone must not
    # undo an intake that has already succeeded — `assign_candidate` returns a
    # `no_staff` result rather than raising, and anything unexpected is logged
    # and swallowed for the same reason.
    if created and complete:
        try:
            result = assign_candidate(candidate_id, profile, repo=repo)
        except Exception as exc:  # noqa: BLE001 — allocation must not fail intake
            log.error("Allocation failed for candidate %s: %s", candidate_id, exc)
        else:
            if getattr(result, "assigned", False):
                _announce_assignment(candidate_id, profile, result)

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
        result = assign_candidate(candidate_id, profile, repo=repo)
    except Exception as exc:  # noqa: BLE001 — allocation must not fail intake
        log.error("Allocation failed for candidate %s: %s", candidate_id, exc)
    else:
        if getattr(result, "assigned", False):
            _announce_assignment(candidate_id, profile, result)


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


def _announce_assignment(candidate_id: str, profile: CandidateProfile, result) -> None:
    """Tell the staff member a candidate is theirs: bell, toast, and phone.

    Imported here rather than at module scope because `app.notifications` pulls
    in the WebSocket module, which imports the routes, which import this — a
    cycle at import time that the email pipeline avoids the same way.

    Never raises. The candidate is created and allocated by the time this runs,
    and losing that to a failed pop-up would be exactly the wrong trade.
    """
    try:
        from app.notifications import notify_candidate_assigned

        notify_candidate_assigned(
            getattr(result, "staff_id", "") or "",
            {"id": candidate_id, "full_name": profile.full_name, "email": profile.email},
            staff_name=getattr(result, "staff_name", None),
        )
    except Exception as exc:  # noqa: BLE001 — notification must not fail intake
        log.warning("Could not announce the allocation of candidate %s: %s", candidate_id, exc)


def _resolve_identity(
    repo: CandidateRepository,
    profile: CandidateProfile,
    phone_key: Optional[str],
    idempotency_key: str,
) -> tuple[Optional[CandidateRecord], str]:
    """The candidate this submission belongs to, and which signal found them.

    Two signals, and the order between them is the whole design:

    **The idempotency key** identifies this exact submission. It is the more
    specific claim — it says "this is the conversation that produced record X" —
    so it is consulted first and it wins outright.

    **The normalised phone** identifies the *person*. It is what makes the five
    or six company numbers one identity rather than five: the key carries the
    line the deployment sends from, the phone carries who is holding the handset,
    and somebody who wrote to number A last month and number B this morning is
    one candidate on both. It is also what recognises somebody who registered
    long enough ago that their conversation, and its key, are gone.

    Name is not a signal and deliberately never will be. Two Ravi Kumars in a
    labour-supply database is a Tuesday, and merging them costs one of them their
    documents.

    Returns `(None, "none")` when neither matches, which is the create path.
    """
    by_key = repo.find_by_idempotency_key(idempotency_key)
    if by_key:
        return by_key, "idempotency_key"

    by_phone = _find_existing_person(repo, profile, phone_key)
    if by_phone:
        return by_phone, "phone"

    return None, "none"


def _refresh_existing(
    repo: CandidateRepository,
    *,
    existing: CandidateRecord,
    profile: CandidateProfile,
    resume: Optional[StoredResume],
    registration: Optional[RegistrationState],
    job: Optional[JobSection],
    identity: Optional[Dict[str, List[Dict[str, Any]]]],
    complete: bool,
    idempotency_key: str,
    matched_on: str,
    cv_required_claim: Optional[bool],
) -> IntakeResult:
    """Land this submission on the candidate who already exists.

    One path for both signals, because the difference between "the same
    conversation sent more" and "the same person came back" is a difference in
    how we found them, not in what we owe them. Either way what they have just
    told us about themselves is written down, and everything the agency decided
    about them is left exactly as it stands.

    Three things this does **not** do, and each of them is a way the feature
    would break in production rather than in a test:

    * **It does not allocate.** The candidate has an owner, and running the
      balancer again would move live work off somebody's desk.
    * **It does not notify.** No ownership changed, so there is no event. A
      message per answered question would be forty messages for one registration.
    * **It does not re-derive the CV decision.** §13: the rule that applied is
      the rule that applied on the day, and the record carries it.
    """
    was_complete = existing.registration.complete if existing.registration is not None else True
    refusing = complete and existing.cv_required and resume is None and existing.resume is None

    # What they told us; nothing the agency concluded. The allow-list in
    # `refresh_whatsapp_profile` is what enforces that, and it is the single
    # most important property on this path.
    repo.refresh_whatsapp_profile(existing.id, profile)
    if registration is not None or job is not None:
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

    # A CV from somebody we already know, who had none, is new information and
    # is kept. One who already has a résumé keeps the one on file: a recruiter
    # may have read it and formed a view, and quietly swapping the document
    # underneath that view is not a refresh, it is a substitution.
    if resume is not None and existing.resume is None:
        repo.attach_resume(existing.id, resume)
    elif resume is not None:
        log.info(
            "Candidate %s re-registered with a resume and already has one; keeping the original",
            existing.id,
        )

    # Record the key on a record the phone found, so the next submission from
    # this conversation takes the direct route and so the link between the two
    # is written down rather than re-derived. Only ever fills a blank — a record
    # that already carries a key keeps it, because that key names the
    # conversation that created it and this one did not.
    if matched_on == "phone":
        repo.adopt_idempotency_key(existing.id, idempotency_key)

    if refusing:
        raise IntakeError(
            "the CV policy requires a resume for this destination and job category",
            422,
            "CV_REQUIRED",
        )

    if complete and not was_complete:
        _allocate(existing.id, profile, repo)

    log.info(
        "WhatsApp submission %s matched candidate %s on %s; profile refreshed",
        idempotency_key,
        existing.id,
        matched_on,
    )

    return IntakeResult(
        candidate_id=existing.id,
        created=False,
        # The record's own decision, not today's. A policy that changed since
        # they registered does not retroactively require a CV of them.
        cv_required=existing.cv_required,
        cv_policy_version=existing.cv_policy_version or "",
        policy_overrode_claim=(
            cv_required_claim is not None and cv_required_claim != existing.cv_required
        ),
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
