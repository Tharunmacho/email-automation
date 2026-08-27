"""See what the passport filter makes of a real document, without sending it.

    python scripts/check_passport_nationality.py path/to/scan.pdf
    python scripts/check_passport_nationality.py *.pdf *.jpg
    python scripts/check_passport_nationality.py bundle.pdf --text     # dump OCR
    python scripts/check_passport_nationality.py bundle.pdf --json

Runs the exact code path the ingestion pipeline runs — the same text extraction,
the same page classifier, the same nationality verdict — and prints, per page,
which endpoint the page would be sent to and why. Nothing is uploaded and
nothing is written to the database, so this is safe to point at anything.

When a passport of yours is held back and you disagree, run it with ``--text``:
the verdict is only ever as good as the text layer, and nine times out of ten a
surprising verdict is a page Tesseract read as mush.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

# Allow running as `python scripts/check_passport_nationality.py` from the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings                      # noqa: E402
from app.extraction import page_classifier as pc     # noqa: E402
from app.extraction import passport_nationality as pn  # noqa: E402
from app.extraction.text_extractor import extract_text  # noqa: E402

_KIND_LABEL = {
    pc.RESUME: "resume",
    pc.AADHAAR: "aadhaar",
    pc.PASSPORT: "passport",
    pc.ID_DOCUMENT: "id document",
    pc.CERTIFICATE: "certificate",
    pc.EXPERIENCE_LETTER: "experience letter",
    pc.OTHER: "other",
    pc.UNKNOWN: "unknown",
    pc.BLANK: "blank",
}


def _route_of(page: int, result: pc.MultipassClassification) -> str:
    if page in result.resume_pages:
        return "-> /v1/jobs mode=resume"
    if page in result.aadhaar_pages:
        return "-> /v1/jobs mode=aadhaar"
    if page in result.passport_pages:
        return "-> /v1/jobs mode=passport"
    if page in result.foreign_passport_pages:
        return "   HELD BACK (not an Indian passport)"
    return "   ignored"


def inspect(path: Path, *, show_text: bool) -> dict:
    data = path.read_bytes()
    doc = extract_text(data, path.name)
    texts: List[str] = [page.text for page in doc.pages] or [doc.text]
    result = pc.classify_multipass(texts)

    print("=" * 78)
    print(f"  {path.name}   ({len(texts)} page(s), extraction={doc.method})")
    print("=" * 78)

    for page in result.pages:
        number = page.page_number
        kind = _KIND_LABEL.get(page.kind, page.kind)
        print(f"  page {number:>3}  {kind:<18} {_route_of(number, result)}")

        verdict = result.passport_nationality.get(number)
        if verdict is not None:
            print(f"           nationality : {verdict.verdict.upper()}  ({verdict.country})")
            print(f"           confidence  : {verdict.confidence:.2f}")
            if verdict.mrz.issuing_state or verdict.mrz.nationality:
                print(
                    f"           MRZ         : issuer={verdict.mrz.issuing_state or '-'}  "
                    f"nationality={verdict.mrz.nationality or '-'}  "
                    f"({verdict.mrz.line_count} MRZ line(s) found)"
                )
            else:
                print("           MRZ         : none readable on this page")
            for line in verdict.evidence:
                print(f"           evidence    : {line}")
            if verdict.scores:
                ranked = sorted(verdict.scores.items(), key=lambda kv: -kv[1])[:4]
                print(
                    "           scores      : "
                    + ", ".join(f"{pn.country_name(c)}={s}" for c, s in ranked)
                )

        if show_text:
            body = (texts[number - 1] if number - 1 < len(texts) else "").strip()
            print("           ----- text layer -----")
            for line in body.splitlines()[:40]:
                print(f"           | {line}")
            if not body:
                print("           | (empty — nothing was read off this page)")
            print("           ----------------------")

    print()
    print(f"  resume pages            : {result.resume_pages or '-'}")
    print(f"  aadhaar -> veris        : {result.aadhaar_pages or '-'}")
    print(f"  passport -> veris       : {result.passport_pages or '-'}")
    print(f"  passport HELD BACK      : {result.foreign_passport_pages or '-'}")
    print()

    return {
        "file": path.name,
        "resume_pages": result.resume_pages,
        "aadhaar_pages": result.aadhaar_pages,
        "passport_sent": result.passport_pages,
        "passport_held": result.foreign_passport_pages,
        "passports": result.nationality_report(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="PDF or image files to inspect")
    parser.add_argument("--text", action="store_true", help="dump the extracted text layer")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args()

    print()
    print(
        f"  policy: india_only={settings.passport_india_only}  "
        f"allow_undetermined={settings.passport_allow_undetermined_nationality}"
    )
    print()

    summaries = []
    for raw in args.paths:
        path = Path(raw)
        if not path.is_file():
            print(f"  !! not a file: {path}")
            continue
        try:
            summaries.append(inspect(path, show_text=args.text))
        except Exception as exc:  # noqa: BLE001 — a bad file must not stop the batch
            print(f"  !! {path.name}: {type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(summaries, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
