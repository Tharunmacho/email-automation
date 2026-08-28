"""Read a stored résumé again and replace the profile built from it.

Why this exists
---------------
A candidate's profile is only as good as the extraction that was available the
moment their email arrived. When the Veris résumé API could not be reached, the
pipeline fell back to a local heuristic parser and recorded that fact on the
record as ``additional_info.extraction_source = "heuristic_fallback"``. Those
profiles are visibly poorer — a designation of "SARAVANAN.A Role", a Projects
section holding a paragraph about languages, no skills, no dated employment
history — and nothing fixes itself, because the email has long since been filed
and will never be polled again.

The original file is kept, so the document can simply be read again. This walks
the candidates, re-runs the full parser over the stored bytes, and replaces the
profile *and* the verbatim payload behind it.

The stored upload is never modified, and neither is anything a person entered:
allocation, verdicts, and the identity records extracted from the same bundle
are all untouched.

    python scripts/reparse_candidates.py --dry-run
    python scripts/reparse_candidates.py                 # degraded profiles only
    python scripts/reparse_candidates.py --all
    python scripts/reparse_candidates.py --id 45389d48...
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.resume_parser import ResumeParser  # noqa: E402
from app.db.repository import CandidateRepository  # noqa: E402
from app.storage.factory import get_storage_backend  # noqa: E402

#: Extraction sources worth redoing. Anything that is not the résumé API is a
#: profile built from less than the document actually holds.
DEGRADED = {"heuristic_fallback", "", None}


def _source_of(record) -> str | None:
    info = getattr(record.profile, "additional_info", None) or {}
    return info.get("extraction_source")


def _load(record) -> bytes | None:
    """The original upload, from whichever backend is actually holding it."""
    key = record.resume.storage_key if record.resume else None
    if not key:
        return None
    declared = (record.resume.storage_backend or "").lower()
    for backend in (declared, "gridfs", "local"):
        if not backend:
            continue
        try:
            store = get_storage_backend(backend)
            if store.exists(key):
                return store.load(key)
        except Exception:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--all", action="store_true",
                        help="re-read every candidate, not only the degraded ones")
    parser.add_argument("--id", default="", help="one candidate id")
    args = parser.parse_args()

    repo = CandidateRepository()
    resume_parser = ResumeParser()

    if args.id:
        record = repo.get(args.id)
        records = [record] if record else []
        if not records:
            print(f"No candidate {args.id}")
            return 1
    else:
        records = repo.list_candidates(limit=1000)

    todo = [r for r in records if args.all or args.id or _source_of(r) in DEGRADED]
    print(f"{len(records)} candidate(s); {len(todo)} to re-read\n")

    fixed = unchanged = no_file = failed = 0

    for record in todo:
        who = record.profile.full_name or record.id
        before = _source_of(record)

        data = _load(record)
        if not data:
            no_file += 1
            print(f"  NO FILE   {who}: the original upload is not in storage")
            continue

        name = record.resume.original_filename if record.resume else "resume.pdf"
        try:
            profile, _extracted = resume_parser.parse_file(data, name)
        except Exception as exc:  # noqa: BLE001 — one bad file is not the run
            failed += 1
            print(f"  FAILED    {who}: {type(exc).__name__}: {exc}")
            continue

        after = (profile.additional_info or {}).get("extraction_source")
        if after in DEGRADED:
            unchanged += 1
            print(f"  STILL POOR {who}: {before} -> {after} (the API is still unreachable)")
            continue

        summary = (
            f"{after} | {profile.current_designation or 'no designation'} | "
            f"{len(profile.skills)} skill(s), {len(profile.work_experience)} job(s), "
            f"{len(profile.education)} qualification(s)"
        )
        if args.dry_run:
            print(f"  would fix {who}: {before} -> {summary}")
            fixed += 1
            continue

        if repo.replace_extraction(record.id, profile):
            fixed += 1
            print(f"  fixed     {who}: {before} -> {summary}")
        else:
            failed += 1
            print(f"  FAILED    {who}: the record could not be updated")

    print(
        f"\n{'would fix' if args.dry_run else 'fixed'}: {fixed}\n"
        f"still degraded: {unchanged}\n"
        f"original file missing: {no_file}\n"
        f"errors: {failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
