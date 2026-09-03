"""Local OCR must not return an empty page just because the host was busy.

The failure these pin down is not a bad scan — it is a read that never
finished. On a container where Tesseract contends for a core, every pass in the
ladder hits `ocr_page_timeout_seconds` and returns "", the page arrives at the
classifier as `0 chars`, and an identity document on it is filed as a blank
sheet. The same bundle read twice gave different pages, which is how a race
announces itself.

Three things answer it, and each is tested here: a ceiling on how many
Tesseract processes exist at once, a per-invocation budget that scales with the
size of the page it is given, and a last rung that reads a failed page *smaller*
rather than giving up on it.
"""
from __future__ import annotations

import threading

import pytest

from app.config import settings
from app.extraction import local_ocr

pytest.importorskip("PIL", reason="Pillow is needed to build test pages")
from PIL import Image  # noqa: E402


def page(width: int, height: int | None = None):
    return Image.new("L", (width, height or int(width * 1.414)), color=255)


# --------------------------------------------------------------------------- #
#  The per-invocation budget
# --------------------------------------------------------------------------- #

def test_a_bigger_page_is_given_proportionally_longer(monkeypatch):
    """45s was calibrated on a 300dpi A4; the 450dpi re-read is 2.25x the pixels."""
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 45.0)

    a4_300 = local_ocr._page_timeout(page(2481, 3507))
    a4_450 = local_ocr._page_timeout(page(3722, 5260))

    assert a4_300 == pytest.approx(45.0, rel=0.05)
    assert a4_450 > a4_300 * 2, (
        "the escalation pass got the same clock as the page a quarter its size, "
        "which made the rescue pass the one most likely to time out"
    )


def test_a_small_page_never_gets_less_than_the_configured_budget(monkeypatch):
    """Scaling is upward only; a thumbnail keeps the full allowance."""
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 45.0)

    assert local_ocr._page_timeout(page(400)) == pytest.approx(45.0)


def test_the_budget_is_capped(monkeypatch):
    """Past 4x the page is not slow, it is unreadable; the document budget owns it."""
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 45.0)

    assert local_ocr._page_timeout(page(30000, 30000)) == pytest.approx(180.0)


def test_no_limit_stays_no_limit(monkeypatch):
    """0 means unlimited to pytesseract, and scaling must not make it a number."""
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 0)

    assert local_ocr._page_timeout(page(2481, 3507)) == 0


# --------------------------------------------------------------------------- #
#  Admission control
# --------------------------------------------------------------------------- #

def test_the_slot_count_honours_an_explicit_setting(monkeypatch):
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 3)
    assert local_ocr.ocr_slot_count() == 3


def test_the_slot_count_is_sized_from_the_cpus_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 0)
    monkeypatch.setattr(local_ocr, "available_cpus", lambda: 6)
    assert local_ocr.ocr_slot_count() == 6


def test_a_one_cpu_container_still_gets_two_slots(monkeypatch):
    """A page read waits on rasterisation and a subprocess, not only on CPU."""
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 0)
    monkeypatch.setattr(local_ocr, "available_cpus", lambda: 1)
    assert local_ocr.ocr_slot_count() == 2


def test_no_more_than_the_ceiling_run_at_once(monkeypatch):
    """The whole point: two pools multiplying cannot exceed one shared bound."""
    monkeypatch.setattr(local_ocr, "_SLOTS", None)
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 3)

    peak = 0
    live = 0
    lock = threading.Lock()
    start = threading.Barrier(12, timeout=10)

    def worker():
        nonlocal peak, live
        start.wait()
        with local_ocr._engine_slot():
            with lock:
                live += 1
                peak = max(peak, live)
            threading.Event().wait(0.02)
            with lock:
                live -= 1

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert peak <= 3, f"{peak} Tesseract processes ran at once against a ceiling of 3"
    monkeypatch.setattr(local_ocr, "_SLOTS", None)


def test_a_slot_is_released_even_when_the_read_raises(monkeypatch):
    """A leaked slot would wedge every later page in the process."""
    monkeypatch.setattr(local_ocr, "_SLOTS", None)
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 1)

    with pytest.raises(RuntimeError):
        with local_ocr._engine_slot():
            raise RuntimeError("tesseract blew up")

    with local_ocr._engine_slot():
        pass  # would block forever if the slot had leaked

    monkeypatch.setattr(local_ocr, "_SLOTS", None)


