"""One phone number is one candidate, whichever company number they wrote to.

The agency runs five or six WhatsApp numbers. They are five or six *sending*
identities — different threads on the candidate's phone, different lines for the
reply to leave from — and exactly one candidate identity, which is the number
the candidate is holding.

Everything here is a claim about that sentence, and each one exists because of a
specific way a candidate ends up as two people in the database:

* **The conversation is not the person.** A registration abandoned on Tuesday
  and finished on Friday is one record. So is one that started on the Chennai
  number and finished on the Gulf number.
* **A submission that matches must actually be written down.** This is the
  regression that motivated the file: the key matched, the intake returned, and
  every answer after the first was dropped. A candidate who answered eight
  questions reached the CRM as a name and a phone number.
* **Coming back must not cost anybody their work.** A refresh writes what the
  candidate said about themselves. It does not reallocate, it does not
  re-notify, and it cannot reach an evaluation.
* **A name is not an identity.** Two Ravi Kumars in a labour-supply database is
  a Tuesday.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# Imported at module scope, deliberately. `app/api/routes.py` builds a
# `UserRepository()` at import time, which opens a Mongo connection, and
# `tests/conftest.py` refuses connections once its autouse fixture is active.
# Importing during collection happens before that fixture runs, which is how the
# rest of the suite does it too; a lazy import inside a test body trips the guard.
from app.api.routes import AssignRequest, assign_candidate_route
from app.core.models import CandidateProfile, CandidateRecord, StoredResume, utcnow
from app.db.users import STAFF_ROLE, User
from app.db.dedup import normalize_phone
from app.db.repository import CandidateRepository
from app.services.candidate_intake import intake_whatsapp_candidate

# The agency's lines, as Meta reports them in the webhook envelope. Six of them,
# because six is the top of the range the deployment runs and a rule that holds
# for two has been known to stop holding at six.
LINES = ("101", "202", "303", "404", "505", "606")

CANDIDATE_NUMBER = "919876543210"


def key_for(line: str, wa_id: str = CANDIDATE_NUMBER) -> str:
    """The bot's idempotency key, in the shape it actually sends.

    Written out here rather than imported because this suite is the other side
    of that contract: if the bot changes the shape, these tests should be what
    notices.
    """
    return f"whatsapp/{line}/{wa_id}"


# --------------------------------------------------------------------------- #
#  A repository faithful enough to be worth testing against
# --------------------------------------------------------------------------- #
class IdentityRepo:
    """In-memory, and models the four constraints identity actually rests on.

    A dictionary would pass every test below and prove nothing, so this one
    reproduces what MongoDB does rather than what is convenient:

    * the sparse unique index on `idempotency_key` — `insert` refuses a second
      document carrying a key it already holds, and `adopt_idempotency_key`
      refuses to take one off another record;
    * `phone_key` as a *derived* field, rewritten by a refresh, which is how a
      record that arrived without a number acquires one;
    * `find_by_email_or_phone` returning the oldest match, which is the
      determinism the real repository had to be given;
    * `assign` clearing the verdict, so a test that reassigns can see the damage
      that would do.
    """

    def __init__(self):
        self.candidates: dict[str, CandidateRecord] = {}
        self._clock = utcnow()

    def _tick(self) -> datetime:
        """Distinct, increasing timestamps, so "oldest" is a real ordering."""
        self._clock += timedelta(seconds=1)
        return self._clock

    # ---- lookups ---- #
    def find_by_idempotency_key(self, key):
        if not key:
            return None
        return next(
            (c for c in self.candidates.values() if c.idempotency_key == key), None
        )

    def find_by_resume_hash(self, resume_hash):
        if not resume_hash:
            return None
        return next(
            (c for c in self.candidates.values() if c.resume_hash == resume_hash), None
        )

    def find_by_email_or_phone(self, email_key, phone_key):
        matches = [
            c
            for c in self.candidates.values()
            if (phone_key and c.phone_key == phone_key)
            or (email_key and c.email_key == email_key)
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda c: c.created_at)[0]

    def get(self, candidate_id):
        return self.candidates.get(candidate_id)

    # ---- writes ---- #
    def insert(self, record: CandidateRecord) -> str:
        existing = self.find_by_idempotency_key(record.idempotency_key)
        if existing:
            return existing.id
        existing = self.find_by_resume_hash(record.resume_hash)
        if existing:
            return existing.id
        record.created_at = self._tick()
        self.candidates[record.id] = record
        return record.id

    def refresh_whatsapp_profile(self, candidate_id: str, profile: CandidateProfile):
        record = self.candidates[candidate_id]
        incoming = profile.model_dump(mode="python")
        for field in CandidateRepository.WHATSAPP_REFRESHABLE_FIELDS:
            value = incoming.get(field)
            if value in (None, "", [], {}):
                continue
            setattr(record.profile, field, value)
        # Derived, exactly as the real one does it: this is how a record created
        # before the phone was known becomes findable by phone afterwards.
        if profile.phone:
            record.phone_key = normalize_phone(profile.phone)

    def adopt_idempotency_key(self, candidate_id: str, key) -> bool:
        record = self.candidates.get(candidate_id)
        if not record or not key or record.idempotency_key:
            return False
        if self.find_by_idempotency_key(key):
            return False
        record.idempotency_key = key
        return True

    def attach_resume(self, candidate_id: str, resume: StoredResume) -> bool:
        record = self.candidates.get(candidate_id)
        if not record:
            return False
        record.resume = resume
        record.resume_hash = resume.sha256
        return True

    def assign(self, candidate_id: str, staff_id: str, staff_name=None) -> bool:
        record = self.candidates.get(candidate_id)
        if not record:
            return False
        record.assigned_staff_id = staff_id
        record.assigned_staff_name = staff_name
        record.assigned_at = self._tick()
        record.viewed_at = None
        record.evaluation_status = "pending"
        record.evaluation_score = None
        record.evaluation_notes = None
        return True


def _resume(filename: str, sha256: str) -> StoredResume:
    """A file already written to the CRM's storage, as intake receives one."""
    return StoredResume(
        original_filename=filename,
        mime_type="application/pdf",
        size=1024,
        sha256=sha256,
        storage_backend="local",
        storage_key=f"resumes/{filename}",
    )


