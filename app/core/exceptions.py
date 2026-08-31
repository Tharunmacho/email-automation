"""Domain exceptions — let the pipeline distinguish 'skip this' from 'real error'."""


class PipelineError(Exception):
    """Base class for all pipeline errors."""


class NotAResumeError(PipelineError):
    """The email/attachment was inspected and judged not to be a resume."""


class ForeignNationalityError(PipelineError):
    """The CV was read and belongs to someone who is not an Indian national.

    A permanent refusal, not a failure: the document was understood perfectly
    well. Kept apart from `NotAResumeError` because "this is not a CV" and "this
    is a CV we do not recruit from" are different answers to a recruiter asking
    why a candidate never appeared, and only one of them means the reader was
    wrong about the file.

    Carries the verdict so the refusal can name the country it read.
    """

    def __init__(self, message: str, verdict: "object | None" = None):
        super().__init__(message)
        self.verdict = verdict


class UnsupportedFileTypeError(PipelineError):
    """The attachment type is not one we know how to extract text from."""


class TextExtractionError(PipelineError):
    """Text could not be extracted from the document (even with OCR)."""


class AIParseError(PipelineError):
    """The AI failed to return usable structured data."""


class DuplicateCandidateError(PipelineError):
    """A candidate matching this resume already exists.

    Carries the id of the existing candidate and the reason it matched.
    """

    def __init__(self, existing_id: str, reason: str):
        self.existing_id = existing_id
        self.reason = reason
        super().__init__(f"Duplicate candidate ({reason}); existing id={existing_id}")
