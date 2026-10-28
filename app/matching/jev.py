"""The one door to Jev, TypeSafe's System One model.

Jev answers typed questions about a state rather than generating text: a
`choice` returns one of your named options with probabilities, a `score`
returns a position on an ordered rubric, a `noul` returns a probability that a
statement is true. That is the whole reason this feature uses it — the result
is a number this code can act on, with no parsing step in which a model's
prose can become a decision nobody wrote.

Written against the documented API at https://docs.typesafe.ai:

    POST {base_url}/v1/systemone
    Authorization: Bearer <key>
    {"model": ..., "state": ..., "questions": {name: {...}}}
    -> {"model": ..., "answers": {name: {...}}, "usage": {...}}

Errors follow the documented codes: 400 bad request, 401 bad key, 413 body too
large, 415 bad encoding, 422 validation, 429 rate limited, 529 overloaded. The
vendor says to retry 429 and 529 with backoff and to respect `retry-after`, and
that is what `_RETRYABLE_STATUS` and `_sleep_for` below do. Nothing else is
retried: a 422 is a bug in the question we built, and retrying it only spends
the same money twice.

`urllib` rather than a client library, matching `app.staff_whatsapp`: the
payload is one small JSON object, and posting it is not a reason to take a
dependency.

The API key never leaves this module. It is read from settings, put in a
header, and nothing here logs a header, a request body or a key fragment.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: Documented input price, in US dollars per million input tokens. Output
#: tokens are free at the time of writing. Used only to report what a run cost;
#: nothing branches on it.
USD_PER_MILLION_INPUT_TOKENS = 0.042

_PATH = "/v1/systemone"

#: The statuses worth another attempt. 429 and 529 are the two the vendor names
#: explicitly; the 5xx family is here because a gateway hiccup is not a
#: statement about the request.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})


class JevNotConfigured(RuntimeError):
    """No API key. A configuration error, never a reason to score some other way."""


class JevError(RuntimeError):
    """A call that did not return answers.

    `status` is the HTTP status where there was one, and `retryable` says
    whether the attempt could have succeeded later — the runner records that on
    the result, so an evaluation that failed on a rate limit can be told apart
    from one whose request will never be accepted.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class JevResponse:
    """One answered request."""

    model: str
    answers: Dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        return self.input_tokens / 1_000_000 * USD_PER_MILLION_INPUT_TOKENS


# --------------------------------------------------------------------------- #
#  Metrics
#
#  A matching run spends real money and a recruiter waits on it, so both are
#  counted. `snapshot()` is what `GET /job-orders/matching/health` reports.
# --------------------------------------------------------------------------- #
@dataclass
class _Metrics:
    calls: int = 0
    failures: int = 0
    retries: int = 0
    rate_limited: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latencies: List[float] = field(default_factory=list)

    def observe(self, seconds: float) -> None:
        self.latencies.append(seconds)
        # Bounded: the p95 of the last thousand calls is the useful number, and
        # an unbounded list in a long-lived process is a leak.
        if len(self.latencies) > 1000:
            del self.latencies[: len(self.latencies) - 1000]


_METRICS = _Metrics()
_METRICS_LOCK = threading.Lock()


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return round(ordered[index], 3)


def snapshot() -> Dict[str, Any]:
    """Counters and latency percentiles since the process started."""
    with _METRICS_LOCK:
        latencies = list(_METRICS.latencies)
        return {
            "configured": is_configured(),
            "model": settings.jev_model,
            "calls": _METRICS.calls,
            "failures": _METRICS.failures,
            "retries": _METRICS.retries,
            "rate_limited": _METRICS.rate_limited,
            "input_tokens": _METRICS.input_tokens,
            "output_tokens": _METRICS.output_tokens,
            "estimated_cost_usd": round(
                _METRICS.input_tokens / 1_000_000 * USD_PER_MILLION_INPUT_TOKENS, 6
            ),
            "latency_p50_seconds": _percentile(latencies, 50),
            "latency_p95_seconds": _percentile(latencies, 95),
        }


def reset_metrics() -> None:
    """For tests, and for an operator who wants a clean reading of one run."""
    global _METRICS
    with _METRICS_LOCK:
        _METRICS = _Metrics()


# --------------------------------------------------------------------------- #
#  Concurrency
#
#  The account ceiling is tokens and requests per second, set per organisation
#  in the vendor console. A run fans out across a thread pool, so the cap has
#  to be process-wide rather than per-run: two recruiters pressing the button
#  at the same moment must not double the rate we offer the vendor.
# --------------------------------------------------------------------------- #
_SLOTS = threading.BoundedSemaphore(max(1, settings.jev_max_concurrency))


