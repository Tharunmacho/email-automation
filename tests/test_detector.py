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
