#!/usr/bin/env python3

import sys
from email import policy
from email.parser import BytesParser


SEP = "-" * 70


def dump_headers(msg):
    print(SEP)
    print("ALL HEADERS (raw, in order — includes anything a DB column might miss)")
    print(SEP)
    for key, value in msg.items():
        print(f"{key}: {value}")

    captured = {
        "message-id", "references", "in-reply-to", "subject",
        "from", "to", "cc", "bcc", "date",
    }
    ignored = [k for k in msg.keys() if k.lower() not in captured]
    if ignored:
        print()
        print("Headers NOT given their own DB column (only in raw_headers JSON):")
        for k in ignored:
            print(f"  - {k}")


def dump_structure(msg, indent=0):
    pad = "  " * indent
    ctype = msg.get_content_type()
    disp = msg.get_content_disposition()  # 'attachment', 'inline', or None
    filename = msg.get_filename()
    cid = msg.get("Content-ID")
    charset = msg.get_content_charset()

    print(f"{pad}- type={ctype}  disposition={disp}  filename={filename}  "
          f"content-id={cid}  charset={charset}")

    if msg.is_multipart():
        for part in msg.iter_parts():
            dump_structure(part, indent + 1)
    else:
        try:
            payload = msg.get_content()
            if isinstance(payload, str):
                preview = " ".join(payload.split())[:80]
                print(f"{pad}    text preview: {preview!r}")
            elif isinstance(payload, bytes):
                print(f"{pad}    binary size: {len(payload)} bytes")
        except Exception as e:
            print(f"{pad}    [could not decode payload: {e}]")


def dump_body_selection(msg):
    print(SEP)
    print("WHAT get_body() WOULD PICK")
    print(SEP)
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    print(f"plain part found: {plain is not None}")
    print(f"html part found:  {html is not None}")
    if plain is None and html is None:
        print("!! No body part found — email would end up with empty body_text/body_html.")


def dump_attachments(msg):
    print(SEP)
    print("WHAT iter_attachments() WOULD CAPTURE")
    print(SEP)
    found_any = False
    for part in msg.iter_attachments():
        found_any = True
        payload = part.get_content()
        size = len(payload) if isinstance(payload, (bytes, str)) else "?"
        print(f"  filename={part.get_filename()!r}  type={part.get_content_type()}  "
              f"disposition={part.get_content_disposition()}  content-id={part.get('Content-ID')}  "
              f"size={size}")
    if not found_any:
        print("  (none found)")


def dump_multipart_warning(msg):
    print(SEP)
    print("SANITY CHECK: any part with a filename that ISN'T in iter_attachments()?")
    print(SEP)
    captured_filenames = {p.get_filename() for p in msg.iter_attachments()}
    suspects = []

    def walk(part):
        if part.is_multipart():
            for p in part.iter_parts():
                walk(p)
        else:
            fn = part.get_filename()
            if fn and fn not in captured_filenames:
                suspects.append((fn, part.get_content_type(), part.get_content_disposition()))

    walk(msg)
    if suspects:
        for fn, ctype, disp in suspects:
            print(f"  !! MISSED: filename={fn!r} type={ctype} disposition={disp}")
    else:
        print("  none — iter_attachments() is catching everything with a filename.")


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python inspect_eml.py path/to/message.eml")

    path = sys.argv[1]
    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    print(f"Inspecting: {path}\n")
    dump_headers(msg)
    print()
    print(SEP)
    print("MIME STRUCTURE TREE")
    print(SEP)
    dump_structure(msg)
    print()
    dump_body_selection(msg)
    print()
    dump_attachments(msg)
    print()
    dump_multipart_warning(msg)


if __name__ == "__main__":
    main()