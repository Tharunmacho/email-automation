"""Command-line entrypoint.

    python -m app.cli init-db          # create MongoDB indexes
    python -m app.cli auth             # run Gmail OAuth consent once
    python -m app.cli run-once         # poll Gmail once and ingest
    python -m app.cli watch --interval 60
    python -m app.cli parse-file resume.pdf   # test extraction+AI on a local file
    python -m app.cli stats            # quick counts
"""
from __future__ import annotations

from pathlib import Path

import typer

from app.logging_config import get_logger

app = typer.Typer(add_completion=False, help="Resume ingestion pipeline CLI")
log = get_logger("cli")


@app.command("init-db")
def init_db() -> None:
    """Create MongoDB indexes (idempotent)."""
    from app.db.mongo import ensure_indexes

    ensure_indexes()
    typer.echo("MongoDB indexes ensured.")


@app.command("auth")
def auth() -> None:
    """Run the Gmail OAuth consent flow and cache the token."""
    from app.gmail.auth import get_credentials

    get_credentials()
    typer.echo("Gmail authorised. Token cached.")


@app.command("run-once")
def run_once(query: str = typer.Option(None, help="Override the Gmail search query")) -> None:
    """Poll Gmail once, ingest every resume found."""
    from app.db.mongo import ensure_indexes
    from app.ingestion.runner import IngestionRunner

    ensure_indexes()
    summary = IngestionRunner().run_once(query=query)
    typer.echo(
        f"fetched={summary.fetched} processed={summary.processed} "
        f"skipped={summary.skipped} errors={summary.errors} "
        f"new_candidates={summary.ingested_candidates}"
    )


@app.command("watch")
def watch(
    interval: int = typer.Option(60, help="Seconds between Gmail polls"),
    query: str = typer.Option(None, help="Override the Gmail search query"),
) -> None:
    """Continuously poll Gmail on an interval."""
    from app.db.mongo import ensure_indexes
    from app.ingestion.runner import IngestionRunner

    ensure_indexes()
    IngestionRunner().watch(interval_seconds=interval, query=query)


@app.command("parse-file")
def parse_file(path: Path) -> None:
    """Extract text + run the AI parser on a LOCAL file (no Gmail, no DB write).

    Useful for validating extraction/OCR/AI without wiring up Gmail or Mongo.
    """
    from app.ai.resume_parser import ResumeParser
    from app.extraction.text_extractor import extract_text

    data = path.read_bytes()
    extracted = extract_text(data, path.name)
    typer.echo(
        f"[extraction] method={extracted.method} ocr={extracted.ocr_used} "
        f"pages={extracted.page_count} chars={extracted.char_count}"
    )
    for page in extracted.pages:
        marker = "*" if page.page_number in extracted.resume_pages else " "
        typer.echo(
            f"  {marker} page {page.page_number:>3}  {page.kind:<18} "
            f"score={page.score:>6.2f}  {len(page.text.strip()):>6} chars"
        )
    typer.echo(
        f"[classification] is_resume={extracted.is_resume} "
        f"confidence={extracted.classification_confidence} "
        f"resume_pages={extracted.resume_pages or 'none'} — {extracted.classification_reason}"
    )
    if extracted.is_resume is False:
        typer.secho("Not a resume; nothing sent to the AI parser.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    typer.echo(
        f"[ai payload] {len(extracted.resume_text)} of {len(extracted.text)} chars"
    )
    profile = ResumeParser().parse(extracted.resume_text)
    typer.echo(profile.model_dump_json(indent=2))


