"""Delete unwanted duplicate Gmail labels via Gmail API and IMAP.
Leaves ONLY:
- Resumes/Processed
- Resumes/Deleted
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import imaplib


def delete_extra_labels():
    print("=" * 60)
    print("Deleting extra duplicate Gmail labels...")
    print("=" * 60)

    # 1. Try Gmail API if available
    try:
        from app.email_client.gmail_client import GmailClient
        gclient = GmailClient()
        if hasattr(gclient, "service") and gclient.service:
            results = gclient.service.users().labels().list(userId="me").execute()
            labels = results.get("labels", [])
            print(f"Found {len(labels)} labels in Gmail API")

            target_to_delete = ["INBOX.Resumes.Deleted", "INBOX.Resumes.Processed", "Resumes.Deleted", "Resumes.Processed"]
            for label in labels:
                name = label.get("name")
                label_id = label.get("id")
                if name in target_to_delete:
                    try:
                        gclient.service.users().labels().delete(userId="me", id=label_id).execute()
                        print(f"  DELETED label '{name}' (ID: {label_id}) via Gmail API!")
                    except Exception as err:
                        print(f"  Could not delete label '{name}' via Gmail API: {err}")
    except Exception as err:
        print(f"Gmail API label check note: {err}")

    # 2. Delete via IMAP
    from app.email_client import get_all_email_clients

    clients = get_all_email_clients()
    for idx, client in enumerate(clients, 1):
        account_name = getattr(client, "imap_username", getattr(client, "_user_email", f"Account #{idx}"))
        print(f"\nProcessing IMAP Account: {account_name}")
        if hasattr(client, "_connect_imap"):
            try:
                mail = client._connect_imap()
                st, mailboxes = mail.list()
                if st == "OK" and mailboxes:
                    for mb in mailboxes:
                        mb_str = mb.decode(errors="ignore")
                        for extra in ["INBOX.Resumes.Deleted", "INBOX.Resumes.Processed", "Resumes.Deleted", "Resumes.Processed"]:
                            if f'"{extra}"' in mb_str or f" {extra}" in mb_str:
                                try:
                                    mail.delete(extra)
                                    print(f"  SUCCESS: Deleted IMAP label '{extra}'")
                                except Exception as e:
                                    print(f"  Failed to delete '{extra}': {e}")
                mail.logout()
            except Exception as err:
                print(f"  IMAP error on {account_name}: {err}")

    print("\n" + "=" * 60)
    print("LABEL CLEANUP COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    delete_extra_labels()
