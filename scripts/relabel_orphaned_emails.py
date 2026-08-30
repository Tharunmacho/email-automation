"""Batch reconciliation script to clean up orphaned/deleted candidate emails
across Gmail and Hostinger IMAP accounts in seconds using persistent connections.

Run:
    python scripts/relabel_orphaned_emails.py
"""
from __future__ import annotations

import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.db.ledger import IngestLedger
from app.db.repository import CandidateRepository
from app.email_client import get_all_email_clients
from app.logging_config import get_logger

log = get_logger("reconcile_labels")


def reconcile_labels() -> None:
    print("=" * 60)
    print("Starting High-Speed Batch Email Label Reconciliation...")
    print("=" * 60)

    repo = CandidateRepository()
    ledger = IngestLedger()
    clients = get_all_email_clients()

    processed_label = settings.gmail_processed_label or "Resumes/Processed"
    deleted_label = settings.gmail_deleted_label or "Resumes/Deleted"

    total_audited = 0
    total_relabelled = 0
    total_active = 0

    # Pre-fetch all active candidate message_ids in 1 single MongoDB query
    active_mids = set(repo._coll.distinct("source_email.message_id"))
    ledger_active_mids = set(ledger._coll.distinct("message_id", {"suppressed": False}))
    all_active = active_mids.union(ledger_active_mids)

    for idx, client in enumerate(clients, 1):
        account_name = getattr(client, "imap_username", getattr(client, "_user_email", f"Account #{idx}"))
        print(f"\n[{idx}/{len(clients)}] Checking Account: {account_name}")

        if hasattr(client, "_connect_imap"):
            try:
                mail = client._connect_imap()
                try:
                    target_folder = deleted_label.replace("/", ".")
                    inbox_target = f"INBOX.{target_folder}"

                    for tf in [target_folder, inbox_target]:
                        try:
                            mail.create(tf)
                        except Exception:
                            pass

                    # Only check processed folders (or INBOX if processed messages sit in INBOX)
                    source_folders = ["Resumes.Processed", "INBOX.Resumes.Processed", "Resumes/Processed"]
                    to_relabel_by_folder: dict[str, list[str]] = {}

                    for src in source_folders:
                        try:
                            st, _ = mail.select(src)
                            if st != "OK":
                                continue
                            st_s, search_d = mail.uid("search", None, "ALL")
                            if st_s == "OK" and search_d and search_d[0]:
                                uids = [u.decode() for u in search_d[0].split() if u.decode()]
                                for uid in uids:
                                    total_audited += 1
                                    if uid in all_active:
                                        total_active += 1
                                    else:
                                        to_relabel_by_folder.setdefault(src, []).append(uid)
                        except Exception as err:
                            log.debug("IMAP folder check %s failed: %s", src, err)

                    for src, uids in to_relabel_by_folder.items():
                        if not uids:
                            continue
                        mail.select(src)
                        uid_set = ",".join(uids)
                        res, _ = mail.uid("copy", uid_set, target_folder)
                        if res != "OK":
                            res, _ = mail.uid("copy", uid_set, inbox_target)
                        if res == "OK":
                            mail.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
                            mail.expunge()
                            total_relabelled += len(uids)
                            print(f"  BATCH SUCCESS: Relabeled {len(uids)} message(s) from '{src}' to '{target_folder}'")
                        else:
                            for uid in uids:
                                try:
                                    client.apply_label(uid, deleted_label)
                                    client.remove_label(uid, processed_label)
                                    total_relabelled += 1
                                except Exception:
                                    pass
                finally:
                    try:
                        mail.logout()
                    except Exception:
                        pass
            except Exception as err:
                print(f"  Error on IMAP account {account_name}: {err}")

        else:
            try:
                query = f"label:\"{processed_label}\""
                mids = client.search_message_ids(query=query)
                print(f"  Found {len(mids)} message(s) on Gmail API account {account_name}")
                for mid in mids:
                    total_audited += 1
                    active = repo._coll.find_one({"source_email.message_id": mid})
                    if active:
                        total_active += 1
                    else:
                        try:
                            client.apply_label(mid, deleted_label)
                            client.remove_label(mid, processed_label)
                            total_relabelled += 1
                            print(f"  Relabeled Gmail message {mid} to '{deleted_label}'")
                        except Exception as err:
                            print(f"  Failed to relabel message {mid}: {err}")
            except Exception as err:
                print(f"  Error on Gmail account {account_name}: {err}")

    print("\n" + "=" * 60)
    print("HIGH-SPEED RECONCILIATION SUMMARY")
    print(f"  Total Messages Audited:            {total_audited}")
    print(f"  Active Candidates (Preserved):     {total_active}")
    print(f"  Deleted/Orphaned Emails Relabeled: {total_relabelled}")
    print("=" * 60)


if __name__ == "__main__":
    reconcile_labels()