@app.command("selftest")
def selftest() -> None:
    """Prove the storage path end-to-end against YOUR MongoDB (no Gmail/AI needed).

    Generates a sample resume PDF, runs it through the real pipeline (extraction →
    GridFS/file storage → Mongo insert), reads it back byte-for-byte, verifies the
    structured fields, checks duplicate detection, then deletes the test data.
    Only the AI step is stubbed with a fixed profile so no Anthropic key is required.
    """
    import fitz

    from app.core.models import Attachment, CandidateProfile, EmailMessage
    from app.db.mongo import ensure_indexes, get_candidates_collection
    from app.ingestion.pipeline import IngestionPipeline
    from app.storage.factory import get_storage_backend

    def ok(msg):   typer.secho(f"  PASS  {msg}", fg=typer.colors.GREEN)
    def fail(msg): typer.secho(f"  FAIL  {msg}", fg=typer.colors.RED); raise typer.Exit(1)

    typer.echo("Resume pipeline self-test\n" + "-" * 40)

    # 1. Connectivity + indexes
    try:
        ensure_indexes()
        ok("connected to MongoDB and ensured indexes")
    except Exception as exc:  # noqa: BLE001
        fail(f"MongoDB connection/index failed: {exc}")

    # 1.5. Optional Anthropic API Check
    from app.config import settings
    if settings.anthropic_api_key and not settings.anthropic_api_key.startswith("sk-ant-xxxxxxxxxx"):
        try:
            from app.ai.resume_parser import ResumeParser
            parser = ResumeParser()
            # Send a tiny message to check API connectivity/key validity
            parser.client.messages.create(
                model=settings.anthropic_model,
                max_tokens=10,
                messages=[{"role": "user", "content": "respond with 'OK'"}],
            )
            ok("connected to Anthropic API successfully")
        except Exception as exc:
            fail(f"Anthropic API key check failed: {exc}")
    else:
        typer.secho("  INFO  No Anthropic API key configured; skipping AI connection check.", fg=typer.colors.YELLOW)

    # 2. Build a realistic sample resume PDF in memory
    doc = fitz.open()
    page = doc.new_page()
    lines = [
        "John Selftest",
        "Email: john.selftest@example.com",
        "Phone: +1 415 555 0134",
        "Location: San Francisco, CA",
        "Skills: Python, MongoDB, FastAPI, Docker",
        "Experience: Senior Backend Engineer at ExampleCorp (2020-2025)",
        "Education: B.S. Computer Science, Example University",
    ]
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=12)
        y += 22
    pdf_bytes = doc.tobytes()
    doc.close()
    ok(f"generated sample resume PDF ({len(pdf_bytes)} bytes)")

    # 3. Stub the AI parser with a fixed profile (no Anthropic key needed here)
    class _StubParser:
        def parse(self, text, hint=""):
            assert "john.selftest@example.com" in text, "extracted text missing expected content"
            return CandidateProfile(
                is_resume=True, confidence=0.97,
                full_name="John Selftest", email="john.selftest@example.com",
                phone="+1 415 555 0134", location="San Francisco, CA",
                skills=["Python", "MongoDB", "FastAPI", "Docker"],
                current_company="ExampleCorp", current_designation="Senior Backend Engineer",
            )

    pipeline = IngestionPipeline(parser=_StubParser())
    storage = get_storage_backend()
    ok(f"storage backend in use: {storage.name}")

    att = Attachment(filename="John_Selftest_Resume.pdf", mime_type="application/pdf",
                     size=len(pdf_bytes), attachment_id="selftest", data=pdf_bytes)
    email = EmailMessage(message_id="selftest-msg-1", thread_id="selftest-thread",
                         from_addr="john.selftest@example.com", from_name="John Selftest",
                         subject="Application for Backend Engineer", attachments=[att])

    coll = get_candidates_collection()
    created_ids: list[str] = []
    try:
        # 4. First ingest — should store the record + file
        result = pipeline.process_email(email)
        if result.status != "processed" or not result.ingested_ids:
            fail(f"expected a stored candidate, got status={result.status} ({result.reason})")
        cid = result.ingested_ids[0]
        created_ids.append(cid)
        ok(f"ingested candidate id={cid}")

        # 5. Verify the stored record's key/value fields
        record = pipeline.repo.get(cid)
        p = record.profile
        checks = {
            "full_name": p.full_name == "John Selftest",
            "email": p.email == "john.selftest@example.com",
            "skills": p.skills == ["Python", "MongoDB", "FastAPI", "Docker"],
            "email_key (dedup)": record.email_key == "john.selftest@example.com",
            "phone_key (dedup)": record.phone_key == "4155550134",
            "extraction method": record.resume.extraction_method == "pdf_text",
        }
        for name, passed in checks.items():
            (ok if passed else (lambda m: fail(m)))(f"stored field verified: {name}")

        # 6. Read the original file back and compare bytes
        loaded = storage.load(record.resume.storage_key)
        if loaded == pdf_bytes:
            ok(f"original file retrieved byte-for-byte from '{storage.name}' ({len(loaded)} bytes)")
        else:
            fail(f"retrieved file differs (got {len(loaded)} bytes, expected {len(pdf_bytes)})")

        # 7. Duplicate detection — same file, different message id
        email2 = email.model_copy(update={"message_id": "selftest-msg-2"})
        email2.attachments = [att.model_copy(update={"data": pdf_bytes})]
        dup_result = pipeline.process_email(email2)
        statuses = [a.status for a in dup_result.attachments]
        if "duplicate" in statuses and not dup_result.ingested_ids:
            ok("duplicate resume correctly detected (no second copy stored)")
        else:
            fail(f"expected duplicate detection, got {statuses}")

        if coll.count_documents({"resume_hash": record.resume_hash}) == 1:
            ok("exactly one candidate exists for the resume hash")
        else:
            fail("duplicate created a second candidate document")

        typer.secho("\nALL CHECKS PASSED — your setup stores resumes correctly.",
                    fg=typer.colors.GREEN, bold=True)
    finally:
        # 8. Clean up test data
        for cid in created_ids:
            r = pipeline.repo.get(cid)
            if r:
                try:
                    storage.delete(r.resume.storage_key)
                except Exception:  # noqa: BLE001
                    pass
            coll.delete_one({"_id": cid})
        coll.delete_many({"source_email.message_id": {"$in": ["selftest-msg-1", "selftest-msg-2"]}})
        typer.echo("(cleaned up self-test data)")


