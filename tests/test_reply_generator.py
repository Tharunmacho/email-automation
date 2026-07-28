"""Unit tests for contextual email reply generator."""

from app.ai.reply_generator import generate_contextual_reply
from app.core.models import CandidateProfile, EmailMessage, WorkExperience


def test_generate_contextual_reply_software_developer():
    profile = CandidateProfile(
        full_name="Alice Developer",
        current_designation="Software Developer",
        skills=["Python", "FastAPI", "React", "PostgreSQL"],
        total_experience_years=4.0,
    )
    email = EmailMessage(
        message_id="msg_123",
        thread_id="thread_123",
        from_addr="alice@example.com",
        from_name="Alice Developer",
        subject="Application for Software Developer Position",
    )

    reply = generate_contextual_reply(profile, email)

    assert "Dear Alice Developer" in reply
    assert "Software Developer" in reply
    assert "Python" in reply or "FastAPI" in reply
    assert "4 years" in reply
    assert "Recruitment Team" in reply


def test_generate_contextual_reply_student_general_submission():
    profile = CandidateProfile(
        full_name="THARUN V",
        current_designation="Undergraduate Student in Computer Science Engineering",
        skills=["Node.js", "MongoDB", "Firebase.", "graduation Degree SIMATS", "2024-2028 ENGINEERING"],
    )

    reply = generate_contextual_reply(profile)

    # Name capitalized cleanly
    assert "Dear Tharun V" in reply
    # General submission handling (does NOT say "for the Undergraduate Student ... position")
    assert "sharing your resume with our recruitment team" in reply
    # Technical skills extracted without noise strings
    assert "Node.js" in reply
    assert "MongoDB" in reply
    assert "Firebase" in reply
    assert "SIMATS" not in reply
    assert "2024-2028" not in reply


def test_generate_contextual_reply_generic_candidate():
    profile = CandidateProfile(
        full_name="Charlie Applicant",
    )

    reply = generate_contextual_reply(profile)

    assert "Dear Charlie Applicant" in reply
    assert "evaluating your profile" in reply
