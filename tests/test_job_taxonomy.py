"""Jobs as rows, and the CV rule an admin writes on one.

The rule used to be a table in the source. It is now a document an admin edits,
and that swap is only safe if two things hold, so both are tested here:

* **The resolution order is the one the admin form implies.** A country
  override beats the job default, the job default beats everything else, and an
  unknown job is asked for a CV. An admin who ticks "Malaysia: not required"
  and sees a candidate asked for a CV anyway has been given a form that lies.

* **Nothing changed for the jobs that already existed.** The seeded rows are a
  transcription of the old built-in table, and a candidate registering after
  this shipped must be asked for exactly what they would have been asked for
  before it. That claim is worth a test because it is the one nobody would
  notice breaking until a client complained.

The database is a dictionary here. These tests are about which rule wins, and a
real MongoDB would not make that more true — it would only make the suite need
one running.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db.taxonomy import SEED_JOBS, job_doc, normalise_country, slugify
from app.db.users import PAGES, ROLE_DEFAULT_PAGES, pages_for
from app.policy.cv_policy import resolve_cv_requirement


# --------------------------------------------------------------------------- #
#  A dictionary that answers the two questions the policy asks of Mongo
# --------------------------------------------------------------------------- #
class FakeJobs:
    def __init__(self, docs=()):
        self.docs = {d["_id"]: dict(d) for d in docs}

    def find_one(self, query, *args, **kwargs):
        doc = self.docs.get(query.get("_id"))
        return dict(doc) if doc else None

    def count_documents(self, _query):
        return len(self.docs)


def with_jobs(*docs):
    """Point `get_job` at a fake table for the duration of a test."""
    fake = FakeJobs(docs)

    def _get_job(job_id):
        doc = fake.find_one({"_id": job_id})
        if doc:
            doc.pop("_id", None)
        return doc

    return patch("app.db.taxonomy.get_job", side_effect=_get_job)


GENERAL_WORKER = job_doc(
    job_id="general_worker",
    title="General Worker",
    cv_required_default=True,
    cv_overrides={"Malaysia": False, "Singapore": False},
)
CNC = job_doc(job_id="cnc_operator", title="CNC Operator", cv_required_default=True)


# --------------------------------------------------------------------------- #
#  Resolution order
# --------------------------------------------------------------------------- #
def test_a_country_override_beats_the_job_default():
    with with_jobs(GENERAL_WORKER):
        required, reason = resolve_cv_requirement("Malaysia", "general_worker")
    assert required is False
    assert "Malaysia" in reason


def test_the_job_default_applies_where_no_override_exists():
    with with_jobs(GENERAL_WORKER):
        required, reason = resolve_cv_requirement("Qatar", "general_worker")
    assert required is True
    assert "default" in reason


def test_a_job_with_no_overrides_uses_its_default_everywhere():
    with with_jobs(CNC):
        assert resolve_cv_requirement("Malaysia", "cnc_operator")[0] is True
        assert resolve_cv_requirement("Singapore", "cnc_operator")[0] is True
        assert resolve_cv_requirement(None, "cnc_operator")[0] is True


@pytest.mark.parametrize("country", ["Malaysia", "malaysia", "  MALAYSIA  ", "mAlAysIa"])
def test_the_override_matches_however_the_country_is_written(country):
    """Four sources write a country four ways; a rule that only answers to one
    of them is a rule that silently does not apply."""
    with with_jobs(GENERAL_WORKER):
        assert resolve_cv_requirement(country, "general_worker")[0] is False


def test_an_unknown_job_is_asked_for_a_cv():
    """The safe direction: an unnecessary CV costs a question, a missing one
    costs a placement."""
    with with_jobs(GENERAL_WORKER):
        assert resolve_cv_requirement("Narnia", "wizard")[0] is True
        assert resolve_cv_requirement(None, None)[0] is True


def test_a_lookup_failure_does_not_take_registration_down():
    """A database that will not answer must not close intake. The built-in
    table answers instead, and it answers the way it always did."""
    with patch("app.db.taxonomy.get_job", side_effect=RuntimeError("mongo is down")):
        assert resolve_cv_requirement("Malaysia", "general_worker")[0] is False
        assert resolve_cv_requirement("Malaysia", "technician")[0] is True


# --------------------------------------------------------------------------- #
#  Nothing changed for the jobs that already existed
# --------------------------------------------------------------------------- #
#: What the built-in table answered before jobs became rows, for every
#: destination/job pair the bot could actually produce.
BEHAVIOUR_BEFORE = [
    ("Malaysia", "general_worker", False),
    ("Malaysia", "factory_warehouse", False),
    ("Malaysia", "packing", False),
    ("Malaysia", "cleaning_housekeeping", False),
    ("Malaysia", "construction", False),
    ("Singapore", "general_worker", False),
    ("Singapore", "factory_warehouse", False),
    ("Singapore", "packing", False),
    ("Singapore", "cleaning_housekeeping", False),
    ("Singapore", "construction", False),
    ("Malaysia", "technician", True),
    ("Singapore", "technician", True),
    ("Malaysia", "electrical_mechanical", True),
    ("Singapore", "fabrication_welding", True),
    ("Malaysia", "driver_operator", True),
    ("Malaysia", "hospitality", True),
    ("Singapore", "sales_retail", True),
]


@pytest.mark.parametrize("country,job_id,expected", BEHAVIOUR_BEFORE)
def test_the_seeded_rows_reproduce_the_old_table_exactly(country, job_id, expected):
    seeded = [
        job_doc(
            job_id=seed["id"],
            title=seed["title"],
            cv_required_default=seed["default"],
            cv_overrides=seed["overrides"],
        )
        for seed in SEED_JOBS
    ]
    with with_jobs(*seeded):
        assert resolve_cv_requirement(country, job_id)[0] is expected


def test_every_job_the_bot_can_send_has_a_seeded_row():
    """A job id with no row falls through to the built-in default, which is
    'CV required' — correct, but not what the old table said for half of them."""
    from app.policy.cv_policy import JOB_CATEGORIES

    seeded = {seed["id"] for seed in SEED_JOBS}
    assert set(JOB_CATEGORIES) <= seeded


# --------------------------------------------------------------------------- #
#  Ids
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title,expected",
    [
        ("General Worker", "general_worker"),
        ("CNC Operator", "cnc_operator"),
        ("Driver / Operator", "driver_operator"),
        ("  Welder (6G)  ", "welder_6g"),
        ("!!!", "job"),
    ],
)
def test_ids_are_derived_from_the_title_once(title, expected):
    assert slugify(title) == expected


def test_country_normalisation_is_what_the_overrides_are_keyed_on():
    assert normalise_country("  Malaysia ") == normalise_country("MALAYSIA") == "malaysia"


# --------------------------------------------------------------------------- #
#  Permissions
# --------------------------------------------------------------------------- #
def test_an_admin_reaches_every_page():
    """Including the page where permissions are edited. An admin who can lock
    themselves out of it is a support call with no answer."""
    assert set(pages_for("admin")) == set(PAGES)
    assert "users" in pages_for("admin")


def test_a_staff_member_reaches_their_queue_and_account_settings_by_default():
    assert pages_for("staff") == ["my-queue", "settings"]


def test_grants_add_and_never_subtract():
    granted = pages_for("staff", ["candidates", "job-orders"])
    assert "my-queue" in granted, "a grant must not cost a staff member their own queue"
    assert set(granted) == {"my-queue", "candidates", "job-orders", "settings"}


def test_a_grant_for_a_page_that_does_not_exist_is_ignored():
    assert pages_for("staff", ["nonsense", "candidates"]) == ["candidates", "my-queue", "settings"]


def test_an_admins_grants_cannot_reduce_what_they_reach():
    assert set(pages_for("admin", [])) == set(PAGES)


def test_the_page_vocabulary_and_the_role_floors_agree():
    """Every page named in a role's floor has to be a real page."""
    for role, floor in ROLE_DEFAULT_PAGES.items():
        assert floor <= set(PAGES), f"{role} has a floor page that does not exist"