def profile_for(**over) -> CandidateProfile:
    """What the bot maps a conversation onto. Phone is the WhatsApp number."""
    fields = {
        "full_name": "Ravi Kumar",
        "phone": f"+{CANDIDATE_NUMBER}",
        "phone_e164": f"+{CANDIDATE_NUMBER}",
        "country": "India",
        "destination_country": "Malaysia",
        "job_category": "general_worker",
        "is_resume": False,
        "confidence": 0.0,
    }
    fields.update(over)
    return CandidateProfile(**fields)


def submit(repo, *, line: str = LINES[0], wa_id: str = CANDIDATE_NUMBER, **over):
    """One `POST /candidates`, with allocation and notification held still.

    Both are patched by default because most of what follows is about identity,
    and the tests that *are* about allocation say so by patching them
    themselves.
    """
    with patch("app.services.candidate_intake.assign_candidate") as assign, patch(
        "app.services.candidate_intake._announce_assignment"
    ) as announce:
        assign.return_value = _Allocation()
        result = intake_whatsapp_candidate(
            profile=profile_for(**over),
            idempotency_key=key_for(line, wa_id),
            repo=repo,
        )
    result.assign_mock = assign
    result.announce_mock = announce
    return result


class _Allocation:
    """What `assign_candidate` hands back when it placed somebody."""

    assigned = True
    staff_id = "staff-1"
    staff_name = "Priya Sharma"


@pytest.fixture
def repo():
    return IdentityRepo()


