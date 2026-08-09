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


def test_an_attachment_alone_is_not_a_candidate_email():
    """The score for merely having a document attached is the old cut-off, so
    every hall ticket, OD letter and class schedule in the mailbox qualified."""
    result = detect(_mail("Fwd: docs", "OD Request Letter for Tharun V.pdf"))
    assert result.is_candidate is False


def test_a_body_keyword_alone_is_still_not_enough():
    result = detect(_mail("Notes", "Ethics HT.pdf", body="please find the details"))
    assert result.is_candidate is False


def test_a_resume_in_the_filename_still_gets_through():
    assert detect(_mail("Hi", "THARUN'S RESUME.pdf")).is_candidate is True


def test_a_resume_in_the_subject_still_gets_through():
    assert detect(_mail("Application for the developer role", "tharun.pdf")).is_candidate is True
