import imaplib

def cleanup_labels_and_usman():
    print("=" * 60)
    print("1. Removing Processed label from Usman and applying Deleted label...")
    print("2. Cleaning up duplicate IMAP folders so only 2 labels exist...")
    print("=" * 60)

    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login("adira.saudi@gmail.com", "ylps icla ughq qfes")

    # Step 1: Remove Processed label from Usman across folders
    folders_to_check = ["INBOX", "Resumes.Processed", "INBOX.Resumes.Processed", "Resumes/Processed", "[Gmail]/All Mail"]
    for folder in folders_to_check:
        try:
            st, _ = mail.select(folder)
            if st != "OK":
                continue
            st_s, data = mail.uid("search", None, "ALL")
            if st_s == "OK" and data and data[0]:
                uids = [u.decode() for u in data[0].split() if u.decode()]
                for u in uids:
                    try:
                        st_f, d = mail.uid("fetch", u, "(BODY[HEADER.FIELDS (SUBJECT FROM)])")
                        if d and d[0] and isinstance(d[0], tuple):
                            hdr = d[0][1].decode(errors="ignore").lower()
                            if "usman" in hdr or "muhammad usman" in hdr:
                                mail.uid("store", u, "-X-GM-LABELS", "Resumes/Processed")
                                mail.uid("store", u, "-X-GM-LABELS", "Resumes.Processed")
                                mail.uid("store", u, "-X-GM-LABELS", "INBOX.Resumes.Processed")
                                mail.uid("store", u, "+X-GM-LABELS", "Resumes/Deleted")
                                print(f"  Fixed Usman email UID {u} in '{folder}': Removed Processed label, applied Deleted label.")
                    except Exception as err:
                        print(f"  Error on UID {u}: {err}")
        except Exception as err:
            print(f"  Error checking folder {folder}: {err}")

    # Step 2: Ensure 2 main labels exist
    for main_label in ["Resumes/Processed", "Resumes/Deleted"]:
        try:
            mail.create(main_label)
        except Exception:
            pass

    # Step 3: Remove redundant extra dot-folders
    extra_folders = ["INBOX.Resumes.Deleted", "INBOX.Resumes.Processed", "Resumes.Deleted", "Resumes.Processed"]
    for ef in extra_folders:
        try:
            mail.delete(ef)
            print(f"  Deleted extra redundant label: '{ef}'")
        except Exception as err:
            print(f"  Folder '{ef}' already deleted or could not be removed: {err}")

    mail.logout()
    print("\n" + "=" * 60)
    print("CLEANUP COMPLETE! Refresh Gmail tab to see ONLY 2 labels.")
    print("=" * 60)

if __name__ == "__main__":
    cleanup_labels_and_usman()
