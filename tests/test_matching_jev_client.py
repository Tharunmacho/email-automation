"""The Jev HTTP client: wire format, retries, failure modes and secrecy.

No network. `urllib.request.urlopen` is replaced with a scripted transport, so
every assertion is about what this code sends and how it reacts — the parts
that are ours. The request shape is checked against the documented one:

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <key>
    {"model", "state", "questions"}
"""
from __future__ import annotations

import io
import json
import logging
import urllib.error
from email.message import Message
from unittest.mock import patch

import pytest

from app.config import settings
from app.matching import jev

KEY = "test-key-never-logged-7f3a"

ANSWERS = {
    "model": "jev-1.14.0",
    "answers": {"fit": {"type": "noul", "noul": 0.93}},
    "usage": {"input_tokens": 2000, "output_tokens": 5},
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: dict, headers: dict | None = None) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api.typesafe.ai/v1/systemone", code, "error", message,
        io.BytesIO(json.dumps(body).encode()),
    )


class Transport:
    """Plays back a script of responses and exceptions, recording each request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": json.loads(request.data.decode()),
            "timeout": timeout,
        })
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return _Response(json.dumps(step).encode())


@pytest.fixture(autouse=True)
def configured():
    jev.reset_metrics()
    with patch.object(settings, "jev_api_key", KEY), \
         patch.object(settings, "jev_max_retries", 3), \
         patch("app.matching.jev.time.sleep") as sleep:
        yield sleep


def test_the_request_is_the_documented_shape():
    transport = Transport(ANSWERS)
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        response = jev.system_one({"candidate": {"skills": ["TIG"]}},
                                  {"fit": {"type": "noul", "instructions": "Fits"}})

    sent = transport.requests[0]
    assert sent["method"] == "POST"
    assert sent["url"] == "https://api.typesafe.ai/v1/systemone"
    assert sent["headers"]["authorization"] == f"Bearer {KEY}"
    assert sent["headers"]["content-type"] == "application/json"
    assert set(sent["body"]) == {"model", "state", "questions"}
    assert sent["body"]["model"] == settings.jev_model
    assert sent["timeout"] == settings.jev_timeout_seconds
    assert response.answers["fit"]["noul"] == 0.93
    assert response.model == "jev-1.14.0"
    assert response.input_tokens == 2000


def test_cost_is_reported_from_billed_input_tokens():
    transport = Transport(ANSWERS)
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        response = jev.system_one("s", {"q": {"type": "noul", "instructions": "x"}})
    assert response.cost_usd == pytest.approx(2000 / 1_000_000 * 0.042)
    snap = jev.snapshot()
    assert snap["calls"] == 1 and snap["input_tokens"] == 2000
    assert snap["estimated_cost_usd"] == pytest.approx(0.000084)


def test_without_a_key_it_refuses_and_names_the_setting():
    with patch.object(settings, "jev_api_key", ""):
        with pytest.raises(jev.JevNotConfigured, match="JEV_API_KEY"):
            jev.system_one("s", {"q": {"type": "noul", "instructions": "x"}})


def test_a_rate_limit_is_retried_honouring_retry_after(configured):
    transport = Transport(_http_error(429, {"detail": "slow down"}, {"retry-after": "2"}), ANSWERS)
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        response = jev.system_one("s", {"q": {"type": "noul", "instructions": "x"}})
    assert response.answers
    assert len(transport.requests) == 2
    configured.assert_called_once_with(2.0)
    assert jev.snapshot()["rate_limited"] == 1


def test_overload_backs_off_exponentially(configured):
    transport = Transport(_http_error(529, {}), _http_error(529, {}), ANSWERS)
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        jev.system_one("s", {"q": {"type": "noul", "instructions": "x"}})
    waits = [call.args[0] for call in configured.call_args_list]
    assert waits == [settings.jev_retry_backoff_seconds, settings.jev_retry_backoff_seconds * 2]


def test_a_validation_error_is_not_retried():
    """A 422 is a malformed question. Sending it again only pays twice."""
    body = {"detail": [{"loc": ["body", "questions", "q", "criteria"], "msg": "field required"}]}
    transport = Transport(_http_error(422, body))
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        with pytest.raises(jev.JevError) as caught:
            jev.system_one("s", {"q": {"type": "choice", "instructions": "x"}})
    assert len(transport.requests) == 1
    assert caught.value.status == 422 and not caught.value.retryable
    assert "questions.q.criteria" in str(caught.value)


def test_a_bad_key_is_not_retried():
    transport = Transport(_http_error(401, {"detail": "invalid api key"}))
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        with pytest.raises(jev.JevError) as caught:
            jev.system_one("s", {"q": {"type": "noul", "instructions": "x"}})
    assert caught.value.status == 401 and len(transport.requests) == 1


def test_timeouts_are_bounded_and_end_in_a_retryable_failure():
    transport = Transport(TimeoutError("read timed out"), TimeoutError("again"), TimeoutError("again"))
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        with pytest.raises(jev.JevError) as caught:
            jev.system_one("s", {"q": {"type": "noul", "instructions": "x"}})
    assert len(transport.requests) == settings.jev_max_retries
    assert caught.value.retryable
    assert jev.snapshot()["failures"] == 1


def test_a_response_without_answers_is_an_error_not_an_empty_result():
    transport = Transport({"model": "jev-1.14.0", "usage": {}})
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        with pytest.raises(jev.JevError, match="no answers"):
            jev.system_one("s", {"q": {"type": "noul", "instructions": "x"}})


def test_non_finite_numbers_are_refused_before_sending():
    transport = Transport(ANSWERS)
    with patch("app.matching.jev.urllib.request.urlopen", transport):
        with pytest.raises(ValueError):
            jev.system_one({"years": float("nan")}, {"q": {"type": "noul", "instructions": "x"}})
    assert transport.requests == []


def test_the_key_never_reaches_the_log(caplog):
    transport = Transport(_http_error(500, {"detail": "boom"}), _http_error(500, {}), _http_error(500, {}))
    with caplog.at_level(logging.DEBUG), \
         patch("app.matching.jev.urllib.request.urlopen", transport):
        with pytest.raises(jev.JevError):
            jev.system_one({"candidate": {"skills": ["secret-skill"]}},
                           {"q": {"type": "noul", "instructions": "x"}})
    assert KEY not in caplog.text
    # Nor the state: a failed match must not be how evidence leaks to logs.
    assert "secret-skill" not in caplog.text
    assert KEY not in json.dumps(jev.snapshot())
