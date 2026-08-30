import imaplib

def clean_inbox():
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login("adira.saudi@gmail.com", "ylps icla ughq qfes")
    mail.select("INBOX")

    st, data = mail.uid("search", None, "ALL")
    uids = data[0].split()
    newest = uids[-50:]

    candidate_uids = []
    for u in newest:
        st, d = mail.uid("fetch", u, "(BODY[HEADER.FIELDS (FROM SUBJECT)])")
        if d and d[0] and isinstance(d[0], tuple):
            hdr = d[0][1].decode(errors="ignore").lower()
            if any(k in hdr for k in ["saravanan", "uday", "tharun", "resume"]):
                candidate_uids.append(u.decode())

    print("Target Candidate UIDs to archive from INBOX:", candidate_uids)

    if candidate_uids:
        for uid in candidate_uids:
            try:
                # Add Resumes/Processed label
                mail.uid("store", uid, "+X-GM-LABELS", "Resumes/Processed")
                # Remove INBOX label in Gmail
                mail.uid("store", uid, "-X-GM-LABELS", "\\Inbox")
                mail.uid("store", uid, "-X-GM-LABELS", "INBOX")
                # Expunge from INBOX
                mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
            except Exception as e:
                print(f"Error on UID {uid}: {e}")
        mail.expunge()
        print("ARCHIVED candidate emails out of INBOX into Resumes/Processed successfully!")

    mail.logout()

if __name__ == "__main__":
    clean_inbox()
