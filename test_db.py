#!/usr/bin/env python3

import argparse
import json
import sqlite3
import sys
from pathlib import Path

def connect_db(db_path):
    if not Path(db_path).exists():
        sys.exit(f"Database not found: {db_path}")
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        sys.exit(f"Failed to open database: {e}")

def print_section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print('='*70)


def get_basic_stats(conn):
    stats = {}
    
    stats['emails'] = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    stats['attachments'] = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    stats['tags'] = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    stats['email_tags'] = conn.execute("SELECT COUNT(*) FROM email_tags").fetchone()[0]
    
    stats['with_attachments'] = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE has_attachments = 1"
    ).fetchone()[0]
    
    stats['categories_assigned'] = conn.execute(
        "SELECT COUNT(*) FROM emails WHERE category IS NOT NULL"
    ).fetchone()[0]
    
    return stats


def get_tag_breakdown(conn, kind):
    rows = conn.execute("""
        SELECT t.name, COUNT(et.email_id) as count
        FROM tags t
        LEFT JOIN email_tags et ON et.tag_id = t.id
        WHERE t.kind = ?
        GROUP BY t.id
        ORDER BY count DESC
    """, (kind,)).fetchall()
    return [(r['name'], r['count']) for r in rows]


def get_sample_emails(conn, tag_name, limit=3):
    rows = conn.execute("""
        SELECT e.subject, e.sender_email, e.date_utc, e.snippet
        FROM emails e
        JOIN email_tags et ON et.email_id = e.id
        JOIN tags t ON t.id = et.tag_id
        WHERE t.name = ?
        ORDER BY e.date_utc DESC
        LIMIT ?
    """, (tag_name, limit)).fetchall()
    return rows


def check_data_completeness(conn):
    issues = []
    
    # Missing subjects
    count = conn.execute("SELECT COUNT(*) FROM emails WHERE subject IS NULL OR subject = ''").fetchone()[0]
    if count > 0:
        issues.append(f"{count} emails missing subject")
    
    # Missing sender
    count = conn.execute("SELECT COUNT(*) FROM emails WHERE sender_email IS NULL OR sender_email = ''").fetchone()[0]
    if count > 0:
        issues.append(f"{count} emails missing sender_email")
    
    # Missing date
    count = conn.execute("SELECT COUNT(*) FROM emails WHERE date_utc IS NULL").fetchone()[0]
    if count > 0:
        issues.append(f"{count} emails missing date_utc")
    
    # Missing body (both text and HTML)
    count = conn.execute("""
        SELECT COUNT(*) FROM emails 
        WHERE (body_text IS NULL OR body_text = '') 
          AND (body_html IS NULL OR body_html = '')
    """).fetchone()[0]
    if count > 0:
        issues.append(f"{count} emails missing both body_text and body_html")
    
    # Emails with attachments flag but no attachment records
    count = conn.execute("""
        SELECT COUNT(*) FROM emails e
        WHERE e.has_attachments = 1
          AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.email_id = e.id)
    """).fetchone()[0]
    if count > 0:
        issues.append(f"{count} emails marked has_attachments=1 but no attachments in DB")
    
    # Attachment records for emails not marked has_attachments
    count = conn.execute("""
        SELECT COUNT(DISTINCT a.email_id) FROM attachments a
        JOIN emails e ON e.id = a.email_id
        WHERE e.has_attachments = 0
    """).fetchone()[0]
    if count > 0:
        issues.append(f"{count} emails with attachments but has_attachments=0")
    
    # Orphaned attachments
    count = conn.execute("""
        SELECT COUNT(*) FROM attachments a
        WHERE NOT EXISTS (SELECT 1 FROM emails e WHERE e.id = a.email_id)
    """).fetchone()[0]
    if count > 0:
        issues.append(f"{count} orphaned attachments (no parent email)")
    
    # Duplicate gmail_msgid
    dupes = conn.execute("""
        SELECT gmail_msgid, COUNT(*) as cnt
        FROM emails
        GROUP BY gmail_msgid
        HAVING cnt > 1
    """).fetchall()
    if dupes:
        issues.append(f"{len(dupes)} duplicate gmail_msgid values found")
    
    return issues