# --------------------------------------------------------------------------- #
#  Reading a failed page smaller
# --------------------------------------------------------------------------- #

def test_a_page_that_times_out_is_retried_smaller(monkeypatch):
    """The rescue: fewer pixels is the only move that makes a read finish."""
    monkeypatch.setattr(settings, "ocr_rescue_enabled", True)
    seen: list[int] = []

    def fake_read(image, psm, seconds=None):
        seen.append(image.width)
        # Times out at full size, exactly as a contended host does.
        return "" if image.width > 2000 else "PASSPORT Republic of India"

    monkeypatch.setattr(local_ocr, "_tesseract_read", fake_read)

    text = local_ocr._rescue_read(page(4000), page_number=20)

    assert "PASSPORT" in text, "the page stayed empty; the passport is still lost"
    # Only the shrunken reads happen here — the full-size attempt already
    # failed in `read_image`, which is what sent the page down this path.
    assert seen and max(seen) < 4000, f"nothing smaller than the page was tried: {seen}"
    assert min(seen) >= local_ocr._MIN_READABLE_WIDTH


def test_the_rescue_never_shrinks_below_what_tesseract_can_segment(monkeypatch):
    """Past the minimum readable width a retry is a slower way of returning ''."""
    monkeypatch.setattr(settings, "ocr_rescue_enabled", True)
    monkeypatch.setattr(local_ocr, "_tesseract_read", lambda image, psm, seconds=None: "")

    assert local_ocr._rescue_read(page(local_ocr._MIN_READABLE_WIDTH), 1) == ""


def test_the_rescue_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "ocr_rescue_enabled", False)
    called = []

    def fake_read(image, psm, seconds=None):
        called.append(1)
        return ""

    monkeypatch.setattr(local_ocr, "_tesseract_read", fake_read)

    assert local_ocr._rescue_read(page(4000), 1) == ""
    assert not called, "the rescue ran while disabled"


def test_read_image_falls_back_to_the_rescue_when_everything_returns_empty(monkeypatch):
    """End to end through the ladder: an empty page comes back with text."""
    monkeypatch.setattr(settings, "ocr_rescue_enabled", True)
    monkeypatch.setattr(settings, "ocr_detect_rotation", False)
    monkeypatch.setattr(local_ocr, "_read_with_secondary", lambda image: "")

    def fake_read(image, psm, seconds=None):
        return "" if image.width > 2000 else "Republic of India Passport"

    monkeypatch.setattr(local_ocr, "_tesseract_read", fake_read)

    read = local_ocr.read_image(page(4000), dpi=300, page_number=20)

    assert read.text.strip(), "read_image returned an empty page"
    assert read.engine == "tesseract:rescue"


def test_a_page_that_reads_fine_never_reaches_the_rescue(monkeypatch):
    """The common case must not pay for the failure case."""
    monkeypatch.setattr(settings, "ocr_detect_rotation", False)
    monkeypatch.setattr(
        local_ocr, "_tesseract_read",
        lambda image, psm, seconds=None: (
            "Curriculum Vitae of a perfectly legible candidate here"
        ),
    )
    rescued = []

    def fake_rescue(*args, **kwargs):
        rescued.append(1)
        return ""

    monkeypatch.setattr(local_ocr, "_rescue_read", fake_rescue)

    local_ocr.read_image(page(4000), dpi=300, page_number=1)

    assert not rescued


def test_a_thread_may_take_the_slot_it_already_holds(monkeypatch):
    """The page-level slot wraps the pass-level ones; one ceiling must not
    deadlock against itself on the single-CPU container it exists to protect."""
    monkeypatch.setattr(local_ocr, "_SLOTS", None)
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 1)

    with local_ocr._engine_slot():
        with local_ocr._engine_slot():
            with local_ocr._engine_slot():
                pass

    # And it is genuinely released once the outermost exits.
    with local_ocr._engine_slot():
        pass

    monkeypatch.setattr(local_ocr, "_SLOTS", None)


