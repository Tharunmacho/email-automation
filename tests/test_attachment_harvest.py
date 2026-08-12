"""Stage 1 must never lose a résumé over what a file is called.

Every case here is one that used to end in `no resume-type attachment` on mail
that plainly carried an application:

  * Gmail hands back small parts as inline base64 with no `attachmentId`, and
    requiring one dropped every attachment under ~50 KB.
  * Forwards and phone clients send parts with no filename at all.
  * A 40 KB image floor discarded re-compressed phone scans of CVs.
  * An unrecognised extension — or none — was treated as a rejection rather
    than as an absence of information.

The rule the tests encode: a filename is a hint, never a verdict. What a
document *is* is decided by reading it, downstream.
"""
from __future__ import annotations

import base64
import email.message

from app.config import settings
from app.core.models import Attachment, EmailMessage
from app.email_client.smtp_imap_client import SMTPIMAPClient
from app.gmail.client import GmailClient
from app.ingestion.detector import detect
from tests.test_page_classifier import CERTIFICATE_PAGE, RESUME_PAGE


def gmail_client() -> GmailClient:
    """A client with no Google credentials — none of this touches the network."""
    return object.__new__(GmailClient)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def att(filename: str, mime: str = "application/pdf", size: int = 100_000) -> Attachment:
    return Attachment(filename=filename, mime_type=mime, size=size, attachment_id="a1")


def mail(**kw) -> EmailMessage:
    base = dict(
        message_id="m1", thread_id="t1", from_addr="candidate@gmail.com",
        subject="Fwd:", body_text="", attachments=[],
    )
    base.update(kw)
    return EmailMessage(**base)


# --------------------------------------------------------------------------- #
#  Gmail: inline parts and missing names
# --------------------------------------------------------------------------- #
def test_a_small_inline_attachment_is_collected():
    """Under ~50 KB Gmail returns the bytes inline and mints no attachment id."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(b"my cv is attached")}},
            {
                "mimeType": "application/pdf",
                "filename": "01.pdf",
                "body": {"data": b64(b"%PDF-1.4 tiny"), "size": 17},
            },
        ],
    }

    found = gmail_client()._collect_attachments(payload)

    assert [a.filename for a in found] == ["01.pdf"]
    # The bytes are already here, so nothing needs downloading later.
    assert found[0].data == b"%PDF-1.4 tiny"
    # Size is the decoded length, which is what the image floor is judged on —
    # not Gmail's base64-inflated figure.
    assert found[0].size == len(b"%PDF-1.4 tiny")


def test_a_large_attachment_is_still_collected_by_id():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [{
            "mimeType": "application/pdf",
            "filename": "Scan_2026.pdf",
            "body": {"attachmentId": "ANGjdJ_x", "size": 2_400_000},
        }],
    }

    found = gmail_client()._collect_attachments(payload)

    assert found[0].attachment_id == "ANGjdJ_x"
    assert found[0].data is None          # fetched on demand
    assert found[0].size == 2_400_000


def test_a_filename_is_recovered_from_content_disposition():
    payload = {
        "mimeType": "application/pdf",
        "filename": "",
        "headers": [
            {"name": "Content-Disposition", "value": 'attachment; filename="OD Request.pdf"'},
        ],
        "body": {"attachmentId": "x1", "size": 90_000},
    }

    found = gmail_client()._collect_attachments(payload)

    assert found[0].filename == "OD Request.pdf"


def test_a_percent_encoded_filename_is_decoded():
    payload = {
        "mimeType": "application/pdf",
        "filename": "",
        "headers": [{
            "name": "Content-Disposition",
            "value": "attachment; filename*=UTF-8''resume%20final.pdf",
        }],
        "body": {"attachmentId": "x1", "size": 90_000},
    }

    found = gmail_client()._collect_attachments(payload)

    assert found[0].filename == "resume final.pdf"


def test_an_unnamed_part_is_named_from_its_mime_type():
    """No filename anywhere is not a reason to discard the bytes."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "application/pdf", "body": {"attachmentId": "x1", "size": 80_000}},
            {"mimeType": "image/jpeg", "body": {"attachmentId": "x2", "size": 60_000}},
        ],
    }

    found = gmail_client()._collect_attachments(payload)

    assert [a.filename for a in found] == ["document_1.pdf", "document_2.jpg"]


def test_the_message_body_is_not_mistaken_for_an_attachment():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(b"hello")}},
            {"mimeType": "text/html", "body": {"data": b64(b"<p>hello</p>")}},
        ],
    }

    assert gmail_client()._collect_attachments(payload) == []


def test_an_explicitly_attached_text_file_is_kept():
    """…but a .txt the sender actually attached is an attachment."""
    payload = {
        "mimeType": "text/plain",
        "filename": "resume.txt",
        "headers": [{"name": "Content-Disposition", "value": "attachment"}],
        "body": {"data": b64(b"Curriculum Vitae")},
    }

    found = gmail_client()._collect_attachments(payload)

    assert [a.filename for a in found] == ["resume.txt"]


def test_a_corrupt_inline_part_does_not_lose_the_rest_of_the_message():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "application/pdf", "filename": "broken.pdf",
             "body": {"data": "!!!not base64!!!"}},
            {"mimeType": "application/pdf", "filename": "good.pdf",
             "body": {"data": b64(b"%PDF-1.4 fine")}},
        ],
    }

    found = gmail_client()._collect_attachments(payload)

    assert [a.filename for a in found] == ["good.pdf"]


