#!/usr/bin/env python3
"""
gmail_sync.py
=============

USAGE
-----
    python gmail_sync.py --self-test       # verify parsers, no network needed
    python gmail_sync.py --limit 800       # download newest 800 messages
    python gmail_sync.py                   # download everything
    python gmail_sync.py --verify          # print a DB summary at the end
"""

import argparse
import hashlib
import imaplib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_a, **_k):
        return False


IMAP_HOST = "imap.gmail.com"

CATEGORIES = ["primary", "social", "promotions", "updates", "forums"]

CATEGORY_PRECEDENCE = ["forums", "social", "promotions", "updates", "primary"]


def resolve_category(cats):
    """Pick the most specific category from a set of matches (None if empty)."""
    for cat in CATEGORY_PRECEDENCE:
        if cat in cats:
            return cat
    return None

VIRTUAL_BUCKETS = ["scheduled", "snoozed"]

SYSTEM_LABELS = {
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
    r"\bin": "trash",
    r"\all": "all_mail",
    r"\allmail": "all_mail",
    r"\chats": "chat",
    r"\chat": "chat",
}

BODY_CHUNK = 20
META_CHUNK = 200

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# =============================================================================
# Schema
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id              TEXT PRIMARY KEY,
    gmail_msgid     TEXT UNIQUE,        -- X-GM-MSGID, stable dedup key
    message_id      TEXT,               -- RFC Message-ID header
    thread_id       TEXT,
    subject         TEXT,
    sender_name     TEXT,
    sender_email    TEXT,
    to_addrs        TEXT,               -- JSON [{name,email}]
    cc_addrs        TEXT,
    bcc_addrs       TEXT,
    date_utc        TEXT,               -- ISO 8601
    category        TEXT,               -- primary/social/... (NULL if none)
    snippet         TEXT,
    body_text       TEXT,
    body_html       TEXT,
    has_attachments INTEGER DEFAULT 0,
    raw_headers     TEXT,               -- JSON {header: [values]}
    source_file     TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,
    email_id        TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    filename        TEXT,
    content_type    TEXT,
    size_bytes      INTEGER,
    storage_path    TEXT,
    content_hash    TEXT,
    content_id      TEXT,
    is_inline       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT UNIQUE NOT NULL,
    kind    TEXT NOT NULL               -- category|system|user|virtual
);

