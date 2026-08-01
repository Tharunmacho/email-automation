"""User accounts for the admin console.

One document per user in the ``users`` collection. Only the password *hash* is
ever stored — see app.core.security.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from pymongo import ASCENDING

from app.core.models import utcnow
from app.core.security import hash_password, verify_password
from app.db.mongo import get_db
from app.logging_config import get_logger

log = get_logger(__name__)

USERS_COLLECTION = "users"


def get_users_collection():
    return get_db()[USERS_COLLECTION]


@dataclass
class User:
    id: str
    email: str
    name: str
    role: str

    def to_public(self) -> dict:
        """The shape sent to the browser — never includes the hash."""
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role}


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
        )

    def authenticate(self, email: str, password: str) -> Optional[User]:
        """Return the user when the password matches, else None.

        Deliberately gives the caller no way to tell "no such user" apart from
        "wrong password" — that distinction lets an attacker enumerate accounts.
        """
        doc = self.find_by_email(email)
        if not doc:
            # Still run a hash so a missing account and a wrong password take
            # roughly the same time and cannot be told apart by timing.
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
            "password_hash": hash_password(password),
            "created_at": utcnow(),
            "last_login_at": None,
        }
        self._coll.insert_one(doc)
        log.info("Created user %s (%s)", email, role)
        return self._to_user(doc)

    def set_password(self, email: str, password: str) -> bool:
        res = self._coll.update_one(
            {"email": _normalize(email)},
            {"$set": {"password_hash": hash_password(password), "updated_at": utcnow()}},
        )
        return res.modified_count > 0

    def count(self) -> int:
        return self._coll.count_documents({})


def ensure_seed_user(email: str, password: str, name: str = "Administrator") -> None:
    """Create the initial admin account if it does not exist yet.

    Only ever *creates*. An existing account's password is left alone, so a
    deployment cannot silently reset a changed password back to the seed value.
    """
    if not email or not password:
        return
    repo = UserRepository()
    if repo.find_by_email(email):
        return
    repo.create(email=email, password=password, name=name, role="admin")
    log.info("Seeded initial admin account: %s", email)


def ensure_user_indexes() -> None:
    get_users_collection().create_index(
        [("email", ASCENDING)], name="user_email_unique", unique=True
    )
