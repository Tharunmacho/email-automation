import os
from app.config import Settings
from app.email_client.factory import get_all_email_clients

os.environ["EMAIL_ACCOUNTS_JSON"] = '[{"provider":"smtp_imap","imap_server":"imap.gmail.com","imap_port":993,"imap_username":"adira.saudi@gmail.com","imap_password":"ylps icla ughq qfes","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.gmail.com","smtp_port":465,"smtp_username":"adira.saudi@gmail.com","smtp_password":"ylps icla ughq qfes","smtp_use_ssl":true,"smtp_use_tls":false},{"provider":"smtp_imap","imap_server":"imap.hostinger.com","imap_port":993,"imap_username":"cv@adiragroups.com","imap_password":"Cvadira2022@","imap_use_ssl":true,"imap_folder":"INBOX","smtp_server":"smtp.hostinger.com","smtp_port":465,"smtp_username":"cv@adiragroups.com","smtp_password":"Cvadira2022@","smtp_use_ssl":true,"smtp_use_tls":false}]'

s = Settings()
print("Parsed accounts count:", len(s.email_accounts))
for a in s.email_accounts:
    print(" Account username:", a.get("imap_username"))

clients = get_all_email_clients()
print("Configured clients count:", len(clients))
