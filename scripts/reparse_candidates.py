"""Re-run extraction for candidates whose resume file is still on disk.

Sends each stored resume back through the Veris OCR/LLM endpoint and rewrites
the profile with the current mapper. Existing profiles are written to a JSON
backup first, so a bad run can be rolled back.

    python scripts/reparse_candidates.py            # dry run, shows the diff
    python scripts/reparse_candidates.py --apply    # write to MongoDB
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai.resume_parser import map_veris_to_profile  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.models import CandidateProfile  # noqa: E402
from app.db.mongo import get_candidates_collection  # noqa: E402
from app.db.repository import CandidateRepository  # noqa: E402

RESUME_ROOT = Path("data/resumes")

# Fields worth counting when reporting what changed.
LIST_FIELDS = [
    "skills", "technical_skills", "languages", "certifications",
    "achievements", "education", "projects", "work_experience",
]
SCALAR_FIELDS = [
    "full_name", "email", "phone", "location",
    "current_designation", "current_company", "total_experience_years",
]


def _merge(before: CandidateProfile, after: CandidateProfile) -> CandidateProfile:
    """Union the two profiles so a re-parse can only ever add information.

    The extractor is not guaranteed to return everything it returned last time
    (a different OCR pass, a section it no longer detects), so replacing
    wholesale would silently delete real data the operator already has.
    """
    merged = after.model_copy(deep=True)

    for field in LIST_FIELDS:
        old_items = list(getattr(before, field, []) or [])
        new_items = list(getattr(merged, field, []) or [])
        seen = set()
        combined = []
        for item in new_items + old_items:
            fingerprint = json.dumps(
                item.model_dump() if hasattr(item, "model_dump") else item,
                sort_keys=True, default=str,
            ).lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            combined.append(item)
        setattr(merged, field, combined)

    # Keep an old scalar when the new pass came back empty.
    for field in SCALAR_FIELDS:
        if getattr(merged, field, None) in (None, "") and getattr(before, field, None):
            setattr(merged, field, getattr(before, field))

    combined_info = dict(before.additional_info or {})
    combined_info.update(after.additional_info or {})
    merged.additional_info = combined_info or None
    return merged


def _populated(profile: CandidateProfile) -> int:
    """How many fields actually carry data — the headline quality signal."""
    n = sum(1 for f in SCALAR_FIELDS if getattr(profile, f, None) not in (None, ""))
    n += sum(1 for f in LIST_FIELDS if getattr(profile, f, None))
    if profile.additional_info:
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write results to MongoDB")
    args = ap.parse_args()

    if not settings.veris_ocr_api_key:
        print("VERIS_OCR_API_KEY is not set — nothing to re-parse with.")
        return 1

    coll = get_candidates_collection()
    repo = CandidateRepository()
    docs = list(coll.find({}))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = Path(f"data/profile-backup-{stamp}.json")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(
        json.dumps(
            {d["_id"]: d.get("profile") for d in docs}, indent=2, default=str, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    print(f"backed up {len(docs)} profile(s) -> {backup}\n")

    from recursai.veris_ocr import VerisOCR

    updated = skipped = failed = 0
    print(f"{'candidate':26} {'before':>7} {'after':>7}   result")
    print("-" * 78)

    for d in docs:
        resume = d.get("resume") or {}
        key = resume.get("storage_key")
        name = (d.get("profile") or {}).get("full_name") or "—"
        path = RESUME_ROOT / key if key else None

        if not path or not path.exists():
            print(f"{name[:24]:26} {'':>7} {'':>7}   skipped — file missing")
            skipped += 1
            continue

        before = CandidateProfile.model_validate(d.get("profile") or {})
        try:
            with VerisOCR(
                api_key=settings.veris_ocr_api_key, base_url=settings.veris_ocr_base_url
            ) as client:
                res = client.resume.extract(str(path.resolve()))
            after = map_veris_to_profile(res, veris_text="")
        except Exception as exc:  # noqa: BLE001
            print(f"{name[:24]:26} {'':>7} {'':>7}   FAILED — {type(exc).__name__}: {exc}")
            failed += 1
            continue

        if not (after.full_name or after.email or after.phone):
            print(f"{name[:24]:26} {'':>7} {'':>7}   skipped — extraction returned nothing usable")
            skipped += 1
            continue

        after = _merge(before, after)

        b, a = _populated(before), _populated(after)
        gained = [f for f in LIST_FIELDS if not getattr(before, f) and getattr(after, f)]
        lost = [f for f in LIST_FIELDS if getattr(before, f) and not getattr(after, f)]

        note = ""
        if gained:
            note += " +" + ",".join(gained)
        if lost:
            note += " -" + ",".join(lost)

        if args.apply:
            repo.update_profile(d["_id"], after)
            updated += 1
            print(f"{(after.full_name or name)[:24]:26} {b:>7} {a:>7}   updated{note}")
        else:
            print(f"{(after.full_name or name)[:24]:26} {b:>7} {a:>7}   would update{note}")

    print("-" * 78)
    verb = "updated" if args.apply else "would update"
    print(f"{verb}: {updated if args.apply else len(docs) - skipped - failed}   skipped: {skipped}   failed: {failed}")
    if not args.apply:
        print("\ndry run — re-run with --apply to write these to MongoDB")
    else:
        print(f"\nrollback: restore profiles from {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
