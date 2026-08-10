"""Why did stage 1 ignore this email? Read-only, mutates nothing.

The poll log can only say "no resume attachment detected", because the pipeline
returns before it has any per-attachment result to report. This prints what the
detector actually saw — every attachment, its extension, and the exact rule that
dropped it — so a real resume being ignored can be traced to the line that did it.

    python -m scripts.diagnose_detection
    python -m scripts.diagnose_detection --query "has:attachment"
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings                     # noqa: E402
from app.email_client import get_email_client       # noqa: E402
from app.ingestion import detector                  # noqa: E402


def explain_attachment(filename: str, size: int) -> str:
    """Why the real type filter kept or dropped this attachment.

    Note what is *not* here any more: no filename is read for meaning. Stage 1
    decides whether to open a file, never what it contains.
    """
    ext = os.path.splitext(filename)[1].lower()
    allowed = {e.lower() for e in settings.resume_extensions}

    if ext not in allowed:
        return f"DROPPED: extension '{ext}' is not in resume_extensions"
    if ext in detector._IMAGE_EXTS and size < settings.min_image_attachment_bytes:
        return (
            f"DROPPED: image of {size} bytes is below the "
            f"{settings.min_image_attachment_bytes}-byte floor (signature logo / icon)"
        )
    return "KEPT: will be opened and judged on its contents"


def _imap_search_all(client, limit: int) -> list:
    """Newest `limit` UIDs in the folder, read or unread. Changes no flags."""
    mail = client._connect_imap()
    try:
        mail.select(client.imap_folder)
        status, data = mail.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        uids = [u.decode() for u in reversed(data[0].split())]
        return uids[:limit]
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=None, help="override the configured search query")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument(
        "--all", action="store_true",
        help="IMAP only: search ALL instead of UNSEEN, to inspect messages a "
             "previous poll already marked read",
    )
    args = ap.parse_args()

    client = get_email_client()
    query = args.query if args.query is not None else settings.gmail_query
    print(f"provider           : {settings.email_provider}")
    print(f"query              : {query!r}")
    print(f"detector_min_score : {settings.detector_min_score}")
    print("-" * 78)

    if args.all and settings.email_provider == "smtp_imap":
        ids = _imap_search_all(client, args.limit)
    else:
        ids = client.search_message_ids(query=query)
    print(f"{len(ids)} message(s) matched\n")

    for mid in ids[: args.limit]:
        email = client.get_message(mid)
        print("=" * 78)
        print(f"message   : {mid}")
        print(f"from      : {email.from_name or ''} <{email.from_addr}>")
        print(f"subject   : {email.subject!r}")
        print(f"body      : {(email.body_text or '')[:160]!r}")
        print(f"attachments ({len(email.attachments)}):")
        for att in email.attachments:
            print(f"   - {att.filename!r}  ({att.mime_type}, {att.size} bytes)")
            print(f"       {explain_attachment(att.filename, att.size)}")
        if not email.attachments:
            print("   (none — the message carried no attachment parts at all)")

        result = detector.detect(email)
        print(f"\nverdict   : is_candidate={result.is_candidate} score={result.score} "
              f"(needs >= {settings.detector_min_score})")
        print(f"reason    : {result.reason}")
        print(f"kept      : {[a.filename for a in result.resume_attachments]}")

        if not result.is_candidate:
            subject_hit = bool(detector._RESUME_KEYWORDS.search(email.subject or ""))
            names = " ".join(a.filename for a in result.resume_attachments)
            name_hit = bool(detector._RESUME_KEYWORDS.search(names))
            body_hit = bool(detector._RESUME_KEYWORDS.search(email.body_text or ""))
            promo_hit = bool(detector._PROMO_SUBJECT.search(email.subject or ""))
            print("\nscore breakdown:")
            print(f"   +0.50 baseline (document attached) : {bool(result.resume_attachments)}")
            print(f"   +0.35 resume keyword in subject/filename : {subject_hit or name_hit}")
            print(f"   +0.10 resume keyword in body       : {body_hit}")
            print(f"   -0.40 promotional subject          : {promo_hit}")
        print()


if __name__ == "__main__":
    main()