# --------------------------------------------------------------------------- #
#  1. A registration that stopped halfway and started again
# --------------------------------------------------------------------------- #
def test_a_restarted_registration_is_the_same_candidate(repo):
    """Session 1 gives a name. Session 2, days later, finishes the job."""
    first = submit(repo, full_name="Ravi Kumar")
    second = submit(repo, destination_country="Singapore", job_preference="Welder")

    assert second.candidate_id == first.candidate_id
    assert second.created is False
    assert len(repo.candidates) == 1


def test_every_answer_after_the_first_actually_reaches_the_record(repo):
    """The regression this file exists for.

    The bot sends every answered question under one key — that is what makes a
    retry safe. Matching that key used to return early, so the record kept the
    first answer and nothing else: a candidate who answered eight questions
    arrived as a name and a phone number, and the desk saw a blank profile.
    """
    # Cumulative, because that is how the bot sends: every partial carries the
    # profile as it stands, so a later one never contradicts an earlier answer.
    answered: dict = {"full_name": "Ravi Kumar"}
    submit(repo, **answered)

    for field, value in (
        ("destination_country", "Singapore"),
        ("job_preference", "Welder"),
        ("city", "Chennai"),
        ("total_experience_band", "3_5"),
    ):
        answered[field] = value
        submit(repo, **answered)

    record = next(iter(repo.candidates.values()))
    assert record.profile.destination_country == "Singapore"
    assert record.profile.job_preference == "Welder"
    assert record.profile.city == "Chennai"
    assert record.profile.total_experience_band == "3_5"
    assert len(repo.candidates) == 1


def test_a_later_answer_never_blanks_an_earlier_one(repo):
    """A submission that omits a field is silent about it, not contradicting it.

    Every partial carries the whole profile as it stands, so this only bites on
    a field the conversation has not reached — but "not asked yet" must not
    overwrite "answered in the last session".
    """
    submit(repo, city="Chennai", job_preference="Welder")
    submit(repo, city=None)

    record = next(iter(repo.candidates.values()))
    assert record.profile.city == "Chennai"
    assert record.profile.job_preference == "Welder"


# --------------------------------------------------------------------------- #
#  2. The same person, a different company number
# --------------------------------------------------------------------------- #
def test_switching_company_numbers_does_not_split_the_candidate(repo):
    """WhatsApp A on Monday, WhatsApp B on Friday. One person, one record."""
    first = submit(repo, line="101")
    second = submit(repo, line="202", destination_country="Qatar")

    assert second.candidate_id == first.candidate_id
    assert len(repo.candidates) == 1
    assert next(iter(repo.candidates.values())).profile.destination_country == "Qatar"


def test_all_six_company_numbers_resolve_to_one_candidate(repo):
    """The claim in full, across the whole fleet.

    Six distinct idempotency keys — one per line — and one candidate, because
    the thing they have in common is the number the candidate is holding.
    """
    ids = {submit(repo, line=line).candidate_id for line in LINES}

    assert len(ids) == 1
    assert len(repo.candidates) == 1


def test_two_people_on_one_line_are_still_two_people(repo):
    """The converse, which is the half a phone-based rule could get wrong."""
    one = submit(repo, line="101", wa_id="919876543210")
    two = submit(
        repo,
        line="101",
        wa_id="919000000001",
        phone="+919000000001",
        phone_e164="+919000000001",
    )

    assert one.candidate_id != two.candidate_id
    assert len(repo.candidates) == 2


def test_a_shared_name_across_lines_is_not_a_shared_identity(repo):
    """Name is never a signal. Two Ravi Kumars are two welders."""
    one = submit(repo, line="303", full_name="Ravi Kumar")
    two = submit(
        repo,
        line="404",
        wa_id="919111111111",
        full_name="Ravi Kumar",
        phone="+919111111111",
        phone_e164="+919111111111",
    )

    assert one.candidate_id != two.candidate_id


