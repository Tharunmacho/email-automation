"""High-speed batch archiver:
Find candidate resume emails in INBOX and move them to Resumes.Processed, removing INBOX label.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email_client import get_all_email_clients
from app.db.repository import CandidateRepository
from app.logging_config import get_logger

log = get_logger("archive_candidate_emails")


def archive_candidate_emails():
    print("=" * 60)
    print("Archiving Candidate Emails from INBOX -> Resumes.Processed...")
    print("=" * 60)

    repo = CandidateRepository()
    clients = get_all_email_clients()

    # Active candidate message IDs in Mongo
    active_mids = set(repo._coll.distinct("source_email.message_id"))
    print(f"Found {len(active_mids)} active candidate message ID(s) in MongoDB.")

    for idx, client in enumerate(clients, 1):
        account_name = getattr(client, "imap_username", getattr(client, "_user_email", f"Account #{idx}"))
        print(f"\n[{idx}/{len(clients)}] Processing Account: {account_name}")

        for mid in active_mids:
            try:
                client.apply_label(mid, "Resumes/Processed")
            except Exception as err:
                log.debug("Could not label candidate message %s: %s", mid, err)

    print("\n" + "=" * 60)
    print("ARCHIVE COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    archive_candidate_emails()
