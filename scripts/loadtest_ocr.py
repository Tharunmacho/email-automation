"""Push N resumes through the OCR path at once and report where the time went.

    # 50 resumes, the real Veris service, files from a folder
    python scripts/loadtest_ocr.py --count 50 --dir M:/samples

    # the same, but against a simulated service — no API spend, tunes the knobs
    python scripts/loadtest_ocr.py --count 50 --simulate 8

    # sweep the in-flight cap to find where the service stops keeping up
    python scripts/loadtest_ocr.py --count 50 --simulate 8 --sweep 4,8,16,24,32

Reports, per run: throughput in resumes/second, the p50/p95/max of a whole
extraction, and — the number that tells you what to change — how long a
submission spent waiting for a free in-flight slot.

Reading the result:

* **queue_wait p95 high, total p95 close to the service's own time.**
  We are the bottleneck. Raise `veris_max_inflight_jobs`.
* **queue_wait p95 ~0, total p95 high.** Veris is the bottleneck. Raising the
  cap will make its queue longer, not our answers faster.
* **total p95 far above the service's own extraction time with an empty
  queue_wait.** The polling is the cost. Lower
  `ocr_job_fast_poll_interval_seconds` or raise `ocr_job_fast_poll_seconds`.

`--simulate` replaces the Veris client with one that sleeps for the given number
of seconds and then reports success. Everything else — the gateway, the cap, the
polling curve, the threads — is the real code, so it is the right way to size
the knobs before spending money on a real run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import random
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings                        # noqa: E402
from app.extraction import ocr_gateway                 # noqa: E402
from app.extraction.jobs import (                      # noqa: E402
    MODE_RESUME,
    QUEUED,
    SUCCEEDED,
    JobHandle,
    JobOutcome,
)


# --------------------------------------------------------------------------- #
#  A stand-in for Veris, so the knobs can be sized without spending anything
# --------------------------------------------------------------------------- #
class SimulatedVeris:
    """Accepts instantly, finishes after `duration` (±20%), reports success.

    Deliberately not instant: a service that answers immediately hides exactly
    the queueing behaviour this harness exists to measure.
    """

    def __init__(self, duration: float, jitter: float = 0.2):
        self.duration = duration
        self.jitter = jitter
        self.inflight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def submit(self, data, filename, mode, key, *, lang=None, **_kw) -> JobHandle:
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        return JobHandle(job_id=f"sim-{key}", mode=mode, status=QUEUED)

    def wait(self, job_id, mode="", budget_seconds=None, **_kw) -> JobOutcome:
        spread = self.duration * self.jitter
        time.sleep(max(0.0, random.uniform(self.duration - spread, self.duration + spread)))
        with self._lock:
            self.inflight -= 1
        return JobOutcome(
            job_id=job_id, mode=mode, status=SUCCEEDED,
            result={"pages": [{"text": "simulated extraction"}]},
        )

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
#  One run
# --------------------------------------------------------------------------- #
def _payloads(count: int, folder: Optional[Path]) -> List[Tuple[bytes, str]]:
    """`count` (bytes, filename) pairs, cycling the folder if it is short."""
    if folder is None:
        return [(b"%PDF-1.4 synthetic load test payload\n", f"synthetic-{i}.pdf")
                for i in range(count)]

    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
    )
    if not files:
        raise SystemExit(f"No PDFs or images in {folder}")
    return [
        (files[i % len(files)].read_bytes(), f"{files[i % len(files)].stem}-{i}{files[i % len(files)].suffix}")
        for i in range(count)
    ]


def run_once(
    payloads: List[Tuple[bytes, str]],
    *,
    workers: int,
    cap: int,
    simulate: Optional[float],
) -> dict:
    settings.veris_max_inflight_jobs = cap
    ocr_gateway.reset_metrics()

    sim = SimulatedVeris(simulate) if simulate is not None else None
    if sim is not None:
        ocr_gateway.client = lambda: sim  # type: ignore[assignment]

    latencies: List[float] = []
    failures = 0
    lock = threading.Lock()

    def one(index: int) -> None:
        nonlocal failures
        data, name = payloads[index]
        started = time.monotonic()
        try:
            _handle, outcome = ocr_gateway.run_job(
                data, name, MODE_RESUME, f"loadtest/{index}/{name}",
                budget_seconds=settings.ocr_job_wait_seconds,
            )
            ok = outcome is not None and outcome.succeeded
        except Exception as exc:  # noqa: BLE001 — a failure is a data point
            print(f"    !! {name}: {type(exc).__name__}: {exc}")
            ok = False
        elapsed = time.monotonic() - started
        with lock:
            latencies.append(elapsed)
            if not ok:
                failures += 1

    wall_start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, range(len(payloads))))
    wall = time.monotonic() - wall_start

    snap = ocr_gateway.snapshot()
    return {
        "cap": cap,
        "workers": workers,
        "count": len(payloads),
        "wall_seconds": round(wall, 2),
        "throughput_per_sec": round(len(payloads) / wall, 2) if wall else 0.0,
        "failures": failures,
        "latency_p50": round(statistics.median(latencies), 2) if latencies else 0.0,
        "latency_p95": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2) if latencies else 0.0,
        "latency_max": round(max(latencies), 2) if latencies else 0.0,
        "queue_wait_p95_ms": snap["queue_wait_ms"]["p95"],
        "peak_inflight": snap["peak_inflight"],
        "service_peak_inflight": sim.peak if sim else None,
    }


def _print(row: dict, target: float) -> None:
    verdict = "OK " if row["latency_p95"] <= target and not row["failures"] else "!! "
    print(
        f"  {verdict}cap={row['cap']:<3} workers={row['workers']:<3} "
        f"wall={row['wall_seconds']:>7.2f}s  "
        f"{row['throughput_per_sec']:>5.2f}/s  "
        f"p50={row['latency_p50']:>6.2f}s  p95={row['latency_p95']:>6.2f}s  "
        f"max={row['latency_max']:>6.2f}s  "
        f"slot_wait_p95={row['queue_wait_p95_ms']:>8.1f}ms  "
        f"peak_inflight={row['peak_inflight']:<3} "
        f"fail={row['failures']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50, help="resumes to push (default 50)")
    parser.add_argument("--dir", type=Path, help="folder of real PDFs/images to cycle")
    parser.add_argument("--workers", type=int, default=None,
                        help=f"threads (default: ingestion_max_workers={settings.ingestion_max_workers})")
    parser.add_argument("--cap", type=int, default=None,
                        help=f"in-flight cap (default: veris_max_inflight_jobs={settings.veris_max_inflight_jobs})")
    parser.add_argument("--sweep", type=str, help="comma-separated caps to try, e.g. 4,8,16,24,32")
    parser.add_argument("--simulate", type=float, metavar="SECONDS",
                        help="fake the service, each job taking SECONDS (no API spend)")
    parser.add_argument("--target", type=float, default=15.0,
                        help="per-resume p95 latency target in seconds (default 15)")
    args = parser.parse_args()

    if args.simulate is None and not settings.veris_ocr_api_key:
        raise SystemExit(
            "VERIS_OCR_API_KEY is not set. Use --simulate SECONDS to size the "
            "knobs without a live service."
        )

    workers = args.workers or settings.ingestion_max_workers
    caps = (
        [int(c) for c in args.sweep.split(",")]
        if args.sweep
        else [args.cap or settings.veris_max_inflight_jobs]
    )

    payloads = _payloads(args.count, args.dir)
    source = f"{args.dir}" if args.dir else "synthetic payloads"
    mode = f"SIMULATED service @ {args.simulate}s/job" if args.simulate is not None else "LIVE Veris"

    print()
    print(f"  {args.count} resumes  |  {source}  |  {mode}")
    print(f"  target: p95 <= {args.target}s per resume")
    print(f"  polling: fast phase {settings.ocr_job_fast_poll_seconds}s "
          f"@ {settings.ocr_job_fast_poll_interval_seconds}s intervals")
    print()

    if max(caps) > workers:
        # Worth saying loudly: the cap can only be reached if there are threads
        # free to hold the slots. A cap of 32 behind 16 workers is a cap of 16,
        # and a sweep that does not say so reads as "raising it stopped helping".
        print(f"  NOTE: caps above --workers ({workers}) cannot be reached — the "
              f"thread pool, not the cap, is the limit there.")
        print()

    rows = []
    for cap in caps:
        row = run_once(payloads, workers=workers, cap=cap, simulate=args.simulate)
        rows.append(row)
        _print(row, args.target)
        if row["peak_inflight"] < row["cap"]:
            print(f"       (never reached cap {row['cap']}; peak was "
                  f"{row['peak_inflight']} — raise --workers to use it)")

    print()
    best = min(rows, key=lambda r: (r["failures"], r["wall_seconds"]))
    print(f"  fastest: cap={best['cap']} -> {best['wall_seconds']}s "
          f"for {best['count']} resumes ({best['throughput_per_sec']}/s)")
    if best["queue_wait_p95_ms"] > 500:
        print("  slot wait is significant -> raise veris_max_inflight_jobs")
    elif best["latency_p95"] > args.target:
        print("  slot wait is ~0 and p95 is over target -> the service itself is "
              "the limit; raising the cap will not help")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