def is_configured() -> bool:
    return bool((settings.jev_api_key or "").strip())


def _sleep_for(attempt: int, retry_after: Optional[str]) -> float:
    """How long to wait before the next attempt.

    `retry-after` is the server saying when it will answer, so it wins over our
    own schedule whenever it is present and sane.
    """
    if retry_after:
        try:
            seconds = float(retry_after)
            if 0 <= seconds <= 60:
                return seconds
        except (TypeError, ValueError):
            pass
    return settings.jev_retry_backoff_seconds * (2 ** attempt)


def _detail_of(body: bytes) -> str:
    """The vendor explanation, without the request we sent it.

    A 422 body names the offending field; a 400 carries a `detail` string. Both
    are safe to log and to store on a result — they describe the question we
    built, not the candidate it was about.
    """
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — an unparseable error body is still an error
        return body[:200].decode("utf-8", "replace")
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            location = ".".join(str(part) for part in first.get("loc", []))
            return f"{location}: {first.get('msg', '')}".strip(": ")
    return str(detail) if detail else json.dumps(parsed)[:200]


def system_one(
    state: Any,
    questions: Dict[str, Any],
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> JevResponse:
    """Ask Jev one set of questions about one state.

    Raises `JevNotConfigured` when there is no key, and `JevError` when the
    call could not be completed. It never returns a partial or invented answer:
    the caller stores a failure and the recruiter sees that the evaluation did
    not happen, which is the only honest thing to show.
    """
    if not is_configured():
        raise JevNotConfigured(
            "JEV_API_KEY is not set. Matching cannot run without it, and no "
            "other provider is substituted for it."
        )
    if not questions:
        raise JevError("At least one question is required", status=None, retryable=False)

    payload = json.dumps(
        {"model": model or settings.jev_model, "state": state, "questions": questions},
        ensure_ascii=False,
        # The API rejects NaN and Infinity. Failing here gives our own stack
        # rather than a 400 whose cause is a float five layers down.
        allow_nan=False,
    ).encode("utf-8")

    url = f"{settings.jev_base_url.rstrip('/')}{_PATH}"
    attempts = max(1, settings.jev_max_retries)
    last: Optional[JevError] = None

    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {settings.jev_api_key.strip()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        started = time.monotonic()
        try:
            with _SLOTS:
                with urllib.request.urlopen(
                    request, timeout=timeout or settings.jev_timeout_seconds
                ) as response:
                    body = response.read()
            elapsed = time.monotonic() - started
            parsed = json.loads(body.decode("utf-8"))
            answers = parsed.get("answers")
            if not isinstance(answers, dict):
                raise JevError("Response carried no answers", status=200, retryable=False)
            usage = parsed.get("usage") or {}
            with _METRICS_LOCK:
                _METRICS.calls += 1
                _METRICS.input_tokens += int(usage.get("input_tokens") or 0)
                _METRICS.output_tokens += int(usage.get("output_tokens") or 0)
                _METRICS.observe(elapsed)
            return JevResponse(
                model=str(parsed.get("model") or model or settings.jev_model),
                answers=answers,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            )
        except urllib.error.HTTPError as exc:
            detail = _detail_of(exc.read() or b"")
            retryable = exc.code in _RETRYABLE_STATUS
            last = JevError(f"Jev returned {exc.code}: {detail}", status=exc.code, retryable=retryable)
            if exc.code == 429:
                with _METRICS_LOCK:
                    _METRICS.rate_limited += 1
            if not retryable or attempt == attempts - 1:
                break
            with _METRICS_LOCK:
                _METRICS.retries += 1
            time.sleep(_sleep_for(attempt, exc.headers.get("retry-after") if exc.headers else None))
        except JevError as exc:
            last = exc
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = JevError(f"Jev could not be reached: {exc}", status=None, retryable=True)
            if attempt == attempts - 1:
                break
            with _METRICS_LOCK:
                _METRICS.retries += 1
            time.sleep(_sleep_for(attempt, None))
        except ValueError as exc:  # includes json.JSONDecodeError
            last = JevError(f"Jev returned a body that is not JSON: {exc}", status=None, retryable=False)
            break

    with _METRICS_LOCK:
        _METRICS.failures += 1
    assert last is not None
    # Deliberately no request body, no state and no key: a log line about a
    # failed match must not become the place candidate evidence leaks to.
    log.warning("Jev call failed: status=%s retryable=%s", last.status, last.retryable)
    raise last
