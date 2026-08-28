import os
from app.email_client.factory import get_all_email_clients

os.environ['EMAIL_ACCOUNTS_JSON'] = '[{"provider":"smtp_imap","imap_server":"imap.gmail.com","imap_port":993,"imap_username":"adira.saudi@gmail.com","imap_password":"ylps icla ughq qfes","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.gmail.com","smtp_port":465,"smtp_username":"adira.saudi@gmail.com","smtp_password":"ylps icla ughq qfes","smtp_use_ssl":true,"smtp_use_tls":false},{"provider":"smtp_imap","imap_server":"imap.hostinger.com","imap_port":993,"imap_username":"cv@adiragroups.com","imap_password":"Cvadira2022@","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.hostinger.com","smtp_port":465,"smtp_username":"cv@adiragroups.com","smtp_password":"Cvadira2022@","smtp_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.hostinger.com","smtp_port":465,"smtp_username":"cv@adiragroups.com","smtp_password":"Cvadira2022@","smtp_use_ssl":true,"smtp_use_tls":false}]'

clients = get_all_email_clients()
for c in clients:
    uname = getattr(c, 'imap_username', '')
    print(f"=== Folders in {uname} ===")
    mail = c._connect_imap()
    status, folders = mail.list()
    for f in folders:
        print("  ", f.decode('utf-8'))
    mail.logout()