# --------------------------------------------------------------------------- #
#  3. The candidate whose first message was a document
# --------------------------------------------------------------------------- #
def test_a_document_first_candidate_keeps_their_record_when_the_name_arrives(repo):
    """Somebody who sends a CV before answering anything.

    The bot submits as soon as there is one answer, and a document is an
    answer — so the record is created with a résumé, a phone number and the
    WhatsApp display name standing in for a real one. The name given three
    questions later has to land on that same record, not beside it.
    """
    resume = _resume("cv.pdf", uuid.uuid4().hex)

    with patch("app.services.candidate_intake.assign_candidate") as assign, patch(
        "app.services.candidate_intake._announce_assignment"
    ):
        assign.return_value = _Allocation()
        first = intake_whatsapp_candidate(
            profile=profile_for(full_name=CANDIDATE_NUMBER),
            idempotency_key=key_for("101"),
            resume=resume,
            repo=repo,
        )

    second = submit(repo, full_name="Ravi Kumar")

    assert second.candidate_id == first.candidate_id
    record = repo.candidates[first.candidate_id]
    assert record.profile.full_name == "Ravi Kumar"
    # And the file they sent first is still the file on the record.
    assert record.resume is not None
    assert record.resume.sha256 == resume.sha256


def test_a_second_resume_never_replaces_the_one_on_file(repo):
    """A recruiter may have read the first one and formed a view."""
    original = _resume("first.pdf", "a" * 64)
    replacement = _resume("second.pdf", "b" * 64)

    with patch("app.services.candidate_intake.assign_candidate") as assign, patch(
        "app.services.candidate_intake._announce_assignment"
    ):
        assign.return_value = _Allocation()
        created = intake_whatsapp_candidate(
            profile=profile_for(),
            idempotency_key=key_for("101"),
            resume=original,
            repo=repo,
        )
        intake_whatsapp_candidate(
            profile=profile_for(),
            idempotency_key=key_for("202"),
            resume=replacement,
            repo=repo,
        )

    assert repo.candidates[created.candidate_id].resume.sha256 == original.sha256


# --------------------------------------------------------------------------- #
#  4. Everything that happens after somebody owns them
# --------------------------------------------------------------------------- #
def test_updates_after_assignment_keep_the_existing_owner(repo):
    """More answers are not a new candidate, so nobody is reallocated."""
    created = submit(repo)
    repo.assign(created.candidate_id, "staff-7", "Arun Nair")

    again = submit(repo, destination_country="Singapore", job_preference="Fitter")

    record = repo.candidates[created.candidate_id]
    assert again.candidate_id == created.candidate_id
    assert record.assigned_staff_id == "staff-7"
    assert record.assigned_staff_name == "Arun Nair"
    # And what they told us did move.
    assert record.profile.destination_country == "Singapore"


def test_updates_after_assignment_do_not_reallocate_or_re_announce(repo):
    """One notification per allocation, and a refresh is not an allocation.

    Registration is a dozen answers and a handful of documents, each one a
    submission. Announcing any of them would be a dozen WhatsApp messages to a
    staff member about one candidate they were already given.
    """
    first = submit(repo)
    assert first.assign_mock.call_count == 1
    assert first.announce_mock.call_count == 1

    for _ in range(5):
        later = submit(repo, job_preference="Welder")
        assert later.assign_mock.call_count == 0
        assert later.announce_mock.call_count == 0


def test_a_refresh_cannot_reach_an_evaluation(repo):
    """The one that destroys real work if it breaks, restated for this path.

    Assessed and rejected last week; walks back into the bot this morning.
    """
    created = submit(repo)
    record = repo.candidates[created.candidate_id]
    record.assigned_staff_id = "staff-7"
    record.evaluation_status = "rejected"
    record.evaluation_score = 2
    record.evaluation_notes = "Not suitable for this client"
    record.viewed_at = utcnow()

    submit(repo, line="606", destination_country="Kuwait")

    after = repo.candidates[created.candidate_id]
    assert after.profile.destination_country == "Kuwait"
    assert after.evaluation_status == "rejected"
    assert after.evaluation_score == 2
    assert after.evaluation_notes == "Not suitable for this client"
    assert after.assigned_staff_id == "staff-7"
    assert after.viewed_at is not None


