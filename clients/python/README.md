# recursai-veris-ocr

Official Python SDK for the Veris OCR API.

## Installation

```bash
python -m pip install recursai-veris-ocr
```

## Configuration

Set your API credentials before initializing the client:

```bash
export VERIS_OCR_BASE_URL=https://veris.recursai.in
export VERIS_OCR_API_KEY=pk_live_...
```

Alternatively, pass them directly to the constructor:

```python
from recursai.veris_ocr import VerisOCR

client = VerisOCR(
    api_key="pk_live_...",
    base_url="https://veris.recursai.in"
)
```

## Usage

You can use the client normally or as a context manager for automatic connection pooling cleanup:

```python
from recursai.veris_ocr import VerisOCR

with VerisOCR() as client:
    # Passport Extraction
    passport = client.passport.extract("passport.jpg")
    print(passport.mrz.passport_number, passport.mrz.expiry_date)

    # Document Extraction
    document = client.document.extract("invoice.pdf", lang="eng+fra")
    print(document.page_count, document.pages[0].text)

    # Resume Extraction
    resume = client.resume.extract("resume.pdf")
    print(resume.name, resume.total_experience_human, resume.skills)
```

## Error Handling

The SDK exposes custom exceptions for clean error management:

```python
from recursai.veris_ocr import VerisOCR, AuthenticationError, APIError, VerisOCRError

try:
    with VerisOCR() as client:
        resume = client.resume.extract("resume.pdf")
except AuthenticationError:
    print("Invalid API Key configured.")
except APIError as e:
    print(f"API returned status {e.status_code}: {e.details}")
except VerisOCRError as e:
    print(f"General SDK error: {e}")
```
