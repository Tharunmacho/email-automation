"""Message identity that says which mailbox it came from.

An IMAP UID is a number that means something only inside one folder of one
account. `611` is not a message; it is the 611th slot in *some* mailbox, and
every account numbers its own slots from 1. Two mailboxes polled by one
deployment therefore hand out the same ids for entirely unrelated mail.

That mattered because a bare UID was being used as global identity — the ingest
ledger, the deletion tombstones, the candidate's `source_email.message_id` and
the Redis per-message claim all key on that one string. A CV arriving on one
account as UID 611 would be dropped as "already settled" because an unrelated
message on the *other* account had been judged under the same number, or worse,
suppressed as belonging to a deleted candidate. Nothing would log it; from the
poller's side the message simply was not in the answer.

So the identifier carries its account: `cv@adiragroups.com:611`. The point of
fixing it here, rather than by passing an account alongside the id, is that
there is then no call site left that *can* forget — the id is correct or it is
not an id.

Legacy ids without an account still read correctly (`local_id_of("611") ==
"611"`), so rows written before this change are not misread. They simply no
longer match the qualified ids the poll now produces, which is the intended
outcome: a bare row cannot say which account it belonged to, and guessing is
what caused the bug.
"""
from __future__ import annotations

#: Splits account from local id. A colon cannot appear in an email address, and
#: splitting from the *right* keeps a Windows credentials path (`C:\...json`)
#: intact on the account side.
_SEPARATOR = ":"


def qualify(account: str, local_id: str) -> str:
    """`("cv@adiragroups.com", "611")` -> `"cv@adiragroups.com:611"`.

    An empty account returns the local id unchanged rather than inventing a
    prefix: a client that cannot say who it is should produce the old shape,
    not a wrong one.
    """
    account = (account or "").strip()
    local_id = str(local_id)
    if not account:
        return local_id
    return f"{account}{_SEPARATOR}{local_id}"


def is_qualified(message_id: str) -> bool:
    return _SEPARATOR in (message_id or "")


def account_of(message_id: str) -> str:
    """The mailbox this id belongs to, or "" for a legacy unqualified id."""
    if not is_qualified(message_id):
        return ""
    return message_id.rsplit(_SEPARATOR, 1)[0]


def local_id_of(message_id: str) -> str:
    """The part the mail server understands — the bare UID or provider id.

    Accepts an unqualified id and returns it unchanged, so anything still
    holding an old id (a queued task, a stored row) keeps working.
    """
    if not is_qualified(message_id):
        return message_id or ""
    return message_id.rsplit(_SEPARATOR, 1)[1]
