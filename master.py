#!/usr/bin/env python3

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

import imaplib

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_args, **_kwargs):
        return False


IMAP_HOST = "imap.gmail.com"

GMAIL_CATEGORIES = ["primary", "social", "promotions", "updates", "forums"]

GMAIL_VIRTUAL_BUCKETS = ["scheduled", "snoozed"]

SYSTEM_LABEL_MAP = {
    r"\inbox": "inbox",
    r"\sent": "sent",
    r"\draft": "draft",
    r"\drafts": "draft",
    r"\important": "important",
    r"\starred": "starred",
    r"\flagged": "starred",
    r"\junk": "spam",
    r"\spam": "spam",
    r"\trash": "trash",
    r"\all": "all_mail",
    r"\allmail": "all_mail",
}

# Chunk size for batched IMAP FETCH commands.
FETCH_CHUNK = 50


# =============================================================================
# Database
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id              TEXT PRIMARY KEY,      -- internal UUID
    gmail_msgid     TEXT UNIQUE,           -- Gmail's stable X-GM-MSGID (dedup key)
    message_id      TEXT,                  -- RFC Message-ID header
    thread_id       TEXT,                  -- derived from References/In-Reply-To
    subject         TEXT,
    sender_name     TEXT,
    sender_email    TEXT,
    to_addrs        TEXT,                  -- JSON array of {name, email}
    cc_addrs        TEXT,
    bcc_addrs       TEXT,
    date_utc        TEXT,                  -- ISO 8601
    category        TEXT,                  -- the single inbox tab: primary/social/... (nullable)
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

-- Normalized tags: an email can belong to many buckets at once
-- (e.g. inbox + primary + important + starred + a user label).
CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT UNIQUE NOT NULL,          -- 'sent', 'primary', 'Work/Clients', ...
    kind    TEXT NOT NULL                  -- 'category' | 'system' | 'user' | 'virtual'
);

