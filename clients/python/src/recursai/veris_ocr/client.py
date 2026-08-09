import os
import requests
from .exceptions import AuthenticationError, APIError, VerisOCRError
from .models import PassportResult, DocumentResult, ResumeResult


class VerisOCR:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 180.0):
        self.api_key = api_key or os.environ.get("VERIS_OCR_API_KEY")
        self.base_url = base_url or os.environ.get("VERIS_OCR_BASE_URL") or "https://veris.recursai.in"
        self.timeout = timeout
        
        if not self.api_key:
            raise AuthenticationError(
                "API Key is required. Please pass api_key to the constructor or set VERIS_OCR_API_KEY environment variable."
            )
            
        self.base_url = self.base_url.rstrip("/")
        self._session = None
        
        # Initialize namespaces
        self.passport = PassportNamespace(self)
        self.document = DocumentNamespace(self)
        self.resume = ResumeNamespace(self)
        
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "X-API-KEY": self.api_key,
            })
        return self._session

    def __enter__(self) -> "VerisOCR":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def request(self, endpoint: str, file_path: str, data: dict | None = None, field_name: str = "file", timeout: float | None = None) -> dict:
        if not os.path.exists(file_path):
            raise VerisOCRError(f"File not found at: {file_path}")
            
        url = f"{self.base_url}{endpoint}"
        req_timeout = timeout if timeout is not None else self.timeout
        
        try:
            filename = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                files = {field_name: (filename, f)}
                response = self.session.post(url, files=files, data=data, timeout=req_timeout)
        except IOError as e:
            raise VerisOCRError(f"Failed to read file {file_path}: {e}")
        except requests.RequestException as e:
            raise APIError(f"Network or connection error during request: {e}", 500)

        if response.status_code in (401, 403):
            raise AuthenticationError(f"Authentication failed: {response.reason}. Please verify your API Key.")
            
        if not (200 <= response.status_code < 300):
            try:
                details = response.json()
            except ValueError:
                details = response.text
            raise APIError(
                f"API request failed with status {response.status_code}: {response.reason}",
                response.status_code,
                details
            )
            
        try:
            return response.json()
        except ValueError as e:
            raise APIError(f"Failed to parse response JSON: {e}", response.status_code, response.text)


class PassportNamespace:
    def __init__(self, client: VerisOCR):
        self.client = client

    def extract(self, file_path: str) -> PassportResult:
        data = self.client.request("/v1/passport/extract", file_path, field_name="image")
        return PassportResult.from_dict(data)


class DocumentNamespace:
    def __init__(self, client: VerisOCR):
        self.client = client

    def extract(self, file_path: str, lang: str | None = None) -> DocumentResult:
        payload = {}
        if lang:
            payload["lang"] = lang
        data = self.client.request("/v1/document/extract", file_path, payload)
        return DocumentResult.from_dict(data)


class ResumeNamespace:
    def __init__(self, client: VerisOCR):
        self.client = client

    def extract(self, file_path: str) -> ResumeResult:
        data = self.client.request("/v1/resume/extract", file_path, field_name="image")
        return ResumeResult.from_dict(data)
