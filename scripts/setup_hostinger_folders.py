import imaplib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings


def setup_hostinger_folders():
    print("=" * 60)
    print("Setting up clean IMAP folders on Hostinger (cv@adiragroups.com)...")
    print("=" * 60)

    try:
        host = getattr(settings, "imap_server", "imap.hostinger.com")
        port = getattr(settings, "imap_port", 993)
        user = getattr(settings, "imap_username", "cv@adiragroups.com")
        pwd = getattr(settings, "imap_password", "Cvadira2022@")

        mail = imaplib.IMAP4_SSL(host, port)
        mail.login(user, pwd)

        folders_to_create = [
            "Resumes",
            "Resumes/Processed",
            "Resumes/Deleted",
            "Resumes.Processed",
            "Resumes.Deleted",
            "INBOX.Resumes",
            "INBOX.Resumes.Processed",
            "INBOX.Resumes.Deleted",
        ]

        for f in folders_to_create:
            try:
                res, msg = mail.create(f)
                print(f"  Creating folder '{f}': {res}")
            except Exception as e:
                print(f"  Folder '{f}' note: {e}")

        st, mailboxes = mail.list()
        if st == "OK" and mailboxes:
            print("\nCurrent Hostinger Mailboxes:")
            for mb in mailboxes:
                print("  ", mb.decode(errors="ignore"))

        mail.logout()
    except Exception as err:
        print(f"Error connecting to Hostinger IMAP: {err}")

    print("\n" + "=" * 60)
    print("HOSTINGER FOLDER SETUP COMPLETE!")
    print("=" * 60)


if __name__ == "__main__":
    setup_hostinger_folders()
