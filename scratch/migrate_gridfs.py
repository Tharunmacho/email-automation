import email
from app.db.mongo import get_db
from app.storage.gridfs import GridFSStorageBackend
from app.email_client.factory import get_all_email_clients
from app.storage.factory import get_storage_backend

gridfs_storage = GridFSStorageBackend()
db = get_db()
clients = get_all_email_clients()

candidates = list(db["candidates"].find())
print(f"Found {len(candidates)} candidates to check/migrate to GridFS...")

for cand in candidates:
    cid = str(cand["_id"])
    name = cand.get("profile", {}).get("full_name", "Unknown")
    res = cand.get("resume", {}) or {}
    key = res.get("storage_key")
    filename = res.get("original_filename", "resume.pdf")
    mime_type = res.get("mime_type", "application/pdf")

    if not key:
        continue

    if gridfs_storage.exists(key):
        print(f"Candidate {name} ({cid}): File ALREADY in GridFS!")
        db["candidates"].update_one({"_id": cand["_id"]}, {"$set": {"resume.storage_backend": "gridfs"}})
        continue

    print(f"Migrating candidate {name} ({cid}) - searching for file...")
    data = None
    
    # Try local storage load
    try:
        data = get_storage_backend("local").load(key)
        if data:
            print("  Found file on local disk!")
    except Exception:
        pass

    if not data:
        for c in clients:
            if data:
                break
            mail = c._connect_imap()
            for folder in ["INBOX", "INBOX.Resumes.Processed", "Resumes.Processed", "INBOX.Resumes.Deleted"]:
                try:
                    res_sel, _ = mail.select(folder)
                    if res_sel != "OK":
                        continue
                    st, search_data = mail.uid("search", None, "ALL")
                    if st == "OK" and search_data and search_data[0]:
                        for u in search_data[0].split():
                            st_f, fetch_d = mail.uid("fetch", u, "(RFC822)")
                            if st_f == "OK" and fetch_d and fetch_d[0] and isinstance(fetch_d[0], tuple):
                                raw_msg = email.message_from_bytes(fetch_d[0][1])
                                for part in raw_msg.walk():
                                    p_filename = part.get_filename()
                                    if p_filename and (filename.lower() in p_filename.lower() or p_filename.lower() in filename.lower()):
                                        data = part.get_payload(decode=True)
                                        if data:
                                            print(f"  FOUND attachment in folder {folder} UID {u.decode()} len: {len(data)}")
                                            break
                                    if data:
                                        break
                            if data:
                                break
                except Exception as err:
                    print(f"Folder {folder} error: {err}")
            mail.logout()

    if data:
        gridfs_storage.save(key, data, content_type=mime_type)
        db["candidates"].update_one({"_id": cand["_id"]}, {"$set": {"resume.storage_backend": "gridfs"}})
        print(f"SUCCESS! Saved {len(data)} bytes to GridFS for candidate {name}")
    else:
        print(f"Could not find file for candidate {name}")
