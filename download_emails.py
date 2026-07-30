import os
import email
from pathlib import Path

import imaplib
from dotenv import load_dotenv

# Load .env
load_dotenv(Path(__file__).parent / ".env")

EMAIL = os.getenv("GMAIL_EMAIL")
PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

if not EMAIL or not PASSWORD:
    raise RuntimeError(
        "Missing GMAIL_EMAIL or GMAIL_APP_PASSWORD in .env"
    )

IMAP_HOST = "imap.gmail.com"
OUTPUT_DIR = Path("emails")
OUTPUT_DIR.mkdir(exist_ok=True)

# Connect
mail = imaplib.IMAP4_SSL(IMAP_HOST)
mail.login(EMAIL, PASSWORD)
mail.select("INBOX")

# Get all email IDs
status, data = mail.search(None, "ALL")

if status != "OK":
    raise RuntimeError("Failed to search mailbox")

ids = data[0].split()

# Latest 500 emails
latest = ids[-500:]

print(f"Downloading {len(latest)} emails...")

for i, msg_id in enumerate(reversed(latest), start=1):
    status, msg_data = mail.fetch(msg_id, "(RFC822)")

    if status != "OK":
        print(f"Skipping email {msg_id.decode()}")
        continue

    raw = msg_data[0][1]
    message = email.message_from_bytes(raw)

    subject = message.get("Subject", "")
    sender = message.get("From", "")
    date = message.get("Date", "")

    print(f"[{i}/{len(latest)}] {subject}")

    filename = OUTPUT_DIR / f"{msg_id.decode()}.eml"

    with open(filename, "wb") as f:
        f.write(raw)

mail.logout()

print("Done.")