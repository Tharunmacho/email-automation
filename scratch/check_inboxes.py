import os
from app.db.mongo import get_db
from app.email_client.factory import get_all_email_clients
from app.ingestion.detector import detect

os.environ['EMAIL_ACCOUNTS_JSON'] = '[{"provider":"smtp_imap","imap_server":"imap.gmail.com","imap_port":993,"imap_username":"adira.saudi@gmail.com","imap_password":"ylps icla ughq qfes","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.gmail.com","smtp_port":465,"smtp_username":"adira.saudi@gmail.com","smtp_password":"ylps icla ughq qfes","smtp_use_ssl":true,"smtp_use_tls":false},{"provider":"smtp_imap","imap_server":"imap.hostinger.com","imap_port":993,"imap_username":"cv@adiragroups.com","imap_password":"Cvadira2022@","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.hostinger.com","smtp_port":465,"smtp_username":"cv@adiragroups.com","imap_password":"Cvadira2022@","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.hostinger.com","smtp_port":465,"smtp_username":"cv@adiragroups.com","smtp_password":"Cvadira2022@","smtp_use_ssl":true,"smtp_use_tls":false}]'

db = get_db()
clients = get_all_email_clients()

print("=== INSPECTING INBOXES FOR UN-INGESTED CANDIDATES ===")

for c in clients:
    uname = getattr(c, 'imap_username', '')
    print(f"\n--- Account: {uname} ---")
    
    # Get uids from search_message_ids
    uids = c.search_message_ids(max_results=50)
    print(f" Account '{uname}': Found {len(uids)} total candidate search message UIDs.")
    
    for u in uids:
        try:
            msg = c.get_message(u)
            if msg and msg.attachments:
                att_names = [a.filename for a in msg.attachments if a.filename]
                det_res = detect(msg)
                
                match_cand = db['candidates'].find_one({'$or': [
                    {'source_email.message_id': u},
                    {'source_email.from_addr': msg.from_addr}
                ]})
                cand_name = match_cand.get('profile', {}).get('full_name') if match_cand else None
                
                status = "✅ INGESTED" if cand_name else "⚠️ NOT INGESTED IN DB"
                print(f"  [{status}] UID {u} | From: {msg.from_addr} | Subject: {msg.subject} | Score: {det_res.score:.2f} | Attachments: {att_names} | Candidate: {cand_name}")
        except Exception as err:
            print(f"  UID {u} error: {err}")