# --------------------------------------------------------------------------- #
#  IMAP
# --------------------------------------------------------------------------- #
def imap_client() -> SMTPIMAPClient:
    return object.__new__(SMTPIMAPClient)


def test_imap_captures_an_inline_image_with_no_filename():
    """A CV pasted in as an inline image carries no disposition and no name."""
    msg = email.message.EmailMessage()
    msg["Subject"] = "Fwd:"
    msg.set_content("see below")
    msg.add_attachment(b"\xff\xd8\xff\xe0 jpeg bytes", maintype="image", subtype="jpeg")
    # Strip what a well-behaved client would have sent, leaving the bare part.
    part = msg.get_payload()[1]
    del part["Content-Disposition"]

    body, attachments = imap_client()._parse_mime_parts("42", msg)

    assert "see below" in body
    assert len(attachments) == 1
    assert attachments[0].filename.endswith(".jpg")
    assert attachments[0].data == b"\xff\xd8\xff\xe0 jpeg bytes"


def test_imap_keeps_a_named_attachment_and_the_body_apart():
    msg = email.message.EmailMessage()
    msg.set_content("My resume is attached.")
    msg.add_attachment(
        b"%PDF-1.4 cv", maintype="application", subtype="pdf", filename="random_name.pdf",
    )

    body, attachments = imap_client()._parse_mime_parts("43", msg)

    assert body.strip() == "My resume is attached."
    assert [a.filename for a in attachments] == ["random_name.pdf"]


def test_imap_drops_an_empty_part():
    msg = email.message.EmailMessage()
    msg.set_content("hi")
    msg.add_attachment(b"", maintype="application", subtype="pdf", filename="empty.pdf")

    _body, attachments = imap_client()._parse_mime_parts("44", msg)

    assert attachments == []


# --------------------------------------------------------------------------- #
#  The detector: no filename is ever a verdict
# --------------------------------------------------------------------------- #
def test_meaningless_filenames_are_all_admitted():
    for name in (
        "01.pdf", "Scan_2026.pdf", "Doc.pdf", "OD Request.pdf",
        "random_name.pdf", "document_1.pdf", "IMG-20260812-WA0007.pdf",
    ):
        result = detect(mail(attachments=[att(name)]))
        assert result.is_candidate, f"{name} was rejected on its name alone"


def test_an_unknown_extension_survives_on_its_mime_type():
    """`.bin` is what several clients call a PDF they could not identify."""
    result = detect(mail(attachments=[att("attachment", mime="application/pdf")]))

    assert result.is_candidate
    assert [a.filename for a in result.resume_attachments] == ["attachment"]


def test_a_genuinely_unopenable_attachment_is_still_refused():
    """Filename-agnostic is not type-blind — a .zip is not a document."""
    result = detect(mail(attachments=[att("photos.zip", mime="application/zip")]))

    assert not result.is_candidate
    assert "no resume-type attachment" in result.reason


# --------------------------------------------------------------------------- #
#  The 2 KB image floor
# --------------------------------------------------------------------------- #
def test_a_compressed_phone_scan_clears_the_image_floor():
    """A re-compressed photo of a CV lands well under the old 40 KB floor."""
    assert settings.min_image_attachment_bytes == 2_000

    result = detect(mail(attachments=[att("image.png", mime="image/png", size=18_000)]))

    assert result.is_candidate


def test_a_signature_logo_is_still_ignored():
    result = detect(mail(attachments=[att("logo.png", mime="image/png", size=900)]))

    assert not result.is_candidate
    assert "no resume-type attachment" in result.reason


def test_the_floor_is_judged_on_type_not_extension():
    """An unnamed inline icon is screened on size just like a named one."""
    tiny = att("document_1", mime="image/png", size=300)

    assert not detect(mail(attachments=[tiny])).is_candidate


# --------------------------------------------------------------------------- #
#  Email body fallback
# --------------------------------------------------------------------------- #
def test_a_resume_pasted_into_the_body_is_ingested():
    result = detect(mail(subject="Application", body_text=RESUME_PAGE))

    assert result.is_candidate
    assert len(result.resume_attachments) == 1
    body_att = result.resume_attachments[0]
    assert body_att.filename == "email_body.txt"
    # Carries its own bytes, so the pipeline needs no special case for it.
    assert b"EOT Crane Operator" in body_att.data


def test_a_covering_note_is_not_a_resume():
    result = detect(mail(body_text="Hi, please find my resume attached. Thanks!"))

    assert not result.is_candidate


def test_a_body_that_is_not_a_resume_is_not_ingested():
    result = detect(mail(body_text=CERTIFICATE_PAGE * 3))

    assert not result.is_candidate


def test_a_cloud_link_is_reported_rather_than_fetched():
    """We never pull a stranger's Drive link — but we say it was there."""
    result = detect(mail(
        subject="My CV",
        body_text="My resume: https://drive.google.com/file/d/1AbCdEf/view thanks",
    ))

    assert not result.is_candidate
    assert "drive.google.com" in result.reason


def test_the_body_fallback_respects_the_sender_ignore_list():
    result = detect(mail(from_addr="no-reply@jobboard.com", body_text=RESUME_PAGE))

    assert not result.is_candidate
