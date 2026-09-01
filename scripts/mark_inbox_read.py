"""Empty the poll queue: mark every INBOX message read, on every account.

`search_message_ids` asks for UNSEEN, so unread *is* the queue. Marking the
whole inbox read draws a line under everything that arrived before now — none
of it is offered to the poll again — and leaves the mailboxes clean for fresh
mail, which arrives unread and is ingested in the ordinary way.

Nothing is deleted, moved or labelled. The only change is the read flag, on the
polled folder, on each configured account.

    python scripts/mark_inbox_read.py            # report what it would do
    python scripts/mark_inbox_read.py --apply    # do it
"""
from __future__ import annotations

import argparse
import imaplib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings

#: The read flag, spelled the way IMAP wants it in a STORE.
_SEEN_FLAG = r"(\Seen)"

#: Servers reject an over-long command line, and a mailbox with thousands of
#: unread messages would build one. UID STORE takes a set, so send it in pieces.
_CHUNK = 200


def _accounts() -> list[dict]:
    accounts = settings.email_accounts
    if accounts:
        return [a for a in accounts if a.get("provider", "smtp_imap") == "smtp_imap"]
    return [{
        "imap_server": settings.imap_server,
        "imap_port": settings.imap_port,
        "imap_username": settings.imap_username,
        "imap_password": settings.imap_password,
        "imap_folder": settings.imap_folder,
    }]


def mark_all_read(apply: bool) -> int:
    total = 0

    for account in _accounts():
        user = account["imap_username"]
        folder = account.get("imap_folder") or "INBOX"
        mail = imaplib.IMAP4_SSL(account["imap_server"], int(account.get("imap_port", 993)))
        mail.login(user, account["imap_password"])
        mail.select(folder, readonly=not apply)

        status, data = mail.uid("search", None, "UNSEEN")
        uids = [u.decode() for u in data[0].split()] if status == "OK" and data and data[0] else []
        print(f"{user} [{folder}]: {len(uids)} unread")

        if apply:
            for start in range(0, len(uids), _CHUNK):
                chunk = uids[start:start + _CHUNK]
                mail.uid("store", ",".join(chunk), "+FLAGS", _SEEN_FLAG)

        total += len(uids)
        mail.logout()

    if apply:
        print(f"\nMarked {total} message(s) read. The queue is empty; "
              f"only mail arriving from now on will be ingested.")
    else:
        print(f"\n{total} message(s) would be marked read. "
              f"Re-run with --apply to do it.")
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually set the read flag (default: report only)")
    args = parser.parse_args()
    mark_all_read(args.apply)