def test_nesting_does_not_release_the_slot_early(monkeypatch):
    """An inner exit must not free the slot the outer scope still holds."""
    monkeypatch.setattr(local_ocr, "_SLOTS", None)
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 1)

    taken_by_other_thread = threading.Event()

    def other():
        if local_ocr._engine_slots().acquire(timeout=0.3):
            taken_by_other_thread.set()
            local_ocr._engine_slots().release()

    with local_ocr._engine_slot():
        with local_ocr._engine_slot():
            pass
        # Inner scope has exited; the slot must still be ours.
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=5)

    assert not taken_by_other_thread.is_set(), (
        "the slot was released while an outer scope still held it"
    )
    monkeypatch.setattr(local_ocr, "_SLOTS", None)


def test_an_impossible_budget_is_reported_before_the_pages_are_lost(monkeypatch, caplog):
    """90s for a 28-page scan is arithmetic, not an accident. Say so up front."""
    import logging

    # The production pair: 90s for the document, 15s for any one page.
    monkeypatch.setattr(settings, "ocr_document_budget_seconds", 90.0)
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 15.0)
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 2)
    monkeypatch.setattr(settings, "ocr_local_workers", 2)
    monkeypatch.setattr(settings, "ocr_max_pages", 300)
    monkeypatch.setattr(
        local_ocr, "_read_pdf_page",
        lambda data, number, deadline=None: local_ocr.PageRead(
            number, "text", 300, "test", 50.0
        ),
    )

    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for _ in range(28):
        doc.new_page()
    pdf = doc.tobytes()
    doc.close()

    with caplog.at_level(logging.WARNING, logger="app.extraction.local_ocr"):
        local_ocr.ocr_pdf_page_reads(pdf, filename="8 Saravanan - Full Docs.pdf")

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("OCR_DOCUMENT_BUDGET_SECONDS" in w for w in warnings), (
        f"an unachievable budget passed unremarked: {warnings}"
    )


def test_a_workable_budget_is_not_complained_about(monkeypatch, caplog):
    """The warning must not cry wolf on a document that fits."""
    import logging

    monkeypatch.setattr(settings, "ocr_document_budget_seconds", 600.0)
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 45.0)
    monkeypatch.setattr(settings, "ocr_max_concurrent_pages", 4)
    monkeypatch.setattr(settings, "ocr_local_workers", 4)
    monkeypatch.setattr(settings, "ocr_max_pages", 300)
    monkeypatch.setattr(
        local_ocr, "_read_pdf_page",
        lambda data, number, deadline=None: local_ocr.PageRead(
            number, "text", 300, "test", 50.0
        ),
    )

    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    for _ in range(4):
        doc.new_page()
    pdf = doc.tobytes()
    doc.close()

    with caplog.at_level(logging.WARNING, logger="app.extraction.local_ocr"):
        local_ocr.ocr_pdf_page_reads(pdf, filename="small.pdf")

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("OCR_DOCUMENT_BUDGET_SECONDS" in w for w in warnings), warnings


def test_the_rescue_is_not_given_less_time_than_the_read_it_rescues(monkeypatch):
    """Measured: a 3400x4400 page got 2.2s and its own rescue got 1.3s.

    `_page_timeout` scales upward with pixels, so shrinking the page shrank its
    allowance too — handing the retry less wall clock than the attempt it exists
    to rescue. A quarter of the work in the same seconds is the point; a quarter
    of both is just a second failure.
    """
    monkeypatch.setattr(settings, "ocr_rescue_enabled", True)
    monkeypatch.setattr(settings, "ocr_page_timeout_seconds", 10.0)
    budgets: list[float] = []

    def fake_read(image, psm, seconds=None):
        budgets.append(seconds if seconds is not None else local_ocr._page_timeout(image))
        return ""

    monkeypatch.setattr(local_ocr, "_tesseract_read", fake_read)

    big = page(4000, 5200)
    granted_to_the_original = local_ocr._page_timeout(big)
    local_ocr._rescue_read(big, page_number=20)

    assert budgets, "the rescue never read anything"
    assert min(budgets) >= granted_to_the_original, (
        f"the rescue got {min(budgets)}s against the {granted_to_the_original}s "
        "the read it is rescuing was given"
    )
