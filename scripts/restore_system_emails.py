"""Restore all emails from Resumes.Deleted back to INBOX, keeping inbox 100% restored.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email_client import get_all_email_clients
from app.db.repository import CandidateRepository
from app.logging_config import get_logger

log = get_logger("restore_all_emails")


def restore_all_emails() -> None:
    print("=" * 60)
    print("Restoring emails from Resumes.Deleted back to INBOX...")
    print("=" * 60)

    repo = CandidateRepository()
    clients = get_all_email_clients()

    total_restored = 0

    for idx, client in enumerate(clients, 1):
        account_name = getattr(client, "imap_username", getattr(client, "_user_email", f"Account #{idx}"))
        print(f"\n[{idx}/{len(clients)}] Processing Account: {account_name}")

        if hasattr(client, "_connect_imap"):
            try:
                mail = client._connect_imap()
                deleted_folders = ["Resumes.Deleted", "INBOX.Resumes.Deleted", "Resumes/Deleted"]
                for df in deleted_folders:
                    try:
                        st, _ = mail.select(df)
                        if st != "OK":
                            continue
                        st_s, search_d = mail.uid("search", None, "ALL")
                        if st_s == "OK" and search_d and search_d[0]:
                            uids = [u.decode() for u in search_d[0].split() if u.decode()]
                            if uids:
                                uid_set = ",".join(uids)
                                res, _ = mail.uid("copy", uid_set, "INBOX")
                                if res == "OK":
                                    mail.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
                                    mail.expunge()
                                    total_restored += len(uids)
                                    print(f"  BATCH RESTORED: Moved {len(uids)} message(s) from '{df}' back to 'INBOX'")
                    except Exception as err:
                        print(f"  Error on folder {df}: {err}")
                mail.logout()
            except Exception as err:
                print(f"  IMAP error on {account_name}: {err}")

    print("\n" + "=" * 60)
    print(f"Total Emails Restored Back to INBOX: {total_restored}")
    print("=" * 60)


if __name__ == "__main__":
    restore_all_emails()