@app.command("stats")
def stats() -> None:
    """Show quick candidate counts."""
    from app.db.repository import CandidateRepository

    typer.echo(f"candidates in DB: {CandidateRepository().count()}")


@app.command("reply-existing")
def reply_existing(
    force: bool = typer.Option(False, "--force", "-f", help="Re-send auto reply even if candidate was already marked replied")
) -> None:
    """Send contextual auto-replies to candidates already stored in MongoDB."""
    from app.ai.reply_generator import generate_contextual_reply
    from app.core.models import EmailMessage
    from app.db.repository import CandidateRepository
    from app.email_client import get_email_client

    repo = CandidateRepository()
    candidates = repo.list_candidates(limit=1000)
    if not candidates:
        typer.echo("No candidate records found in MongoDB.")
        return

    gmail = get_email_client()
    sent_count = 0
    skipped_count = 0

    for cand in candidates:
        if getattr(cand, "auto_reply_sent", False) and not force:
            skipped_count += 1
            continue

        source = cand.source_email
        if not source or not source.from_addr:
            log.warning("Candidate %s has no valid source email address. Skipping.", cand.id)
            skipped_count += 1
            continue

        mock_email = EmailMessage(
            message_id=source.message_id,
            thread_id=source.thread_id,
            from_addr=source.from_addr,
            from_name=source.from_name,
            subject=source.subject,
            date=source.received_date,
        )

        reply_body = generate_contextual_reply(cand.profile, mock_email)
        try:
            gmail.send_reply(
                message_id=source.message_id,
                thread_id=source.thread_id,
                to_addr=source.from_addr,
                subject=source.subject,
                body_text=reply_body,
            )
            repo.mark_auto_reply_sent(cand.id)
            sent_count += 1
            typer.echo(f"  [SENT] Candidate {cand.id} ({cand.profile.full_name or source.from_addr})")
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"  [FAILED] Candidate {cand.id}: {exc}", fg=typer.colors.RED)

    typer.echo(f"Done. Sent={sent_count}, Skipped={skipped_count}, Total={len(candidates)}")


if __name__ == "__main__":
    app()