def test_a_new_candidate_is_announced_exactly_once(repo):
    """The other half: allocation that really happened has to be told to
    somebody. It was not — intake allocated and notified nobody, so a candidate
    who registered on WhatsApp landed on a desk in silence."""
    created = submit(repo)

    assert created.created is True
    assert created.announce_mock.call_count == 1
    candidate_id, _profile, allocation = created.announce_mock.call_args.args
    assert candidate_id == created.candidate_id
    assert allocation.staff_id == "staff-1"


def test_an_unplaceable_candidate_is_not_announced(repo):
    """An empty roster is not an allocation to tell anybody about."""

    class _NoStaff:
        assigned = False
        staff_id = None
        staff_name = None

    with patch("app.services.candidate_intake.assign_candidate") as assign, patch(
        "app.services.candidate_intake._announce_assignment"
    ) as announce:
        assign.return_value = _NoStaff()
        intake_whatsapp_candidate(
            profile=profile_for(), idempotency_key=key_for("101"), repo=repo
        )

    announce.assert_not_called()


def test_a_failed_allocation_does_not_fail_the_intake(repo):
    """The candidate is already written down. Losing them to a balancer that
    raised would be exactly the wrong trade."""
    with patch(
        "app.services.candidate_intake.assign_candidate",
        side_effect=RuntimeError("roster unreachable"),
    ), patch("app.services.candidate_intake._announce_assignment") as announce:
        result = intake_whatsapp_candidate(
            profile=profile_for(), idempotency_key=key_for("101"), repo=repo
        )

    assert result.created is True
    assert result.candidate_id in repo.candidates
    announce.assert_not_called()


# --------------------------------------------------------------------------- #
#  5. Which signal found them, and what gets written down about it
# --------------------------------------------------------------------------- #
def test_a_record_found_by_phone_adopts_the_key_that_found_it(repo):
    """So the next submission takes the direct route, and so the link is
    recorded rather than re-derived from a phone number every time."""
    created = submit(repo, line="101")
    record = repo.candidates[created.candidate_id]
    record.idempotency_key = None  # a record from before keys existed

    submit(repo, line="505")

    assert repo.candidates[created.candidate_id].idempotency_key == key_for("505")


def test_a_key_already_on_a_record_is_never_overwritten(repo):
    """That key names the conversation that created the record. A later
    submission is a different conversation and does not get to rename it."""
    created = submit(repo, line="101")
    submit(repo, line="202")

    assert repo.candidates[created.candidate_id].idempotency_key == key_for("101")


def test_the_key_wins_over_the_phone_when_they_disagree(repo):
    """The more specific claim. A key identifies *this* submission; a phone
    identifies a person, and a person can have more than one record from before
    any of this existed."""
    first = submit(repo, line="101")
    # A second record carrying the same number, as a historic split would look.
    stray = CandidateRecord(
        id=uuid.uuid4().hex,
        source="whatsapp",
        profile=profile_for(),
        phone_key=normalize_phone(f"+{CANDIDATE_NUMBER}"),
        cv_required=False,
        status="ingested",
        ingested_at=utcnow(),
    )
    repo.insert(stray)

    again = submit(repo, line="101")
    assert again.candidate_id == first.candidate_id


def test_the_oldest_record_wins_when_a_phone_reaches_two(repo):
    """The oldest is the one with the history hanging off it — the allocation,
    the verdict, the documents already filed."""
    older = CandidateRecord(
        id=uuid.uuid4().hex,
        source="whatsapp",
        profile=profile_for(),
        phone_key=normalize_phone(f"+{CANDIDATE_NUMBER}"),
        cv_required=False,
        status="ingested",
        ingested_at=utcnow(),
    )
    newer = CandidateRecord(
        id=uuid.uuid4().hex,
        source="whatsapp",
        profile=profile_for(),
        phone_key=normalize_phone(f"+{CANDIDATE_NUMBER}"),
        cv_required=False,
        status="ingested",
        ingested_at=utcnow(),
    )
    repo.insert(older)
    repo.insert(newer)

    landed = submit(repo, line="101")
    assert landed.candidate_id == older.id