CREATE TABLE IF NOT EXISTS email_tags (
    email_id TEXT NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (email_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_emails_date     ON emails(date_utc);
CREATE INDEX IF NOT EXISTS idx_emails_category ON emails(category);
CREATE INDEX IF NOT EXISTS idx_emails_thread   ON emails(thread_id);
CREATE INDEX IF NOT EXISTS idx_att_email       ON attachments(email_id);
CREATE INDEX IF NOT EXISTS idx_etags_tag       ON email_tags(tag_id);
"""


def get_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def link_tag(conn, email_id, name, kind):
    if not name:
        return
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    tag_id = row[0] if row else conn.execute(
        "INSERT INTO tags (name, kind) VALUES (?, ?)", (name, kind)
    ).lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO email_tags (email_id, tag_id) VALUES (?, ?)",
        (email_id, tag_id),
    )


# =============================================================================
# FETCH response parsing  (this is where the old bug lived)
# =============================================================================

def unescape_imap_string(s):
    
    return s.replace(r"\\", "\\").replace(r'\"', '"')


def tokenize_labels(blob):
    
    out = []
    for quoted, bare in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', blob):
        val = unescape_imap_string(quoted) if quoted else bare
        if val:
            out.append(val)
    return out


def parse_fetch_line(prefix):
    
    if isinstance(prefix, (bytes, bytearray)):
        text = prefix.decode("utf-8", errors="replace")
    else:
        text = prefix

    m = re.search(r"\bUID\s+(\d+)", text)
    uid = m.group(1) if m else None

    m = re.search(r"\bX-GM-MSGID\s+(\d+)", text)
    msgid = m.group(1) if m else None

    labels = []
    m = re.search(r"\bX-GM-LABELS\s+\(", text)
    if m:
        start = m.end() - 1          # index of the opening paren
        depth = 0
        end = start
        in_quotes = False
        i = start
        while i < len(text):
            c = text[i]
            if c == "\\" and in_quotes:
                i += 2               # skip escaped char inside quotes
                continue
            if c == '"':
                in_quotes = not in_quotes
            elif not in_quotes:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            i += 1
        labels = tokenize_labels(text[start + 1:end])

    return uid, msgid, labels


def normalize_label(label):
    
    if label.startswith("\\"):
        key = label.lower().replace(" ", "")
        return SYSTEM_LABELS.get(key, label.lstrip("\\").lower()), "system"
    return label, "user"


def iter_fetch_items(data):
    
    for item in data:
        if isinstance(item, tuple):
            if len(item) >= 2:
                yield item[0] or b"", item[1]
            elif item:
                yield item[0] or b"", None
        elif isinstance(item, (bytes, bytearray)):
            if item.strip() in (b")", b""):
                continue
            yield item, None


# =============================================================================
# Mailbox discovery
# =============================================================================

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


def list_mailboxes(mail):
    status, data = mail.list()
    if status != "OK":
        raise RuntimeError("IMAP LIST failed")

    boxes = []
    for line in data:
        if line is None:
            continue
        if isinstance(line, tuple):
            line = b"".join(x for x in line if isinstance(x, (bytes, bytearray)))
        m = _LIST_RE.match(line.strip())
        if not m:
            continue

        flags = m.group("flags").decode("utf-8", errors="replace").split()
        name = m.group("name").decode("utf-8", errors="replace").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]

        tag = None
        for f in flags:
            key = f.lower().replace(" ", "")
            if key in SYSTEM_LABELS:
                tag = SYSTEM_LABELS[key]
                break
        if tag is None and name.upper() == "INBOX":
            tag = "inbox"

        boxes.append({"name": name, "flags": flags, "tag": tag})
    return boxes


def pick_sources(boxes):
    
    by_tag = {}
    for b in boxes:
        if b["tag"]:
            by_tag.setdefault(b["tag"], b)

    if "all_mail" in by_tag:
        srcs = [by_tag["all_mail"]]
        srcs += [by_tag[t] for t in ("spam", "trash") if t in by_tag]
        return srcs, "all_mail + spam + trash"

    srcs = [b for b in boxes
            if "\\Noselect" not in b["flags"] and b["name"].upper() != "[GMAIL]"]
    return srcs, "per-folder fallback"


def select(mail, mailbox, readonly=True):
    status, _ = mail.select(f'"{mailbox}"', readonly=readonly)
    return status == "OK"


# =============================================================================
# Download
# =============================================================================

def download(mail, out_dir, limit):
    
    out_dir.mkdir(parents=True, exist_ok=True)
    boxes = list_mailboxes(mail)
    sources, strategy = pick_sources(boxes)

    print("Mailboxes discovered:")
    for b in boxes:
        print(f"  {b['name']}" + (f"  -> {b['tag']}" if b["tag"] else ""))
    print(f"\nCoverage: {strategy}")
    print(f"Reading:  {', '.join(b['name'] for b in sources)}\n")

    records = {}
    oldest = None
    no_msgid = 0

    for box in sources:
        if not select(mail, box["name"]):
            print(f"  [skip] cannot select {box['name']}")
            continue
        status, data = mail.uid("search", None, "ALL")
        if status != "OK" or not data or data[0] is None:
            print(f"  [skip] search failed in {box['name']}")
            continue

        uids = data[0].split()
        if limit:
            uids = uids[-limit:]
        if not uids:
            print(f"  {box['name']}: 0 messages")
            continue

        print(f"  {box['name']}: {len(uids)} messages", flush=True)
        done = 0

        for i in range(0, len(uids), BODY_CHUNK):
            chunk = uids[i:i + BODY_CHUNK]
            status, data = mail.uid(
                "fetch", b",".join(chunk), "(X-GM-MSGID X-GM-LABELS RFC822)"
            )
            if status != "OK" or not data:
                continue

            for prefix, body in iter_fetch_items(data):
                uid, msgid, labels = parse_fetch_line(prefix)
                if not msgid:
                    no_msgid += 1
                    continue

                rec = records.setdefault(msgid, {"path": None, "tags": set()})
                for lbl in labels:
                    rec["tags"].add(normalize_label(lbl))
                if box["tag"] and box["tag"] != "all_mail":
                    # Spam/Trash often carry no labels of their own.
                    rec["tags"].add((box["tag"], "system"))

                if body and rec["path"] is None:
                    path = out_dir / f"{msgid}.eml"
                    with open(path, "wb") as f:
                        f.write(body)
                    rec["path"] = path

                    d = header_date(body)
                    if d and (oldest is None or d < oldest):
                        oldest = d
                done += 1

            if done and done % 200 == 0:
                print(f"    ... {done}/{len(uids)}", flush=True)

    stored = sum(1 for r in records.values() if r["path"])
    print(f"\nStored {stored} unique messages "
          f"({len(records)} seen).")
    if no_msgid:
        print(f"  note: {no_msgid} response rows had no X-GM-MSGID and were skipped")
    return boxes, records, oldest


def header_date(raw):
    
    m = re.search(rb"^Date:\s*(.+)$", raw[:8192], re.MULTILINE | re.IGNORECASE)
    if not m:
        return None
    try:
        dt = parsedate_to_datetime(m.group(1).decode("utf-8", errors="replace").strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


# =============================================================================
# Category + virtual bucket resolution
# =============================================================================

def imap_date(dt):
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def search_msgids(mail, mailbox, raw_query, since=None):
    """X-GM-RAW search -> set of X-GM-MSGID. Narrowed by SINCE when provided."""
    if not select(mail, mailbox):
        return set()

    args = ["X-GM-RAW", f'"{raw_query}"']
    if since:
        args = ["SINCE", imap_date(since)] + args

    try:
        status, data = mail.uid("search", None, *args)
    except imaplib.IMAP4.error:
        return set()
    if status != "OK" or not data or data[0] is None:
        return set()

    uids = data[0].split()
    if not uids:
        return set()

    found = set()
    for i in range(0, len(uids), META_CHUNK):
        chunk = uids[i:i + META_CHUNK]
        status, data = mail.uid("fetch", b",".join(chunk), "(X-GM-MSGID)")
        if status != "OK" or not data:
            continue
        for prefix, _ in iter_fetch_items(data):
            _uid, msgid, _labels = parse_fetch_line(prefix)
            if msgid:
                found.add(msgid)
    return found


def resolve_buckets(mail, boxes, downloaded, since):
    
    inbox = next((b["name"] for b in boxes if b["tag"] == "inbox"), "INBOX")
    all_mail = next((b["name"] for b in boxes if b["tag"] == "all_mail"), inbox)

    if since:
        print(f"\nResolving categories in {inbox} (SINCE {imap_date(since)}):")
    else:
        print(f"\nResolving categories in {inbox}:")

    category_sets = {}
    for cat in CATEGORIES:
        found = search_msgids(mail, inbox, f"category:{cat}", since)
        hits = found & downloaded
        for mid in hits:
            category_sets.setdefault(mid, set()).add(cat)
        print(f"  {cat:<12} {len(hits):>6} matched   ({len(found)} in search window)")

    overlaps = {m: c for m, c in category_sets.items() if len(c) > 1}
    if overlaps:
        print(f"\n  {len(overlaps)} message(s) matched more than one category.")
        print("  This is normal: 'primary' also returns messages from any tab")
        print("  you have switched off in Gmail's settings. Every match is kept")
        print("  as a tag; the `category` column holds the most specific one.")
        example = next(iter(overlaps.values()))
        print(f"  e.g. {sorted(example)} -> category = {resolve_category(example)!r}")

    print("\nResolving scheduled/snoozed (best effort):")
    virtual_map = {}
    for bucket in VIRTUAL_BUCKETS:
        found = search_msgids(mail, all_mail, f"in:{bucket}", since)
        hits = found & downloaded
        for mid in hits:
            virtual_map.setdefault(mid, set()).add(bucket)
        suffix = "" if hits else "   (none / not exposed by this account)"
        print(f"  {bucket:<12} {len(hits):>6} matched{suffix}")

    print("\nNote: 'outbox' is a client-side send queue and does not exist on "
          "the IMAP server, so it cannot be captured.")
    return category_sets, virtual_map


# =============================================================================
# Email -> DB
# =============================================================================

def addrs_json(value):
    if not value:
        return json.dumps([])
    return json.dumps([{"name": n, "email": e}
                       for n, e in getaddresses([value]) if e])


def msg_date(msg):
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


def thread_id(msg):
    refs = msg.get("References")
    if refs and refs.split():
        return refs.split()[0].strip()
    if msg.get("In-Reply-To"):
        return msg.get("In-Reply-To").strip()
    return (msg.get("Message-ID") or "").strip() or None


def bodies(msg):
    def content(part):
        if part is None:
            return None
        try:
            return part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if not payload:
                return None
            return payload.decode(part.get_content_charset() or "utf-8",
                                  errors="replace")
    return (content(msg.get_body(preferencelist=("plain",))),
            content(msg.get_body(preferencelist=("html",))))


def snippet_of(text, html, n=150):
    src = text or (re.sub(r"<[^>]+>", " ", html) if html else "")
    return " ".join(src.split())[:n] if src else ""


def save_attachments(msg, email_id, conn, att_dir):
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

        name = part.get_filename() or f"attachment-{uuid.uuid4().hex[:8]}"
        ctype = part.get_content_type()
        cid = (part.get("Content-ID") or "").strip("<>") or None
        att_id = str(uuid.uuid4())
        ext = os.path.splitext(name)[1] or mimetypes.guess_extension(ctype) or ""
        path = os.path.join(att_dir, f"{att_id}{ext}")

        with open(path, "wb") as f:
            f.write(payload)

        conn.execute(
            """INSERT INTO attachments (id, email_id, filename, content_type,
                   size_bytes, storage_path, content_hash, content_id, is_inline)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (att_id, email_id, name, ctype, len(payload), path,
             hashlib.sha256(payload).hexdigest(), cid,
             1 if part.get_content_disposition() == "inline" else 0),
        )
        count += 1
    return count


def insert_email(conn, path, msgid, category, att_dir):
    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    row = conn.execute("SELECT id FROM emails WHERE gmail_msgid = ?",
                       (msgid,)).fetchone()
    if row:
        return row[0], False

    email_id = str(uuid.uuid4())
    pairs = getaddresses([msg.get("From", "")])
    sender_name, sender_email = pairs[0] if pairs else ("", "")
    text, html = bodies(msg)

    headers = {}
    for k, v in msg.items():
        headers.setdefault(k, []).append(str(v))

    conn.execute(
        """INSERT INTO emails (id, gmail_msgid, message_id, thread_id, subject,
               sender_name, sender_email, to_addrs, cc_addrs, bcc_addrs,
               date_utc, category, snippet, body_text, body_html,
               has_attachments, raw_headers, source_file, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (email_id, msgid, (msg.get("Message-ID") or "").strip(), thread_id(msg),
         msg.get("Subject", ""), sender_name, sender_email,
         addrs_json(msg.get("To")), addrs_json(msg.get("Cc")),
         addrs_json(msg.get("Bcc")), msg_date(msg), category,
         snippet_of(text, html), text, html, 0, json.dumps(headers),
         os.path.basename(str(path)), datetime.now(timezone.utc).isoformat()),
    )

    if save_attachments(msg, email_id, conn, att_dir):
        conn.execute("UPDATE emails SET has_attachments = 1 WHERE id = ?",
                     (email_id,))
    return email_id, True


def build_db(conn, records, category_sets, virtual_map, att_dir):
    os.makedirs(att_dir, exist_ok=True)
    new = dup = fail = 0

    for msgid, rec in records.items():
        if not rec["path"]:
            continue
        cats = category_sets.get(msgid) or set()
        category = resolve_category(cats)
        try:
            email_id, is_new = insert_email(conn, rec["path"], msgid,
                                            category, att_dir)
            for name, kind in rec["tags"]:
                link_tag(conn, email_id, name, kind)
            # Link EVERY matched category, not just the winner, so the overlap
            # between 'primary' and a specific tab is never lost.
            for cat in cats:
                link_tag(conn, email_id, cat, "category")
            for bucket in virtual_map.get(msgid, ()):
                link_tag(conn, email_id, bucket, "virtual")
            conn.commit()
            new += is_new
            dup += not is_new
        except Exception as e:
            conn.rollback()
            fail += 1
            print(f"[FAILED] {rec['path']}: {e}", file=sys.stderr)

    return new, dup, fail


def summarize(conn):
    total = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    att = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    print(f"\n{'='*58}\n  DATABASE SUMMARY\n{'='*58}")
    print(f"  emails: {total}    attachments: {att}")

    rows = conn.execute(
        """SELECT t.kind, t.name, COUNT(et.email_id) c
           FROM tags t LEFT JOIN email_tags et ON et.tag_id = t.id
           GROUP BY t.id ORDER BY t.kind, c DESC"""
    ).fetchall()

    kind = None
    for k, name, c in rows:
        if k != kind:
            label = ("category tags (a message can hold several)"
                     if k == "category" else k)
            print(f"\n  [{label}]")
            kind = k
        print(f"    {name:<28} {c:>6}")

    # The `category` column keeps one winner per message; show it separately so
    # it can never silently disagree with the tag counts above.
    col = conn.execute(
        """SELECT COALESCE(category, '(none)'), COUNT(*)
           FROM emails GROUP BY category ORDER BY COUNT(*) DESC"""
    ).fetchall()
    print("\n  [category column -- one winner per message]")
    for name, c in col:
        print(f"    {name:<28} {c:>6}")

    print(f"\n  '(none)' is expected for sent, draft, spam, trash and archived"
          f"\n  mail, because Gmail only assigns a category to inbox messages.")
    print("="*58)


# =============================================================================
# Self test -- uses the exact bytes a real Gmail server returned
# =============================================================================

def self_test():
    ok = True

    def check(label, got, want):
        nonlocal ok
        if got == want:
            print(f"  PASS  {label}")
        else:
            ok = False
            print(f"  FAIL  {label}\n        got  {got!r}\n        want {want!r}")

    print("Parsing real Gmail FETCH responses:\n")

    # Verbatim from a live server. Note seq 44852 != UID 45640.
    line = rb'44852 (X-GM-MSGID 1872165143799453659 X-GM-LABELS ("\\Important") UID 45640)'
    uid, msgid, labels = parse_fetch_line(line)
    check("uid is the UID token, not the sequence number", uid, "45640")
    check("msgid parsed", msgid, "1872165143799453659")
    check("escaped system label unquoted", labels, ["\\Important"])
    check("label normalized", [normalize_label(l) for l in labels],
          [("important", "system")])

    line = rb'44854 (X-GM-MSGID 1872173196805907297 X-GM-LABELS () UID 45642)'
    uid, msgid, labels = parse_fetch_line(line)
    check("empty label list", (uid, msgid, labels),
          ("45642", "1872173196805907297", []))

    line = rb'1 (X-GM-MSGID 1833189891985626277 X-GM-LABELS () UID 1409)'
    uid, msgid, _ = parse_fetch_line(line)
    check("drafts row", (uid, msgid), ("1409", "1833189891985626277"))

    # Mixed system + user labels, quoted name containing a space.
    line = (rb'7 (X-GM-MSGID 42 X-GM-LABELS ('
            rb'"\\Inbox" "\\Important" "Work/Clients" "Read Later") UID 99)')
    uid, msgid, labels = parse_fetch_line(line)
    check("mixed labels", labels,
          ["\\Inbox", "\\Important", "Work/Clients", "Read Later"])
    check("mixed normalized", sorted(normalize_label(l) for l in labels),
          sorted([("inbox", "system"), ("important", "system"),
                  ("Work/Clients", "user"), ("Read Later", "user")]))

    # Body fetch shape: (prefix, literal) then a stray b')'
    data = [(rb'44852 (X-GM-MSGID 777 X-GM-LABELS ("\\Sent") UID 45640 RFC822 {12}',
             b"From: a@b.c\n"), b")"]
    items = list(iter_fetch_items(data))
    check("iter_fetch_items drops stray paren", len(items), 1)
    uid, msgid, labels = parse_fetch_line(items[0][0])
    check("body-fetch metadata", (uid, msgid, labels),
          ("45640", "777", ["\\Sent"]))
    check("body bytes preserved", items[0][1], b"From: a@b.c\n")

    print("\nParsing LIST responses:\n")

    class FakeList:
        def list(self):
            return ("OK", [
                rb'(\HasNoChildren) "/" "INBOX"',
                rb'(\HasChildren \Noselect) "/" "[Gmail]"',
                rb'(\HasNoChildren \All) "/" "[Gmail]/All Mail"',
                rb'(\HasNoChildren \Trash) "/" "[Gmail]/Bin"',
                rb'(\HasNoChildren \Drafts) "/" "[Gmail]/Drafts"',
                rb'(\HasNoChildren \Important) "/" "[Gmail]/Important"',
                rb'(\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"',
                rb'(\HasNoChildren \Junk) "/" "[Gmail]/Spam"',
                rb'(\HasNoChildren \Flagged) "/" "[Gmail]/Starred"',
                rb'(\HasNoChildren) "/" "Work/Clients"',
            ])

    boxes = list_mailboxes(FakeList())
    tags = {b["name"]: b["tag"] for b in boxes}
    check("INBOX tagged", tags.get("INBOX"), "inbox")
    check("All Mail tagged", tags.get("[Gmail]/All Mail"), "all_mail")
    check("Bin -> trash", tags.get("[Gmail]/Bin"), "trash")
    check("Spam via \\Junk", tags.get("[Gmail]/Spam"), "spam")
    check("Starred via \\Flagged", tags.get("[Gmail]/Starred"), "starred")
    check("user folder untagged", tags.get("Work/Clients"), None)

    srcs, _ = pick_sources(boxes)
    check("sources are All Mail + Spam + Trash",
          [b["name"] for b in srcs],
          ["[Gmail]/All Mail", "[Gmail]/Spam", "[Gmail]/Bin"])

    print("\nEnd-to-end DB build with an attachment:\n")

    from email.message import EmailMessage
    tmp = Path("._selftest")
    (tmp / "eml").mkdir(parents=True, exist_ok=True)
    (tmp / "att").mkdir(parents=True, exist_ok=True)

    m = EmailMessage()
    m["Message-ID"] = "<x@mail.gmail.com>"
    m["Subject"] = "Quarterly report"
    m["From"] = "Alice Example <alice@example.com>"
    m["To"] = "bob@example.com"
    m["Date"] = "Thu, 30 Jul 2026 10:00:00 +0000"
    m.set_content("Numbers are attached.")
    pdf_bytes = b"%PDF-1.4 fake"
    m.add_attachment(pdf_bytes, maintype="application",
                     subtype="pdf", filename="q3.pdf")
    eml = tmp / "eml" / "555.eml"
    eml.write_bytes(bytes(m))

    db = tmp / "t.db"
    if db.exists():
        db.unlink()
    conn = get_db(str(db))
    records = {"555": {"path": eml, "tags": {("inbox", "system"),
                                            ("important", "system"),
                                            ("Work/Clients", "user")}}}
    new, dup, fail = build_db(conn, records, {"555": {"promotions"}},
                              {"555": {"snoozed"}}, str(tmp / "att"))
    check("inserted/dup/fail", (new, dup, fail), (1, 0, 0))

    row = conn.execute(
        "SELECT subject, sender_email, category, has_attachments FROM emails"
    ).fetchone()
    check("email row", row,
          ("Quarterly report", "alice@example.com", "promotions", 1))

    tags = conn.execute(
        """SELECT t.kind, t.name FROM tags t
           JOIN email_tags et ON et.tag_id = t.id ORDER BY t.kind, t.name"""
    ).fetchall()
    check("all four tag kinds linked", tags,
          [("category", "promotions"), ("system", "important"),
           ("system", "inbox"), ("user", "Work/Clients"),
           ("virtual", "snoozed")])

    att = conn.execute(
        "SELECT filename, content_type, size_bytes FROM attachments"
    ).fetchone()
    check("attachment linked", att,
          ("q3.pdf", "application/pdf", len(pdf_bytes)))
    check("attachment file written",
          len(list((tmp / "att").iterdir())), 1)

    # Re-running must not duplicate.
    new2, dup2, _ = build_db(conn, records, {"555": {"promotions"}},
                             {"555": {"snoozed"}}, str(tmp / "att"))
    check("idempotent on re-run", (new2, dup2), (0, 1))
    conn.close()

    print("\nMulti-category handling (the primary/updates overlap bug):\n")

    check("specific tab beats primary",
          resolve_category({"primary", "updates"}), "updates")
    check("forums outranks everything",
          resolve_category({"primary", "updates", "forums"}), "forums")
    check("primary alone still wins",
          resolve_category({"primary"}), "primary")
    check("empty set -> None", resolve_category(set()), None)

    db2 = tmp / "t2.db"
    conn = get_db(str(db2))
    eml2 = tmp / "eml" / "666.eml"
    eml2.write_bytes(bytes(m))
    recs2 = {"666": {"path": eml2, "tags": {("inbox", "system")}}}
    # This message matched BOTH searches, exactly like the real run did.
    build_db(conn, recs2, {"666": {"primary", "updates"}}, {}, str(tmp / "att"))

    col = conn.execute("SELECT category FROM emails").fetchone()[0]
    check("category column holds the specific tab", col, "updates")

    cat_tags = conn.execute(
        """SELECT t.name FROM tags t JOIN email_tags et ON et.tag_id = t.id
           WHERE t.kind = 'category' ORDER BY t.name"""
    ).fetchall()
    check("BOTH categories kept as tags (nothing dropped)",
          [r[0] for r in cat_tags], ["primary", "updates"])
    conn.close()

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("ALL TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


# =============================================================================
# Main
# =============================================================================

def main():
    p = argparse.ArgumentParser(
        description="Download Gmail into .eml + attachments + a tagged SQLite DB."
    )
    p.add_argument("--emails-dir", default="emails")
    p.add_argument("--db", default="emails.db")
    p.add_argument("--attachments-dir", default="attachments")
    p.add_argument("--limit", type=int, default=0,
                   help="newest N messages per source mailbox (0 = all)")
    p.add_argument("--since-days", type=int, default=0,
                   help="narrow the category search window (0 = auto from data)")
    p.add_argument("--verify", action="store_true",
                   help="print a DB summary when finished")
    p.add_argument("--self-test", action="store_true",
                   help="verify parsers and DB logic offline, then exit")
    args = p.parse_args()

    if args.self_test:
        sys.exit(self_test())

    load_dotenv(Path(__file__).parent / ".env")
    user = os.getenv("GMAIL_EMAIL")
    pwd = os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pwd:
        sys.exit("Missing GMAIL_EMAIL or GMAIL_APP_PASSWORD in .env")

    emails_dir = Path(args.emails_dir)
    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(user, pwd)

    try:
        boxes, records, oldest = download(mail, emails_dir, args.limit or None)
        downloaded = {mid for mid, r in records.items() if r["path"]}

        since = None
        if args.since_days:
            since = datetime.now(timezone.utc) - timedelta(days=args.since_days)
        elif oldest:
            since = oldest - timedelta(days=1)   # pad for timezone skew

        category_sets, virtual_map = resolve_buckets(mail, boxes, downloaded, since)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    conn = get_db(args.db)
    new, dup, fail = build_db(conn, records, category_sets, virtual_map,
                              args.attachments_dir)
    print(f"\nInserted {new}, already present {dup}, failed {fail}.")
    if args.verify:
        summarize(conn)
    conn.close()

    print(f"\n  emails:      {emails_dir}/")
    print(f"  attachments: {args.attachments_dir}/")
    print(f"  database:    {args.db}")


if __name__ == "__main__":
    main()