CREATE TABLE IF NOT EXISTS email_tags (
    email_id    TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (email_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date_utc);
CREATE INDEX IF NOT EXISTS idx_emails_category ON emails(category);
CREATE INDEX IF NOT EXISTS idx_attachments_email ON attachments(email_id);
CREATE INDEX IF NOT EXISTS idx_email_tags_tag ON email_tags(tag_id);
"""


def get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def get_or_create_tag(conn, name, kind):
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO tags (name, kind) VALUES (?, ?)", (name, kind))
    return cur.lastrowid


def link_tag(conn, email_id, tag_name, kind):
    if not tag_name:
        return
    tag_id = get_or_create_tag(conn, tag_name, kind)
    conn.execute(
        "INSERT OR IGNORE INTO email_tags (email_id, tag_id) VALUES (?, ?)",
        (email_id, tag_id),
    )


# =============================================================================
# Parsing helpers (email content -> DB columns)
# =============================================================================

def addr_list_to_json(header_value):
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
    refs = msg.get("References")
    if refs:
        ids = refs.split()
        if ids:
            return ids[0].strip()
    in_reply_to = msg.get("In-Reply-To")
    if in_reply_to:
        return in_reply_to.strip()
    return (msg.get("Message-ID") or "").strip() or None


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
            if not payload:
                return None
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")

    return get_content(text_part), get_content(html_part)


def make_snippet(text, html, length=150):
    source = text
    if not source and html:
        source = re.sub("<[^<]+?>", " ", html)
    if not source:
        return ""
    return " ".join(source.split())[:length]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# =============================================================================
# IMAP helpers
# =============================================================================

# LIST line looks like:  (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+"?[^"]*"?\s+(?P<name>.+)$')


def imap_connect(email_addr, password):
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(email_addr, password)
    return mail


def decode_mailbox_name(raw_name):
    
    name = raw_name.strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    try:
        return name.encode("ascii").decode("ascii")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return name


def list_mailboxes(mail):
    status, data = mail.list()
    if status != "OK":
        raise RuntimeError("LIST command failed")

    mailboxes = []
    for line in data:
        if line is None:
            continue
        if isinstance(line, tuple):
            line = line[0]
        m = _LIST_RE.match(line)
        if not m:
            continue

        flags = m.group("flags").decode(errors="replace").split()
        name = decode_mailbox_name(m.group("name").decode(errors="replace"))

        tag = None
        for f in flags:
            key = f.lower().replace(" ", "")
            if key in SYSTEM_LABEL_MAP:
                tag = SYSTEM_LABEL_MAP[key]
                break
        if tag is None and name.upper() == "INBOX":
            tag = "inbox"

        mailboxes.append({"name": name, "flags": flags, "tag": tag})
    return mailboxes


def choose_source_mailboxes(mailboxes):
    by_tag = {}
    for mb in mailboxes:
        if mb["tag"]:
            by_tag.setdefault(mb["tag"], mb)

    if "all_mail" in by_tag:
        sources = [by_tag["all_mail"]]
        for extra in ("spam", "trash"):
            if extra in by_tag:
                sources.append(by_tag[extra])
        return sources, "all_mail"

    selectable = [
        mb for mb in mailboxes
        if "\\Noselect" not in mb["flags"] and mb["name"].upper() != "[GMAIL]"
    ]
    return selectable, "fallback"


# X-GM-LABELS payload parsing -------------------------------------------------

def _tokenize_labels(blob):
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', blob)
    result = []
    for quoted, bare in tokens:
        val = quoted if quoted else bare
        val = val.replace('\\"', '"')
        if val:
            result.append(val)
    return result


def parse_fetch_metadata(prefix_bytes):
    text = prefix_bytes.decode(errors="replace")

    msgid = None
    m = re.search(r"X-GM-MSGID\s+(\d+)", text)
    if m:
        msgid = m.group(1)

    labels = []
    m = re.search(r"X-GM-LABELS\s+\(", text)
    if m:
        start = m.end() - 1
        depth = 0
        end = start
        for i in range(start, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        inner = text[start + 1:end]
        labels = _tokenize_labels(inner)
    return msgid, labels


def normalize_label(label):
    if label.startswith("\\"):
        key = label.lower().replace(" ", "")
        return SYSTEM_LABEL_MAP.get(key, label.lstrip("\\").lower()), "system"
    return label, "user"


def fetch_uids_all(mail, mailbox):
    status, _ = mail.select(f'"{mailbox}"', readonly=True)
    if status != "OK":
        return None
    status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or data[0] is None:
        return []
    return data[0].split()


def fetch_metadata_batch(mail, uid_chunk):
    uid_set = b",".join(uid_chunk)
    status, data = mail.uid("fetch", uid_set, "(X-GM-MSGID X-GM-LABELS)")
    out = {}
    if status != "OK" or not data:
        return out
    for item in data:
        if not isinstance(item, (bytes, bytearray)):
            item = item[0] if isinstance(item, tuple) else b""
        m = re.match(rb"(\d+)\s+\(", item)
        if not m:
            continue
        uid = m.group(1)
        msgid, labels = parse_fetch_metadata(item)
        out[uid] = (msgid, labels)
    return out


def fetch_rfc822(mail, uid):
    status, data = mail.uid("fetch", uid, "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) == 2 and item[1]:
            return item[1]
    return None


def search_msgids_for_query(mail, mailbox, raw_query):
    status, _ = mail.select(f'"{mailbox}"', readonly=True)
    if status != "OK":
        return set()
    try:
        status, data = mail.uid("search", None, "X-GM-RAW", f'"{raw_query}"')
    except imaplib.IMAP4.error:
        return set()
    if status != "OK" or not data or data[0] is None:
        return set()
    uids = data[0].split()
    if not uids:
        return set()

    msgids = set()
    for i in range(0, len(uids), FETCH_CHUNK):
        chunk = uids[i:i + FETCH_CHUNK]
        meta = fetch_metadata_batch(mail, chunk)
        for msgid, _labels in meta.values():
            if msgid:
                msgids.add(msgid)
    return msgids


# =============================================================================
# Download phase
# =============================================================================

def download_all(mail, output_dir, limit_per_mailbox):
    output_dir.mkdir(exist_ok=True)

    mailboxes = list_mailboxes(mail)
    sources, strategy = choose_source_mailboxes(mailboxes)

    print("Discovered mailboxes:")
    for mb in mailboxes:
        marker = f" -> tag '{mb['tag']}'" if mb["tag"] else ""
        print(f"  - {mb['name']}{marker}")
    print(f"\nCoverage strategy: {strategy} "
          f"(reading from {', '.join(mb['name'] for mb in sources)})\n")

    msgid_to_labels = {}
    msgid_to_file = {}

    for mb in sources:
        uids = fetch_uids_all(mail, mb["name"])
        if uids is None:
            print(f"  [skip] cannot select {mb['name']}")
            continue
        if limit_per_mailbox:
            uids = uids[-limit_per_mailbox:]
        print(f"  {mb['name']}: {len(uids)} messages")

        for i in range(0, len(uids), FETCH_CHUNK):
            chunk = uids[i:i + FETCH_CHUNK]
            meta = fetch_metadata_batch(mail, chunk)

            for uid in chunk:
                msgid, labels = meta.get(uid, (None, []))
                if not msgid:
                    msgid = f"uid-{mb['name']}-{uid.decode(errors='replace')}"

                tagset = msgid_to_labels.setdefault(msgid, set())
                for lbl in labels:
                    tagset.add(normalize_label(lbl))
                
                if mb["tag"]:
                    tagset.add((mb["tag"], "system"))

                if msgid in msgid_to_file:
                    continue
                raw = fetch_rfc822(mail, uid)
                if raw is None:
                    continue
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", msgid)
                path = output_dir / f"{safe}.eml"
                with open(path, "wb") as f:
                    f.write(raw)
                msgid_to_file[msgid] = path

    print(f"\nDownloaded {len(msgid_to_file)} unique messages.\n")
    return msgid_to_labels, msgid_to_file


def collect_category_map(mail, mailboxes):
    inbox = next((mb["name"] for mb in mailboxes if mb["tag"] == "inbox"), "INBOX")

    category_map = {}
    print("Resolving inbox category tabs via X-GM-RAW search:")
    for cat in GMAIL_CATEGORIES:
        msgids = search_msgids_for_query(mail, inbox, f"category:{cat}")
        for mid in msgids:
            category_map[mid] = cat
        print(f"  category:{cat}: {len(msgids)} messages")

    all_mail = next((mb["name"] for mb in mailboxes if mb["tag"] == "all_mail"), inbox)
    virtual_map = {}
    print("\nResolving virtual buckets (best-effort):")
    for bucket in GMAIL_VIRTUAL_BUCKETS:
        msgids = search_msgids_for_query(mail, all_mail, f"in:{bucket}")
        for mid in msgids:
            virtual_map.setdefault(mid, set()).add(bucket)
        note = "" if msgids else "  (none / not exposed by this account)"
        print(f"  in:{bucket}: {len(msgids)} messages{note}")

    print("\nNote: 'outbox' is a client-side queue and is not accessible over "
          "IMAP, so it cannot be captured here.\n")
    return category_map, virtual_map


# =============================================================================
# DB build phase
# =============================================================================

def insert_email(conn, filepath, gmail_msgid, category, attachments_dir):
    with open(filepath, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    message_id = (msg.get("Message-ID") or "").strip()

    existing = conn.execute(
        "SELECT id FROM emails WHERE gmail_msgid = ?", (gmail_msgid,)
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
            id, gmail_msgid, message_id, thread_id, subject, sender_name, sender_email,
            to_addrs, cc_addrs, bcc_addrs, date_utc, category, snippet, body_text, body_html,
            has_attachments, raw_headers, source_file, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            email_id,
            gmail_msgid,
            message_id,
            derive_thread_id(msg),
            msg.get("Subject", ""),
            sender_name,
            sender_email,
            addr_list_to_json(msg.get("To")),
            addr_list_to_json(msg.get("Cc")),
            addr_list_to_json(msg.get("Bcc")),
            parse_date(msg),
            category,
            snippet,
            body_text,
            body_html,
            0,
            json.dumps(raw_headers),
            os.path.basename(filepath),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    if save_attachments(msg, email_id, conn, attachments_dir):
        conn.execute("UPDATE emails SET has_attachments = 1 WHERE id = ?", (email_id,))

    return email_id, True


def save_attachments(msg, email_id, conn, attachments_dir):
    count = 0
    for part in msg.iter_attachments():
        try:
            payload = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
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
        stored_path = os.path.join(attachments_dir, f"{attachment_id}{ext}")

        with open(stored_path, "wb") as out:
            out.write(payload)

        conn.execute(
            """INSERT INTO attachments (
                id, email_id, filename, content_type, size_bytes,
                storage_path, content_hash, content_id, is_inline
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                attachment_id, email_id, original_name, content_type, len(payload),
                stored_path, sha256_bytes(payload), content_id, is_inline,
            ),
        )
        count += 1
    return count


