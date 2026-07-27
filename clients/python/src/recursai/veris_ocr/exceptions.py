class VerisOCRError(Exception):
    """Base exception class for Veris OCR SDK."""
    pass


class AuthenticationError(VerisOCRError):
    """Raised when authentication fails or API key is missing."""
    pass


class APIError(VerisOCRError):
    """Raised when the Veris OCR API returns a non-2xx status code."""
    def __init__(self, message: str, status_code: int, details: any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details

    def __str__(self):
        return f"{super().__str__()} (Status: {self.status_code}, Details: {self.details})"
