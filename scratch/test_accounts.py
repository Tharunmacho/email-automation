import sys
from app.config import Settings
from app.email_client.factory import get_all_email_clients

s = Settings()
print("Parsed accounts count:", len(s.email_accounts))
for a in s.email_accounts:
    print(" Account username:", a.get("imap_username"), "| Provider server:", a.get("imap_server"))

clients = get_all_email_clients()
print("Configured clients count:", len(clients))
