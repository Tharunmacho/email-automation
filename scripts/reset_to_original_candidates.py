"""Reset database candidates to the original 10 records present before VPS setup,
and move all Hostinger emails back to INBOX.
"""
from __future__ import annotations

import email
import imaplib, os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings
from app.db.mongo import get_candidates_collection, get_db
from app.email_client.smtp_imap_client import SMTPIMAPClient

# Original 10 candidate IDs to keep in MongoDB
ORIGINAL_KEEP_IDS = {
    "206b42d7c8b14ca2bcae477c2d96cabc",  # BLESSICA JUAN
    "4408fdc74cf542c0b1436dad5b32ae2e",  # Thomas Shelby
    "0451418942ff4e8ca91ca286c40e0292",  # SHA HE ZAMAN S
    "dadec8084bff4acea679be5e485f8823",  # S. THARUN KAMALESH
    "f80ae1f6fec24b0b980d86c1edb212cc",  # Nabeel Noorudheen
    "df8546ffdd294d5784a363bca6ed95fe",  # RIYA SHARMA
    "01674064b1d441669b0958ffea216cdf",  # Vignesh S
    "46fc2bc7868749719f8aa08d571d237f",  # SREENU ERITAM
    "1c75e1b5f5be464cabe0875d164c83f3",  # HEMANT P
    "926cbb7493d842838f485adaec63f0d8",  # THARUN V
}

def reset_db_and_hostinger():
    coll = get_candidates_collection()
    all_records = list(coll.find({}))
    
    records_to_delete = [r for r in all_records if r["_id"] not in ORIGINAL_KEEP_IDS]
    records_to_keep = [r for r in all_records if r["_id"] in ORIGINAL_KEEP_IDS]
    
    print(f"Total candidates currently in DB: {len(all_records)}")
    print(f"Candidates to KEEP: {len(records_to_keep)}")
    print(f"Candidates to REMOVE from DB: {len(records_to_delete)}")
    
    delete_ids = [r["_id"] for r in records_to_delete]
    
    # 1. Move ALL emails from Hostinger processed folder back to INBOX
    print("\n--- Cleaning up Hostinger IMAP Folders ---")
    client = SMTPIMAPClient()
    if settings.imap_server and settings.imap_username:
        mail = client._connect_imap()
        processed_folders = ["INBOX.Resumes.Processed", "Resumes.Processed", "Resumes/Processed"]
        selected_folder = None
        for pf in processed_folders:
            res, _ = mail.select(f'"{pf}"')
            if res == "OK":
                selected_folder = pf
                print(f"[OK] Selected IMAP folder: '{selected_folder}'")
                break
        
        if selected_folder:
            status, data = mail.uid("search", None, "ALL")
            if status == "OK" and data and data[0]:
                uids = data[0].split()
                print(f"Moving {len(uids)} email(s) from '{selected_folder}' back to INBOX...")
                moved_count = 0
                for uid_b in uids:
                    uid = uid_b.decode("utf-8")
                    res_copy, _ = mail.uid("copy", uid, "INBOX")
                    if res_copy == "OK":
                        mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
                        moved_count += 1
                mail.expunge()
                print(f"[DONE] Successfully moved {moved_count} email(s) back to INBOX.")
            else:
                print("[INFO] Processed folder is already empty.")
        mail.logout()
    else:
        print("[WARN] IMAP settings not configured.")

    # 2. Remove records from MongoDB Atlas
    print("\n--- Removing batch records from MongoDB Atlas ---")
    if delete_ids:
        del_res = coll.delete_many({"_id": {"$in": delete_ids}})
        print(f"[DONE] Deleted {del_res.deleted_count} candidate record(s) from MongoDB Atlas.")
    
    # Clear ledger entries for deleted candidates
    db = get_db()
    ledger_coll = db["ingest_ledger"]
    del_ledger = ledger_coll.delete_many({"candidate_id": {"$in": delete_ids}})
    print(f"[DONE] Deleted {del_ledger.deleted_count} ledger entry(ies) from ingest_ledger.")
    
    remaining_count = coll.count_documents({})
    print(f"\nFinal MongoDB Atlas Candidates Count: {remaining_count}")
    print("Kept Candidates:")
    for r in coll.find({}):
        print(f"  - {r.get('profile', {}).get('full_name')} (ID: {r.get('_id')})")

if __name__ == "__main__":
    reset_db_and_hostinger()