def check_attachment_files(conn, attachments_dir):
    
    if not Path(attachments_dir).exists():
        return [f"Attachments directory not found: {attachments_dir}"]
    
    issues = []
    rows = conn.execute("SELECT id, storage_path, filename FROM attachments").fetchall()
    
    missing = []
    for row in rows:
        if not Path(row['storage_path']).exists():
            missing.append((row['id'], row['filename']))
    
    if missing:
        issues.append(f"{len(missing)} attachment files missing from disk:")
        for att_id, fname in missing[:5]:  # Show first 5
            issues.append(f"     - {fname} (id: {att_id})")
        if len(missing) > 5:
            issues.append(f"     ... and {len(missing) - 5} more")
    
    # Check for extra files on disk not in DB
    db_files = {Path(r['storage_path']).name for r in rows}
    disk_files = {f.name for f in Path(attachments_dir).iterdir() if f.is_file()}
    extra = disk_files - db_files
    if extra:
        issues.append(f"{len(extra)} files in {attachments_dir}/ not in DB")
        for fname in list(extra)[:5]:
            issues.append(f"     - {fname}")
        if len(extra) > 5:
            issues.append(f"     ... and {len(extra) - 5} more")
    
    return issues


def print_email_samples(rows, verbose=False):
    if not rows:
        print("    (no emails)")
        return
    
    for i, row in enumerate(rows, 1):
        subject = row['subject'] or "(no subject)"
        sender = row['sender_email'] or "(no sender)"
        date = row['date_utc'] or "(no date)"
        
        if verbose:
            snippet = row['snippet'] or "(no preview)"
            print(f"    {i}. {subject}")
            print(f"       From: {sender}")
            print(f"       Date: {date}")
            print(f"       Preview: {snippet[:80]}{'...' if len(snippet) > 80 else ''}")
        else:
            print(f"    {i}. {subject[:60]:<60}  ({sender})")