# --------------------------------------------------------------------------- #
#  6. When there is no phone to key on
# --------------------------------------------------------------------------- #
def test_a_candidate_with_no_number_keeps_a_session_identity(repo):
    """Nothing to resolve against, so the conversation is the identity.

    This cannot happen over WhatsApp — the number *is* the account — but the
    endpoint does not get to assume its caller, and a submission with no phone
    must produce one record that accumulates rather than one per answer.
    """
    first = submit(repo, line="101", phone=None, phone_e164=None)
    second = submit(repo, line="101", phone=None, phone_e164=None, job_preference="Mason")

    assert second.candidate_id == first.candidate_id
    assert len(repo.candidates) == 1
    assert repo.candidates[first.candidate_id].profile.job_preference == "Mason"


def test_a_number_arriving_later_becomes_the_identity(repo):
    """And from then on the phone is what finds them, including from a
    different company number under a different key."""
    first = submit(repo, line="101", phone=None, phone_e164=None)
    submit(repo, line="101")  # the number arrives

    from_another_line = submit(repo, line="404")

    assert from_another_line.candidate_id == first.candidate_id
    assert len(repo.candidates) == 1


# --------------------------------------------------------------------------- #
#  7. Two arriving at once
# --------------------------------------------------------------------------- #
def test_two_simultaneous_first_submissions_yield_one_candidate(repo):
    """Both pass the lookup; the unique index arbitrates.

    Staged at the service, which is the only place the race can be reproduced
    exactly: two intakes built against a repository that has nothing in it.
    """
    first = submit(repo, line="101")
    second = submit(repo, line="101")

    assert first.candidate_id == second.candidate_id
    assert len(repo.candidates) == 1


# --------------------------------------------------------------------------- #
#  8. What the real repository sends to MongoDB
#
#  The fake above claims two things about the database. These check the queries
#  that have to be true for those claims to hold, because a fake agreeing with
#  itself proves nothing about Atlas.
# --------------------------------------------------------------------------- #
class _RecordingCollection:
    """Captures the filter, update and sort a repository call actually issues."""

    def __init__(self, doc=None, modified=1):
        self.doc = doc
        self.modified = modified
        self.calls: list = []

    def find_one(self, query, projection=None, sort=None):
        self.calls.append({"query": query, "sort": sort})
        return self.doc

    def update_one(self, query, update):
        self.calls.append({"query": query, "update": update})

        class _Result:
            modified_count = self.modified
            matched_count = self.modified

        return _Result()


def test_the_phone_lookup_asks_the_database_for_the_oldest():
    """Not "a" match. `find_one` with no sort is whichever document the storage
    engine reaches first, and that is not stable between two calls."""
    coll = _RecordingCollection(doc=None)
    CandidateRepository(collection=coll).find_by_email_or_phone(None, "9876543210")

    assert coll.calls[0]["sort"] == [("created_at", 1)]
    assert coll.calls[0]["query"] == {"$or": [{"phone_key": "9876543210"}]}


def test_adopting_a_key_only_ever_fills_a_blank():
    """The filter is what makes that true when two submissions arrive together.
    A read-then-write would let the second overwrite the first."""
    coll = _RecordingCollection()
    CandidateRepository(collection=coll).adopt_idempotency_key("cand-1", "whatsapp/101/91")

    query = coll.calls[0]["query"]
    assert query["idempotency_key"] == {"$in": [None, ""]}
    assert coll.calls[0]["update"] == {"$set": {"idempotency_key": "whatsapp/101/91"}}


def test_adopting_a_key_another_record_holds_is_swallowed():
    """The sparse unique index refusing is not an error: the profile refresh
    this accompanies has already happened and is the half that mattered."""
    from pymongo.errors import DuplicateKeyError

    class _Colliding(_RecordingCollection):
        def update_one(self, query, update):
            raise DuplicateKeyError("idempotency_key_unique")

    assert (
        CandidateRepository(collection=_Colliding()).adopt_idempotency_key(
            "cand-1", "whatsapp/101/91"
        )
        is False
    )