def build_database(conn, msgid_to_file, msgid_to_labels, category_map,
                   virtual_map, attachments_dir):
    os.makedirs(attachments_dir, exist_ok=True)

    inserted = skipped = failed = 0
    for gmail_msgid, path in msgid_to_file.items():
        category = category_map.get(gmail_msgid)
        try:
            email_id, was_new = insert_email(
                conn, path, gmail_msgid, category, attachments_dir
            )

            for tag_name, kind in msgid_to_labels.get(gmail_msgid, set()):
                link_tag(conn, email_id, tag_name, kind)

            if category:
                link_tag(conn, email_id, category, "category")

            for bucket in virtual_map.get(gmail_msgid, set()):
                link_tag(conn, email_id, bucket, "virtual")

            conn.commit()
            inserted += was_new
            skipped += (not was_new)
        except Exception as e:
            conn.rollback()
            failed += 1
            print(f"[FAILED] {path}: {e}", file=sys.stderr)

    return inserted, skipped, failed


def print_tag_summary(conn):
    rows = conn.execute(
        """SELECT t.kind, t.name, COUNT(et.email_id)
           FROM tags t
           LEFT JOIN email_tags et ON et.tag_id = t.id
           GROUP BY t.id
           ORDER BY t.kind, COUNT(et.email_id) DESC"""
    ).fetchall()
    if not rows:
        return
    print("\nTag breakdown (name: emails):")
    current_kind = None
    for kind, name, cnt in rows:
        if kind != current_kind:
            print(f"  [{kind}]")
            current_kind = kind
        print(f"    {name}: {cnt}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Download Gmail, extract attachments, and record category/"
                    "label membership in a SQLite DB."
    )
    parser.add_argument("--emails-dir", default="emails",
                        help="Directory for downloaded .eml files (default: emails)")
    parser.add_argument("--db", default="emails.db",
                        help="Path to the SQLite DB (default: emails.db)")
    parser.add_argument("--attachments-dir", default="attachments",
                        help="Directory for extracted attachments (default: attachments)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max messages to read per source mailbox (0 = no limit)")
    parser.add_argument("--no-download", action="store_true",
                        help="Skip IMAP download; rebuild DB from existing .eml files")
    args = parser.parse_args()

    emails_dir = Path(args.emails_dir)
    limit = args.limit or None

    msgid_to_labels = {}
    msgid_to_file = {}
    category_map = {}
    virtual_map = {}

    if args.no_download:
        emails_dir.mkdir(exist_ok=True)
        for p in sorted(emails_dir.glob("*.eml")):
            msgid_to_file[p.stem] = p
        print(f"Offline mode: {len(msgid_to_file)} .eml files found. "
              "Labels/categories are not available without an IMAP connection.")
    else:
        load_dotenv(Path(__file__).parent / ".env")
        email_addr = os.getenv("GMAIL_EMAIL")
        password = os.getenv("GMAIL_APP_PASSWORD")
        if not email_addr or not password:
            sys.exit("Missing GMAIL_EMAIL or GMAIL_APP_PASSWORD in .env")

        mail = imap_connect(email_addr, password)
        try:
            mailboxes = list_mailboxes(mail)
            msgid_to_labels, msgid_to_file = download_all(mail, emails_dir, limit)
            category_map, virtual_map = collect_category_map(mail, mailboxes)
        finally:
            try:
                mail.logout()
            except Exception:
                pass

    conn = get_db(args.db)
    inserted, skipped, failed = build_database(
        conn, msgid_to_file, msgid_to_labels, category_map, virtual_map,
        args.attachments_dir,
    )
    print_tag_summary(conn)
    conn.close()

    print(f"\nDone. {inserted} new, {skipped} already existed, {failed} failed.")
    print(f"  Emails:      {emails_dir}/")
    print(f"  Attachments: {args.attachments_dir}/")
    print(f"  Database:    {args.db}")


if __name__ == "__main__":
    main()
