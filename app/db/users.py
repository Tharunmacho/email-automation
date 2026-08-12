"""User accounts for the admin console.

One document per user in the ``users`` collection. Only the password *hash* is
ever stored — see app.core.security.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from pymongo import ASCENDING

from app.core.models import utcnow
from app.core.security import hash_password, verify_password
from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

ADMIN_ROLE = "admin"
STAFF_ROLE = "staff"
USERS_COLLECTION = "users"


def get_users_collection():
    return get_db()[USERS_COLLECTION]


@dataclass
class User:
    id: str
    email: str
    name: str
    role: str
    keywords: list[str] = None
    active: bool = True
    created_at: Optional[datetime] = None

    def to_public(self) -> dict:
        """The shape sent to the browser — never includes the hash."""
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "keywords": self.keywords or [],
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


class UserRepository:
    def __init__(self, collection=None):
        self._coll = collection if collection is not None else get_users_collection()

    def find_by_email(self, email: str) -> Optional[dict]:
        return self._coll.find_one({"email": _normalize(email)})

    def get(self, user_id: str) -> Optional[User]:
        doc = self._coll.find_one({"_id": user_id})
        return self._to_user(doc) if doc else None

    @staticmethod
    def _to_user(doc: dict) -> User:
        return User(
            id=doc["_id"],
            email=doc.get("email", ""),
            name=doc.get("name") or doc.get("email", "").split("@")[0].title(),
            role=doc.get("role", "admin"),
            keywords=doc.get("keywords", []),
            active=doc.get("active", True),
            created_at=doc.get("created_at"),
        )

    def authenticate(self, email: str, password: str) -> Optional[User]:
        """Return the user when the password matches, else None."""
        doc = self.find_by_email(email)
        if not doc:
            verify_password(password, hash_password("timing-equalizer"))
            return None
        if not verify_password(password, doc.get("password_hash", "")):
            return None

        self._coll.update_one({"_id": doc["_id"]}, {"$set": {"last_login_at": utcnow()}})
        return self._to_user(doc)

    def create(self, email: str, password: str, name: str = "", role: str = "admin") -> User:
        email = _normalize(email)
        if self.find_by_email(email):
            raise ValueError(f"A user with email {email} already exists.")
        doc = {
            "_id": uuid.uuid4().hex,
            "email": email,
            "name": name or email.split("@")[0].title(),
            "role": role,
            # Stored explicitly: an account without the field would be filtered
            # out of every `active: True` query, and a staff member allocation
            # cannot see is a staff member who never receives work.
            "active": True,
            "keywords": [],
            "password_hash": hash_password(password),
            "created_at": utcnow(),
            "last_login_at": None,
        }
        self._coll.insert_one(doc)
        log.info("Created user %s (%s)", email, role)
        return self._to_user(doc)

    def list_admins(self, include_inactive: bool = False) -> list[User]:
        """Every administrator, for events that fan out to all of them.

        Used by the notification service: an ingested candidate is news to the
        staff member it lands with *and* to whoever is running the sync, and the
        admin half needs a list of who those people are.

        Defaults to active accounts only — a deactivated admin cannot sign in,
        so writing a notification they will never read is just retention cost.
        """
        query: dict = {"role": ADMIN_ROLE}
        if not include_inactive:
            query["active"] = {"$ne": False}
        docs = list(self._coll.find(query).sort("created_at", ASCENDING))
        return [self._to_user(d) for d in docs]

    def list_staff(self, include_inactive: bool = True) -> list[User]:
        query: dict = {"role": STAFF_ROLE}
        if not include_inactive:
            # `$ne: False` rather than `== True`: accounts created before the
            # flag existed have no `active` field, and treating those as
            # deactivated would quietly empty the allocation pool.
            query["active"] = {"$ne": False}
        docs = list(self._coll.find(query).sort("created_at", ASCENDING))
        return [self._to_user(d) for d in docs]

    def list_assignable_staff(self) -> list[User]:
        """The pool allocation draws from: active staff accounts, in join order.

        The balancer's single source of "who could this go to" — a deactivated
        account keeps the work it already holds but receives nothing new.
        """
        return self.list_staff(include_inactive=False)

    def create_staff(
        self,
        email: str,
        password: str,
        name: str = "",
        keywords: list[str] = None,
    ) -> User:
        email = _normalize(email)
        if self.find_by_email(email):
            raise ValueError(f"A user with email {email} already exists.")
        doc = {
            "_id": uuid.uuid4().hex,
            "email": email,
            "name": name or email.split("@")[0].title(),
            "role": STAFF_ROLE,
            "keywords": keywords or [],
            "active": True,
            "password_hash": hash_password(password),
            "created_at": utcnow(),
            "last_login_at": None,
        }
        self._coll.insert_one(doc)
        log.info("Created staff user %s", email)
        return self._to_user(doc)

    def update_staff(
        self,
        staff_id: str,
        name: str | None = None,
        keywords: list[str] | None = None,
        active: bool | None = None,
        password: str | None = None,
    ) -> User | None:
        doc = self._coll.find_one({"_id": staff_id, "role": STAFF_ROLE})
        if not doc:
            return None
        updates: dict = {"updated_at": utcnow()}
        if name is not None:
            updates["name"] = name
        if keywords is not None:
            updates["keywords"] = keywords
        if active is not None:
            updates["active"] = active
        if password:
            updates["password_hash"] = hash_password(password)

        self._coll.update_one({"_id": staff_id}, {"$set": updates})
        return self.get(staff_id)

    def delete_staff(self, staff_id: str) -> bool:
        res = self._coll.delete_one({"_id": staff_id, "role": STAFF_ROLE})
        return res.deleted_count > 0

    def set_password(self, email: str, password: str) -> bool:
        res = self._coll.update_one(
            {"email": _normalize(email)},
            {"$set": {"password_hash": hash_password(password), "updated_at": utcnow()}},
        )
        return res.modified_count > 0

    def count(self) -> int:
        return self._coll.count_documents({})


def ensure_seed_user(email: str, password: str, name: str = "Administrator") -> None:
    repo = UserRepository()
    if email and password:
        existing = repo.find_by_email(email)
        if not existing:
            repo.create(email=email, password=password, name=name, role="admin")
            log.info("Seeded initial admin account: %s", email)
        else:
            repo.set_password(email, password)

    from app.config import settings

    if settings.demo_admin_email and settings.demo_admin_password:
        existing_demo_admin = repo.find_by_email(settings.demo_admin_email)
        if not existing_demo_admin:
            repo.create(
                email=settings.demo_admin_email,
                password=settings.demo_admin_password,
                name="Super Admin",
                role="admin",
            )
            log.info("Seeded demo admin account: %s", settings.demo_admin_email)
        else:
            repo.set_password(settings.demo_admin_email, settings.demo_admin_password)

    if settings.demo_staff_email and settings.demo_staff_password:
        existing_staff = repo.find_by_email(settings.demo_staff_email)
        if not existing_staff:
            repo.create_staff(
                email=settings.demo_staff_email,
                password=settings.demo_staff_password,
                name="Staff Reviewer",
                keywords=["Python", "React", "DevOps", "Java", "SQL"],
            )
            log.info("Seeded initial staff account: %s", settings.demo_staff_email)
        else:
            repo.set_password(settings.demo_staff_email, settings.demo_staff_password)


def ensure_demo_accounts(
    admin_email: str,
    admin_password: str,
    staff_email: str,
    staff_password: str,
) -> list[str]:
    """Create the two accounts the login screen advertises, once.

    Creates, never overwrites: an operator who changes a demo password keeps
    that change across restarts. Both roles are seeded because the staff account
    is the only thing that demonstrates the isolation — there is nothing to
    isolate without one.

    Returns the addresses actually created, so a boot log can say what it did.
    """
    repo = UserRepository()
    created: list[str] = []
    for email, password, name, role in (
        (admin_email, admin_password, "Super Admin", ADMIN_ROLE),
        (staff_email, staff_password, "Staff Member", STAFF_ROLE),
    ):
        if not email or not password:
            continue
        if repo.find_by_email(email):
            continue
        repo.create(email=email, password=password, name=name, role=role)
        created.append(email)
        log.info("Seeded demo %s account: %s", role, email)
    return created


def ensure_user_indexes() -> None:
    from app.db.mongo import ensure_index

    coll = get_users_collection()
    # Sign-in reads by address, and two accounts on one address would make
    # authentication ambiguous.
    ensure_index(coll, [("email", ASCENDING)], "user_email_unique", unique=True)
    # The allocation pool: active staff, in join order. Read on every ingested
    # résumé, so it is not a rare query.
    ensure_index(coll, [("role", ASCENDING), ("active", ASCENDING)], "user_role_active_idx")
