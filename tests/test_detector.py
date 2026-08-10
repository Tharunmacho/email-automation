from app.core.models import Attachment, EmailMessage
from app.ingestion.detector import detect


def _att(filename, mime="application/pdf"):
    return Attachment(filename=filename, mime_type=mime, size=1000, attachment_id="a1")


def test_resume_email_with_pdf_is_candidate():
    email = EmailMessage(
        message_id="1", thread_id="t1", from_addr="jane@example.com",
        subject="Application for Backend Engineer",
        attachments=[_att("Jane_Doe_Resume.pdf")],
    )
    result = detect(email)
    assert result.is_candidate
    assert result.resume_attachments


def test_no_attachment_is_not_candidate():
    email = EmailMessage(message_id="2", thread_id="t2", from_addr="jane@example.com",
                         subject="Hello", attachments=[])
    assert not detect(email).is_candidate


def test_noreply_sender_is_ignored():
    email = EmailMessage(
        message_id="3", thread_id="t3", from_addr="no-reply@bank.com",
        subject="Your statement", attachments=[_att("statement.pdf")],
    )
    assert not detect(email).is_candidate


def test_otp_style_email_scored_down():
    email = EmailMessage(
        message_id="4", thread_id="t4", from_addr="security@service.com",
        subject="Your OTP verification code", attachments=[_att("code.png", "image/png")],
    )
    # Promo/OTP subject drags score below threshold despite an image attachment.
    assert not detect(email).is_candidate


def test_non_resume_extension_ignored():
    email = EmailMessage(
        message_id="5", thread_id="t5", from_addr="jane@example.com",
        subject="Spreadsheet", attachments=[_att("data.xlsx", "application/vnd.ms-excel")],
    )
    assert not detect(email).is_candidate


def _mail(subject: str, filename: str, body: str = "") -> EmailMessage:
    return EmailMessage(
        message_id="m", thread_id="t", from_addr="someone@gmail.com",
        subject=subject, body_text=body,
        attachments=[Attachment(filename=filename, mime_type="application/pdf",
                                size=1000, attachment_id="a")],
    )


def test_any_document_is_opened_whatever_it_is_called():
    """Stage 1 no longer decides what a document is; it decides whether to look.

    These two used to assert the opposite, because a filename was the only
    evidence available and hall tickets were getting in. There is a page
    classifier now, so the safe move is to open the file and read it —
    `test_a_hall_ticket_is_refused_on_content` in test_resume_location.py is
    what actually keeps the hall ticket out.
    """
    assert detect(_mail("Fwd: docs", "OD Request Letter for Tharun V.pdf")).is_candidate is True
    assert detect(_mail("Notes", "Ethics HT.pdf", body="details")).is_candidate is True


def test_opaque_filenames_are_opened_rather_than_guessed_at():
    """`01.pdf` and `Scan_2026.pdf` are what candidates actually send."""
    for filename in ("01.pdf", "Scan_2026.pdf", "Doc.pdf", "IMG_20240103.pdf"):
        assert detect(_mail("Fwd:", filename)).is_candidate is True, filename


def test_a_resume_in_the_filename_still_gets_through():
    assert detect(_mail("Hi", "THARUN'S RESUME.pdf")).is_candidate is True


def test_a_resume_in_the_subject_still_gets_through():
    assert detect(_mail("Application for the developer role", "tharun.pdf")).is_candidate is True


# --------------------------------------------------------------------------- #
#  A filename is not a content judgement.
#
#  A real candidate was lost to these rules: a CV attached as
#  "Asif_mohd_MOTOR WORKSHOP ADMIN.pdf" under the subject "Resume" was discarded
#  because the blocklist matched "workshop" and "admin" — words in the man's job
#  title. Stage 1 is a *type* filter; deciding what a document is belongs to the
#  page classifier, which actually reads it.
# --------------------------------------------------------------------------- #
def test_a_job_title_in_the_filename_is_not_a_rejection():
    result = detect(_mail("Resume", "Asif_mohd_MOTOR WORKSHOP ADMIN.pdf"))
    assert result.is_candidate is True
    assert [a.filename for a in result.resume_attachments] == [
        "Asif_mohd_MOTOR WORKSHOP ADMIN.pdf"
    ]


def test_trade_job_titles_survive_the_filename_filter():
    for filename in (
        "Ramesh_WORKSHOP SUPERVISOR.pdf",
        "admin_assistant_kumar.pdf",
        "Site Guide Engineer - Suresh.pdf",
        "capstone_workshop_trainer_cv.pdf",
    ):
        assert detect(_mail("Job application", filename)).is_candidate is True, filename


def test_even_a_document_named_invoice_is_opened_not_assumed():
    """A candidate's CV has been attached as worse. Content decides, not the name."""
    assert detect(_mail("Fwd: docs", "tax invoice 2024.pdf")).is_candidate is True
    assert detect(_mail("Fwd: docs", "payment receipt.pdf")).is_candidate is True


def test_a_promotional_subject_still_blocks_without_opening_anything():
    """A judgement about the *email*, which is fair game — unlike the filename."""
    assert detect(_mail("Your invoice is ready", "statement.pdf")).is_candidate is False


def test_images_stay_strict_so_signature_logos_are_not_ocrd():
    email = EmailMessage(
        message_id="m", thread_id="t", from_addr="someone@gmail.com",
        subject="Application for the crane operator position",
        attachments=[Attachment(filename="logo.png", mime_type="image/png",
                                size=1000, attachment_id="a")],
    )
    assert detect(email).is_candidate is False


def test_the_reason_names_the_attachments_it_refused():
    """A poll that ingests nothing has to say what it actually saw."""
    email = EmailMessage(
        message_id="m", thread_id="t", from_addr="someone@gmail.com",
        subject="Hello",
        attachments=[Attachment(filename="notes.xlsx", mime_type="application/vnd.ms-excel",
                                size=1000, attachment_id="a")],
    )
    result = detect(email)
    assert result.is_candidate is False
    assert "notes.xlsx" in result.reason


def test_a_trade_application_mentioning_certificates_is_not_scored_down():
    """The promo list contains 'certificate'; trade applications say it a lot."""
    result = detect(_mail(
        "Application for Welder position - certificates attached",
        "Ramesh_Welder.pdf",
    ))
    assert result.is_candidate is True


def test_a_real_promotional_email_is_still_scored_down():
    assert detect(_mail("Your course completion certificate", "certificate.pdf")).is_candidate is False
    assert detect(_mail("Your invoice is ready", "statement.pdf")).is_candidate is False
