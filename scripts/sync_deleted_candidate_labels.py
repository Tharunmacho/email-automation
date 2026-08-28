"""Sync Gmail candidate email labels:
If a candidate was deleted in MongoDB, remove Resumes/Processed label and move to Resumes/Deleted!
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.email_client import get_all_email_clients
from app.db.repository import CandidateRepository


def sync_labels():
    print("=" * 60)
    print("Syncing Gmail candidate email labels with MongoDB...")
    print("=" * 60)

    repo = CandidateRepository()
    clients = get_all_email_clients()

    active_mids = set(repo._coll.distinct("source_email.message_id"))
    print(f"Active candidates in MongoDB: {len(active_mids)}")

    for idx, client in enumerate(clients, 1):
        account_name = getattr(client, "imap_username", getattr(client, "_user_email", f"Account #{idx}"))
        print(f"\n[{idx}/{len(clients)}] Checking Account: {account_name}")

        if hasattr(client, "_connect_imap"):
            try:
                mail = client._connect_imap()
                try:
                    mail.create("Resumes.Deleted")
                except Exception:
                    pass

                for proc_folder in ["Resumes.Processed", "INBOX.Resumes.Processed"]:
                    st, _ = mail.select(proc_folder)
                    if st != "OK":
                        continue

                    st_s, search_d = mail.uid("search", None, "ALL")
                    if st_s == "OK" and search_d and search_d[0]:
                        uids = [u.decode() for u in search_d[0].split() if u.decode()]
                        print(f"  Folder '{proc_folder}' has {len(uids)} message(s)")

                        for uid in uids:
                            try:
                                mail.uid("store", uid, "-X-GM-LABELS", "Resumes/Processed")
                                mail.uid("store", uid, "-X-GM-LABELS", "Resumes.Processed")
                                mail.uid("store", uid, "-X-GM-LABELS", proc_folder)
                                mail.uid("store", uid, "+X-GM-LABELS", "Resumes/Deleted")
                                mail.uid("store", uid, "+X-GM-LABELS", "Resumes.Deleted")
                                mail.uid("copy", uid, "Resumes.Deleted")
                                mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
                            except Exception as err:
                                print(f"    Error processing UID {uid}: {err}")

                        mail.expunge()
                        print(f"  Cleaned {len(uids)} deleted candidate message(s) out of '{proc_folder}' into 'Resumes.Deleted'!")

                mail.logout()
            except Exception as err:
                print(f"  IMAP error: {err}")

    print("\n" + "=" * 60)
    print("SYNC COMPLETE! Refresh your Gmail tab now.")
    print("=" * 60)


if __name__ == "__main__":
    sync_labels()
