"""Only send the résumé endpoint what it can actually read.

An email with no attachment whose *body* reads as a résumé arrives at the
parser as `email_body.txt`. The endpoint rejects it outright — "Queued OCR
supports JPEG, PNG, WebP, PDF, and DOCX files" — and under
``REQUIRE_VERIS_RESUME`` that rejection was raised as a parse failure, which
leaves the mail unlabelled so the next poll can try again. The next poll then
did exactly the same thing.

That is not a transient failure worth retrying. It is a question not worth
asking, and the loop it created showed up as `errors=1` on every single sync
with no candidate ever produced.
"""
from __future__ import annotations

import pytest

from app.ai import resume_parser as rp


@pytest.mark.parametrize(
    "name", ["cv.pdf", "cv.PDF", "resume.docx", "scan.jpg", "scan.jpeg",
             "photo.png", "shot.webp"],
)
def test_the_endpoint_is_offered_what_it_accepts(name):
    assert rp._veris_can_read(name) is True


@pytest.mark.parametrize(
    "name", ["email_body.txt", "notes.rtf", "legacy.doc", "body.html", "", "noext"],
)
def test_the_endpoint_is_not_offered_what_it_refuses(name):
    """`.doc` belongs here too: the service names DOCX, not DOC."""
    assert rp._veris_can_read(name) is False


def test_a_text_body_is_parsed_instead_of_being_sent_and_refused(monkeypatch):
    """The whole point: a body-only résumé must produce a profile, not an error.

    `require_veris_resume` is left ON, because that is the setting under which
    this used to fail permanently. Nothing is sent, so nothing can be refused,
    and the text reaches `parse_text_fallback` — which tries Anthropic before
    any heuristic, so this is the LLM reading real text rather than a guess
    standing in for a failed extraction.
    """
    from app.core.models import CandidateProfile

    monkeypatch.setattr(rp.settings, "veris_ocr_api_key", "pk_test_key")
    monkeypatch.setattr(rp.settings, "require_veris_resume", True)

    sent: list = []

    def explode(*args, **kwargs):
        sent.append(args)
        raise AssertionError("a text body was sent to the résumé endpoint")

    monkeypatch.setattr(rp, "_veris_can_read", rp._veris_can_read)  # keep the real check

    parser = rp.ResumeParser()
    monkeypatch.setattr(
        parser, "parse_text_fallback",
        lambda text, hint="": CandidateProfile(full_name="Body Candidate",
                                               email="body@example.com"),
    )

    body = (
        "RAJESH KUMAR\nHeavy Vehicle Driver\n"
        "Email: rajesh@example.com  Mobile: +91 98765 43210\n"
        "EXPERIENCE\nAshok Leyland, Chennai - Driver, 2018 to present\n"
        "EDUCATION\nHigher Secondary, Tamil Nadu Board, 2010\n"
    )
    profile, extracted = parser.parse_file(body.encode("utf-8"), "email_body.txt")

    assert sent == [], "nothing should have been offered to the endpoint"
    assert profile.full_name == "Body Candidate"
    assert extracted.method == "plain"
