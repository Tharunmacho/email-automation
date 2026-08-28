import os
from app.config import get_settings
from app.email_client.factory import get_all_email_clients

os.environ['EMAIL_ACCOUNTS_JSON'] = '[{"provider":"smtp_imap","imap_server":"imap.gmail.com","imap_port":993,"imap_username":"adira.saudi@gmail.com","imap_password":"ylps icla ughq qfes","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.gmail.com","smtp_port":465,"smtp_username":"adira.saudi@gmail.com","smtp_password":"ylps icla ughq qfes","smtp_use_ssl":true,"smtp_use_tls":false},{"provider":"smtp_imap","imap_server":"imap.hostinger.com","imap_port":993,"imap_username":"cv@adiragroups.com","imap_password":"Cvadira2022@","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.hostinger.com","smtp_port":465,"smtp_username":"cv@adiragroups.com","imap_password":"Cvadira2022@","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.hostinger.com","smtp_port":465,"smtp_username":"cv@adiragroups.com","smtp_password":"Cvadira2022@","smtp_use_ssl":true,"smtp_use_tls":false}]'

clients = get_all_email_clients()
print("Clients count:", len(clients))

for c in clients:
    uname = getattr(c, 'imap_username', '')
    print(f"=== Folders in {uname} ===")
    mail = c._connect_imap()
    for folder in ['Resumes.Processed', 'Resumes.Deleted', 'INBOX']:
        try:
            res, _ = mail.select(f'"{folder}"')
            if res == 'OK':
                st, data = mail.uid('search', None, 'ALL')
                if st == 'OK' and data and data[0]:
                    uids = [u.decode() for u in data[0].split()]
                    print(f" Folder '{folder}' has {len(uids)} UIDs:", uids[:5])
                else:
                    print(f" Folder '{folder}' is empty.")
        except Exception as e:
            print(f" Error in {folder}:", e)
    mail.logout()