def test_adopting_nothing_is_not_a_write():
    coll = _RecordingCollection()
    assert CandidateRepository(collection=coll).adopt_idempotency_key("cand-1", None) is False
    assert not coll.calls


# --------------------------------------------------------------------------- #
#  9. One notification per actual change of owner
#
#  Driven at the route function rather than through HTTP: the admin dependency
#  is not what is being tested, and calling it directly is what lets the same
#  assignment be replayed exactly as a double-click or a retried request would.
# --------------------------------------------------------------------------- #
def _staff(staff_id: str, name: str):
    return User(id=staff_id, email=f"{staff_id}@example.com", name=name, role=STAFF_ROLE)


def _assign_route(repo, candidate_id, staff, notify):
    class _Users:
        def get(self, staff_id):
            return staff if staff_id == staff.id else None

    with patch("app.api.routes.users", _Users()), patch(
        "app.api.routes.repo", return_value=repo
    ), patch("app.api.routes.notify_candidate_assigned", notify), patch(
        "app.api.routes.relay_assignment", return_value=True
    ):
        return assign_candidate_route(
            candidate_id, AssignRequest(staff_id=staff.id), _admin={}
        )


def test_a_genuine_reassignment_tells_the_new_owner(repo):
    """Ownership really moved, so the person it moved to is told."""
    created = submit(repo)
    repo.assign(created.candidate_id, "staff-1", "Priya Sharma")

    notify = MagicMock()
    outcome = _assign_route(repo, created.candidate_id, _staff("staff-2", "Arun Nair"), notify)

    assert outcome["status"] == "assigned"
    assert outcome["whatsapp_notified"] is True
    assert repo.candidates[created.candidate_id].assigned_staff_id == "staff-2"
    assert notify.call_count == 1
    assert notify.call_args.args[0] == "staff-2"


def test_reassigning_to_the_current_owner_changes_nothing(repo):
    """A double-clicked button, or a retried request.

    Two things would happen if this went through, and both destroy something:
    `assign` clears `viewed_at` and the verdict, so the evaluation that person
    just wrote is thrown away; and the message goes out a second time for work
    they were told about the first time.
    """
    created = submit(repo)
    repo.assign(created.candidate_id, "staff-1", "Priya Sharma")

    record = repo.candidates[created.candidate_id]
    record.viewed_at = utcnow()
    record.evaluation_status = "shortlisted"
    record.evaluation_notes = "Strong welding history"

    notify = MagicMock()
    outcome = _assign_route(repo, created.candidate_id, _staff("staff-1", "Priya Sharma"), notify)

    assert outcome["status"] == "unchanged"
    notify.assert_not_called()
    after = repo.candidates[created.candidate_id]
    assert after.evaluation_status == "shortlisted"
    assert after.evaluation_notes == "Strong welding history"
    assert after.viewed_at is not None


def test_a_duplicated_assignment_event_notifies_once(repo):
    """The same request arriving twice is one assignment, not two."""
    created = submit(repo)
    notify = MagicMock()
    staff = _staff("staff-2", "Arun Nair")

    _assign_route(repo, created.candidate_id, staff, notify)
    _assign_route(repo, created.candidate_id, staff, notify)
    _assign_route(repo, created.candidate_id, staff, notify)

    assert notify.call_count == 1


def test_moving_a_candidate_back_is_a_real_change(repo):
    """A -> B -> A. The third step is somebody genuinely being given work
    again, and it is announced, because by then they no longer held it."""
    created = submit(repo)
    notify = MagicMock()
    priya = _staff("staff-1", "Priya Sharma")
    arun = _staff("staff-2", "Arun Nair")

    _assign_route(repo, created.candidate_id, priya, notify)
    _assign_route(repo, created.candidate_id, arun, notify)
    _assign_route(repo, created.candidate_id, priya, notify)

    assert notify.call_count == 3
    assert [call.args[0] for call in notify.call_args_list] == [
        "staff-1", "staff-2", "staff-1",
    ]
