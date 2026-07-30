#!/usr/bin/env python3

import argparse
import hashlib
import json
import mimetypes
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime


# --------------------------------------------------------------------------
# DB setup
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id              TEXT PRIMARY KEY,      -- internal UUID
    message_id      TEXT UNIQUE,           -- RFC Message-ID header (dedup key)
    thread_id       TEXT,                  -- derived from References/In-Reply-To
    subject         TEXT,
    sender_name     TEXT,
    sender_email    TEXT,
    to_addrs        TEXT,                  -- JSON array of {name, email}
    cc_addrs        TEXT,
    bcc_addrs       TEXT,
    date_utc        TEXT,                  -- ISO 8601
    snippet         TEXT,                  -- first ~150 chars of plain text, for list view
    body_text       TEXT,
    body_html       TEXT,
    has_attachments INTEGER DEFAULT 0,
    raw_headers     TEXT,                  -- JSON dict of all headers
    source_file     TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,      -- UUID, also used in stored filename
    email_id        TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    filename        TEXT,
    content_type    TEXT,
    size_bytes      INTEGER,
    storage_path    TEXT,
    content_hash    TEXT,                  -- sha256, used for dedup
    content_id      TEXT,                  -- Content-ID header, for inline cid: images
    is_inline       INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_utc);
CREATE INDEX IF NOT EXISTS idx_attachments_email ON attachments(email_id);
"""


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def addr_list_to_json(header_value):
    """Turn a 'To'/'Cc' header string into a JSON string of [{name, email}, ...]."""
    if not header_value:
        return json.dumps([])
    pairs = getaddresses([header_value])
    return json.dumps([{"name": n, "email": e} for n, e in pairs if e])


def parse_date(msg):
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def derive_thread_id(msg):
    """
    Gmail threads on References/In-Reply-To. Use the first Message-ID in the
    References chain (the root of the thread) if present, otherwise fall back
    to In-Reply-To, otherwise the email is the root of its own thread.
    """
    refs = msg.get("References")
    if refs:
        ids = refs.split()
        if ids:
            return ids[0].strip()
    in_reply_to = msg.get("In-Reply-To")
    if in_reply_to:
        return in_reply_to.strip()
    return msg.get("Message-ID", "").strip() or None


def extract_body(msg):
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))

    def get_content(part):
        if part is None:
            return None
        try:
            return part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace") if payload else None

    return get_content(text_part), get_content(html_part)


def make_snippet(text, html, length=150):
    source = text
    if not source and html:
        import re
        source = re.sub("<[^<]+?>", " ", html)
    if not source:
        return ""
    snippet = " ".join(source.split())
    return snippet[:length]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Core processing
# --------------------------------------------------------------------------

def process_eml_file(filepath, conn, attachments_dir):
    with open(filepath, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        # Not all exported eml files include one; fall back to a stable hash of the file
        with open(filepath, "rb") as f:
            message_id = "sha256:" + sha256_bytes(f.read())

    # Skip re-processing if we've already stored this message
    existing = conn.execute(
        "SELECT id FROM emails WHERE message_id = ?", (message_id,)
    ).fetchone()
    if existing:
        return existing[0], False

    email_id = str(uuid.uuid4())
    from_pairs = getaddresses([msg.get("From", "")])
    sender_name, sender_email = (from_pairs[0] if from_pairs else ("", ""))

    body_text, body_html = extract_body(msg)
    snippet = make_snippet(body_text, body_html)

    raw_headers = {}
    for k, v in msg.items():
        raw_headers.setdefault(k, []).append(v)

    conn.execute(
        """INSERT INTO emails (
            id, message_id, thread_id, subject, sender_name, sender_email,
            to_addrs, cc_addrs, bcc_addrs, date_utc, snippet, body_text, body_html,
            has_attachments, raw_headers, source_file, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            email_id,
            message_id,
            derive_thread_id(msg),
            msg.get("Subject", ""),
            sender_name,
            sender_email,
            addr_list_to_json(msg.get("To")),
            addr_list_to_json(msg.get("Cc")),
            addr_list_to_json(msg.get("Bcc")),
            parse_date(msg),
            snippet,
            body_text,
            body_html,
            0,
            json.dumps(raw_headers),
            os.path.basename(filepath),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    attachment_count = save_attachments(msg, email_id, conn, attachments_dir)
    if attachment_count:
        conn.execute(
            "UPDATE emails SET has_attachments = 1 WHERE id = ?", (email_id,)
        )

    return email_id, True


def save_attachments(msg, email_id, conn, attachments_dir):
    count = 0
    
    for part in msg.iter_attachments():
        payload = part.get_content()
        if isinstance(payload, str):
            payload = payload.encode("utf-8", errors="replace")
        if not payload:
            continue

        original_name = part.get_filename() or f"attachment-{uuid.uuid4().hex[:8]}"
        content_type = part.get_content_type()
        content_id = part.get("Content-ID")
        if content_id:
            content_id = content_id.strip("<>")
        is_inline = 1 if part.get_content_disposition() == "inline" else 0

        attachment_id = str(uuid.uuid4())
        ext = os.path.splitext(original_name)[1] or mimetypes.guess_extension(content_type) or ""
        stored_name = f"{attachment_id}{ext}"
        stored_path = os.path.join(attachments_dir, stored_name)

        with open(stored_path, "wb") as out:
            out.write(payload)

        conn.execute(
            """INSERT INTO attachments (
                id, email_id, filename, content_type, size_bytes,
                storage_path, content_hash, content_id, is_inline
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                attachment_id,
                email_id,
                original_name,
                content_type,
                len(payload),
                stored_path,
                sha256_bytes(payload),
                content_id,
                is_inline,
            ),
        )
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Convert .eml files into a SQLite DB with extracted attachments.")
    parser.add_argument("--eml-dir", required=True, help="Directory containing .eml files")
    parser.add_argument("--db", default="gmail_clone.db", help="Path to SQLite DB file")
    parser.add_argument("--attachments-dir", default="attachments", help="Directory to save extracted attachments")
    args = parser.parse_args()

    if not os.path.isdir(args.eml_dir):
        sys.exit(f"Not a directory: {args.eml_dir}")
    os.makedirs(args.attachments_dir, exist_ok=True)

    conn = get_db(args.db)

    eml_files = [
        os.path.join(args.eml_dir, f)
        for f in sorted(os.listdir(args.eml_dir))
        if f.lower().endswith(".eml")
    ]

    inserted, skipped, failed = 0, 0, 0
    for path in eml_files:
        try:
            _, was_new = process_eml_file(path, conn, args.attachments_dir)
            conn.commit()
            if was_new:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            conn.rollback()
            failed += 1
            print(f"[FAILED] {path}: {e}", file=sys.stderr)

    conn.close()
    print(f"Done. {inserted} new emails inserted, {skipped} already existed, {failed} failed.")
    print(f"DB: {args.db}")
    print(f"Attachments: {args.attachments_dir}")


if __name__ == "__main__":
    main()