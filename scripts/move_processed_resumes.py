import imaplib

client = imaplib.IMAP4_SSL("imap.gmail.com", 993)
client.login("adira.saudi@gmail.com", "ylps icla ughq qfes")
client.select("INBOX")

st, data = client.uid("search", None, "ALL")
uids = data[0].split()
newest_uids = uids[-100:]

candidate_uids = []
for u in newest_uids:
    st, d = client.uid("fetch", u, "(BODY[HEADER.FIELDS (FROM SUBJECT)])")
    if d and d[0] and isinstance(d[0], tuple):
        hdr = d[0][1].decode(errors="ignore").lower()
        if any(k in hdr for k in ["resume", "saravanan", "uday", "tharun"]):
            candidate_uids.append(u.decode())

print("Found candidate email UIDs in INBOX:", candidate_uids)
if candidate_uids:
    uid_set = ",".join(candidate_uids)
    client.create("Resumes.Processed")
    st, _ = client.uid("copy", uid_set, "Resumes.Processed")
    if st != "OK":
        client.create("INBOX.Resumes.Processed")
        st, _ = client.uid("copy", uid_set, "INBOX.Resumes.Processed")
    print("Copy result:", st)
    if st == "OK":
        client.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
        client.expunge()
        print(f"Moved {len(candidate_uids)} candidate email(s) from INBOX to Resumes.Processed!")

client.logout()
