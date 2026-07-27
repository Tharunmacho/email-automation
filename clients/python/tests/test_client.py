import os
import unittest
from unittest.mock import patch, MagicMock
import tempfile

from recursai.veris_ocr import VerisOCR, AuthenticationError, APIError, VerisOCRError


class TestVerisOCR(unittest.TestCase):
    def setUp(self):
        # Create a temporary file to use as a dummy file for tests
        self.temp_file = tempfile.NamedTemporaryFile(delete=False)
        self.temp_file.write(b"dummy file content")
        self.temp_file.close()

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.unlink(self.temp_file.name)

    def test_missing_api_key(self):
        # Temporarily clear env var
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AuthenticationError):
                VerisOCR()

    def test_init_with_params(self):
        client = VerisOCR(api_key="custom_key", base_url="https://custom.api")
        self.assertEqual(client.api_key, "custom_key")
        self.assertEqual(client.base_url, "https://custom.api")

    @patch("requests.Session.post")
    def test_passport_extraction(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "mrz": {
                "passport_number": "L898902C",
                "expiry_date": "2030-01-01"
            }
        }
        mock_post.return_value = mock_response

        client = VerisOCR(api_key="test_key", base_url="https://test.api")
        result = client.passport.extract(self.temp_file.name)

        self.assertEqual(result.mrz.passport_number, "L898902C")
        self.assertEqual(result.mrz.expiry_date, "2030-01-01")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://test.api/v1/passport/extract")
        self.assertIn("image", kwargs["files"])

    @patch("requests.Session.post")
    def test_document_extraction_with_lang(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "page_count": 1,
            "pages": [{"text": "Invoice text"}]
        }
        mock_post.return_value = mock_response

        client = VerisOCR(api_key="test_key", base_url="https://test.api")
        result = client.document.extract(self.temp_file.name, lang="eng+fra")

        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.pages[0].text, "Invoice text")
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"], {"lang": "eng+fra"})

    @patch("requests.Session.post")
    def test_resume_extraction(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "name": "Jane Doe",
            "total_experience_human": "3 years",
            "skills": ["Python", "Flask"]
        }
        mock_post.return_value = mock_response

        with VerisOCR(api_key="test_key", base_url="https://test.api") as client:
            result = client.resume.extract(self.temp_file.name)
            self.assertEqual(result.name, "Jane Doe")
            self.assertEqual(result.skills, ["Python", "Flask"])

    @patch("requests.Session.post")
    def test_api_error_handling(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.reason = "Bad Request"
        mock_response.json.return_value = {"error": "Invalid format"}
        mock_post.return_value = mock_response

        client = VerisOCR(api_key="test_key")
        with self.assertRaises(APIError) as ctx:
            client.resume.extract(self.temp_file.name)
        
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.details, {"error": "Invalid format"})

    def test_nonexistent_file(self):
        client = VerisOCR(api_key="test_key")
        with self.assertRaises(VerisOCRError):
            client.resume.extract("nonexistent_file_path.pdf")
