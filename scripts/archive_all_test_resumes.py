import imaplib

def archive_test_resumes():
    print("=" * 60)
    print("Archiving Test Resume Emails from INBOX -> Resumes.Processed...")
    print("=" * 60)

    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login("adira.saudi@gmail.com", "ylps icla ughq qfes")
    mail.select("INBOX")

    st, data = mail.uid("search", None, "ALL")
    uids = data[0].split()
    newest_uids = uids[-100:]
    print(f"Scanning newest {len(newest_uids)} UIDs in INBOX...")

    uids_to_archive = []
    for u in newest_uids:
        st, d = mail.uid("fetch", u, "(BODY[HEADER.FIELDS (FROM SUBJECT CONTENT-TYPE)])")
        if d and d[0] and isinstance(d[0], tuple):
            hdr = d[0][1].decode(errors="ignore").lower()
            
            # Skip system emails
            if any(s in hdr for s in [
                "google", "openai", "canva", "security alert",
                "sign-in", "verification", "no-reply", "noreply",
                "updates to", "2-step"
            ]):
                continue

            # Candidate resume email
            if any(k in hdr for k in ["saravanan", "uday", "tharun", "resume", "pdf", "applicant", "multipart"]):
                uids_to_archive.append(u.decode())

    print(f"Found {len(uids_to_archive)} candidate resume email(s) in INBOX:", uids_to_archive)

    if uids_to_archive:
        try:
            mail.create("Resumes.Processed")
        except Exception:
            pass

        for uid in uids_to_archive:
            try:
                res, _ = mail.uid("copy", uid, "Resumes.Processed")
                if res != "OK":
                    try:
                        mail.create("INBOX.Resumes.Processed")
                    except Exception:
                        pass
                    res, _ = mail.uid("copy", uid, "INBOX.Resumes.Processed")
                
                try:
                    mail.uid("store", uid, "-X-GM-LABELS", "\\Inbox")
                    mail.uid("store", uid, "-X-GM-LABELS", "INBOX")
                except Exception:
                    pass
                
                mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
            except Exception as err:
                print(f"  Error on UID {uid}: {err}")

        mail.expunge()
        print(f"Successfully archived {len(uids_to_archive)} candidate email(s) out of INBOX into Resumes.Processed!")

    mail.logout()

if __name__ == "__main__":
    archive_test_resumes()