def main():
    parser = argparse.ArgumentParser(
        description="Inspect and validate emails.db created by master.py"
    )
    parser.add_argument("--db", default="emails.db", help="Path to the database")
    parser.add_argument("--samples", type=int, default=3, 
                       help="Number of sample emails to show per bucket")
    parser.add_argument("--verbose", action="store_true",
                       help="Show full details for sample emails")
    parser.add_argument("--attachments-dir", default="attachments",
                       help="Path to attachments directory for file checks")
    parser.add_argument("--no-samples", action="store_true",
                       help="Skip showing sample emails")
    args = parser.parse_args()
    
    conn = connect_db(args.db)
    
    # =========================================================================
    # BASIC STATS
    # =========================================================================
    print_section("DATABASE OVERVIEW")
    stats = get_basic_stats(conn)
    
    print(f"""
  Total Emails:              {stats['emails']:,}
  Total Attachments:         {stats['attachments']:,}
  Emails with Attachments:   {stats['with_attachments']:,}
  Total Tags:                {stats['tags']:,}
  Total Email-Tag Links:     {stats['email_tags']:,}
  Emails with Category:      {stats['categories_assigned']:,}
    """)
    
    if stats['emails'] == 0:
        print("\n Database is empty. Run master.py to populate it.\n")
        sys.exit(0)
    
    # =========================================================================
    # CATEGORIES (inbox tabs)
    # =========================================================================
    print_section("INBOX CATEGORIES (Primary/Social/Promotions/etc.)")
    categories = get_tag_breakdown(conn, "category")
    
    if categories:
        for name, count in categories:
            pct = (count / stats['emails']) * 100 if stats['emails'] > 0 else 0
            print(f"  {name:<20} {count:>6,} emails  ({pct:>5.1f}%)")
            if not args.no_samples and count > 0:
                samples = get_sample_emails(conn, name, args.samples)
                print_email_samples(samples, args.verbose)
    else:
        print("  (no category tags found)")
    
    # Count emails with NO category assigned
    no_cat = stats['emails'] - stats['categories_assigned']
    if no_cat > 0:
        pct = (no_cat / stats['emails']) * 100
        print(f"\n  {no_cat:,} emails ({pct:.1f}%) have NO category assigned")
        print("      (This is normal for Sent/Draft/Spam/Trash items)")
    
    # =========================================================================
    # SYSTEM FOLDERS
    # =========================================================================
    print_section("SYSTEM FOLDERS (Inbox/Sent/Draft/Spam/etc.)")
    system = get_tag_breakdown(conn, "system")
    
    if system:
        for name, count in system:
            pct = (count / stats['emails']) * 100 if stats['emails'] > 0 else 0
            print(f"  {name:<20} {count:>6,} emails  ({pct:>5.1f}%)")
            if not args.no_samples and count > 0:
                samples = get_sample_emails(conn, name, args.samples)
                print_email_samples(samples, args.verbose)
    else:
        print("  (no system tags found)")
    
    # =========================================================================
    # USER LABELS
    # =========================================================================
    print_section("USER LABELS")
    user = get_tag_breakdown(conn, "user")
    
    if user:
        for name, count in user:
            print(f"  {name:<40} {count:>6,} emails")
            if not args.no_samples and count > 0:
                samples = get_sample_emails(conn, name, args.samples)
                print_email_samples(samples, args.verbose)
    else:
        print("  (no user labels found)")
    
    # =========================================================================
    # VIRTUAL BUCKETS
    # =========================================================================
    print_section("VIRTUAL BUCKETS (Scheduled/Snoozed)")
    virtual = get_tag_breakdown(conn, "virtual")
    
    if virtual:
        for name, count in virtual:
            print(f"  {name:<20} {count:>6,} emails")
            if not args.no_samples and count > 0:
                samples = get_sample_emails(conn, name, args.samples)
                print_email_samples(samples, args.verbose)
    else:
        print("  (no virtual buckets found)")
        print("  Note: scheduled/snoozed are best-effort and may not be")
        print("        exposed by all Gmail accounts over IMAP.")
    
    # =========================================================================
    # ATTACHMENTS BREAKDOWN
    # =========================================================================
    print_section("ATTACHMENTS")
    
    if stats['attachments'] > 0:
        # By content type
        types = conn.execute("""
            SELECT content_type, COUNT(*) as count, SUM(size_bytes) as total_bytes
            FROM attachments
            GROUP BY content_type
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()
        
        print("\n  Top Content Types:")
        for row in types:
            mb = row['total_bytes'] / (1024 * 1024)
            print(f"    {row['content_type']:<40} {row['count']:>5} files  "
                  f"({mb:>8.2f} MB)")
        
        # Inline vs regular
        inline_count = conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE is_inline = 1"
        ).fetchone()[0]
        regular_count = stats['attachments'] - inline_count
        print(f"\n  Inline attachments:  {inline_count:>6,}")
        print(f"  Regular attachments: {regular_count:>6,}")
        
        # Total size
        total_bytes = conn.execute(
            "SELECT SUM(size_bytes) FROM attachments"
        ).fetchone()[0] or 0
        total_mb = total_bytes / (1024 * 1024)
        total_gb = total_bytes / (1024 * 1024 * 1024)
        size_str = f"{total_gb:.2f} GB" if total_gb >= 1 else f"{total_mb:.2f} MB"
        print(f"  Total size:          {size_str}")
    else:
        print("  (no attachments)")
    
    # =========================================================================
    # DATA COMPLETENESS
    # =========================================================================
    print_section("DATA COMPLETENESS CHECKS")
    
    issues = check_data_completeness(conn)
    if not issues:
        print("\n  All checks passed! Data looks good.")
    else:
        print()
        for issue in issues:
            print(f"  {issue}")
    
    # =========================================================================
    # ATTACHMENT FILE CHECKS
    # =========================================================================
    print_section("ATTACHMENT FILE CHECKS")
    
    att_issues = check_attachment_files(conn, args.attachments_dir)
    if not att_issues:
        print(f"\n  All {stats['attachments']:,} attachment files exist on disk.")
    else:
        print()
        for issue in att_issues:
            print(f"  {issue}")
    
    # =========================================================================
    # DATE RANGE
    # =========================================================================
    print_section("DATE RANGE")
    
    date_range = conn.execute("""
        SELECT MIN(date_utc) as earliest, MAX(date_utc) as latest
        FROM emails
        WHERE date_utc IS NOT NULL
    """).fetchone()
    
    if date_range['earliest']:
        print(f"""
  Earliest email: {date_range['earliest']}
  Latest email:   {date_range['latest']}
        """)
    else:
        print("\n  (no dates found)")
    
    # =========================================================================
    # SUMMARY
    # =========================================================================
    print_section("SUMMARY")
    
    total_issues = len(issues) + len(att_issues)
    if total_issues == 0:
        print("""
     Database looks healthy! All data properly stored.
  
  Your emails.db contains:
    - All Gmail categories (Primary/Social/Promotions/Updates/Forums)
    - System folders (Inbox/Sent/Draft/Spam/Trash/Starred/Important)
    - User labels
    - Virtual buckets (Scheduled/Snoozed, if available)
    - Attachments with proper associations
        """)
    else:
        print(f"""
      Found {total_issues} potential issue(s). Review the sections above.
        """)
    
    print("="*70)
    print()
    
    conn.close()


if __name__ == "__main__":
    main()

