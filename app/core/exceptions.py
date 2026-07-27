"""Domain exceptions — let the pipeline distinguish 'skip this' from 'real error'."""


class PipelineError(Exception):
    """Base class for all pipeline errors."""


class NotAResumeError(PipelineError):
    """The email/attachment was inspected and judged not to be a resume."""


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
