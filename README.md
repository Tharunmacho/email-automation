# Veris OCR SDK

Official Python SDK for the Veris OCR API.

Set your API credentials before using the SDK:

```bash
export VERIS_OCR_BASE_URL=https://veris.recursai.in
export VERIS_OCR_API_KEY=pk_live_...
```

## Python

Install [`recursai-veris-ocr`](clients/python/README.md):

```bash
python -m pip install recursai-veris-ocr
```

```python
from recursai.veris_ocr import VerisOCR

with VerisOCR() as client:
    passport = client.passport.extract("passport.jpg")
    print(passport.mrz.passport_number, passport.mrz.expiry_date)

    document = client.document.extract("invoice.pdf", lang="eng+fra")
    print(document.page_count, document.pages[0].text)

    resume = client.resume.extract("resume.pdf")
    print(resume.name, resume.total_experience_human, resume.skills)
```

Requires Python 3.10–3.14.
