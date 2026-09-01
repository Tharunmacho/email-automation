"""How many pages we read at once, and why the host's core count is the wrong
number to ask.

`os.cpu_count()` reports the machine's cores and knows nothing about cgroups, so
inside a container it answers with the whole host. On a Dokploy host with eight
cores and a one-CPU quota it says 8, and eight Tesseract processes then share
one CPU: the work does not go faster, and every concurrent page is holding a
300-DPI raster (~26 MB for A4) plus Tesseract's working set.

Measured on an 11 MB, 28-page scanned bundle: 1 worker 7.06 s/page, 2 workers
4.37, 10 workers 2.71. Oversubscribing does not reach the middle column — it
lands below it, with the memory cost of the right-hand one.
"""
from __future__ import annotations

import os

import pytest

from app.config import settings
from app.extraction import local_ocr
from app.extraction.local_ocr import available_cpus, local_worker_count


@pytest.fixture
def cgroup(tmp_path, monkeypatch):
    """Point the cgroup probes at files we control."""
    def _write(v2: str | None = None, v1: tuple[int, int] | None = None):
        files: dict[str, str] = {}
        if v2 is not None:
            files["/sys/fs/cgroup/cpu.max"] = v2
        if v1 is not None:
            files["/sys/fs/cgroup/cpu/cpu.cfs_quota_us"] = str(v1[0])
            files["/sys/fs/cgroup/cpu/cpu.cfs_period_us"] = str(v1[1])

        real_path = local_ocr.Path

        class _Path(real_path):
            def read_text(self, *a, **k):
                key = str(self).replace("\\", "/")
                if key in files:
                    return files[key]
                raise FileNotFoundError(key)

        monkeypatch.setattr(local_ocr, "Path", _Path)
    return _write


def test_a_one_cpu_container_is_not_told_it_has_eight(cgroup, monkeypatch):
    """The reported case: `cpu.max` says 100000/100000, the host says 8."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False)
    cgroup(v2="100000 100000")

    assert available_cpus() == 1


def test_a_two_cpu_quota_is_read_as_two(cgroup, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(16)), raising=False)
    cgroup(v2="200000 100000")

    assert available_cpus() == 2


def test_an_unlimited_container_falls_back_to_the_host(cgroup, monkeypatch):
    """`max` is cgroup v2 for "no quota", and must not be read as a limit."""
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False)
    cgroup(v2="max 100000")

    assert available_cpus() == 8


def test_an_older_host_is_read_through_cgroup_v1(cgroup, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False)
    cgroup(v1=(150000, 100000))

    assert available_cpus() == 1


def test_v1_unlimited_is_minus_one_and_is_not_a_limit(cgroup, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(4)), raising=False)
    cgroup(v1=(-1, 100000))

    assert available_cpus() == 4


def test_a_pinned_process_is_capped_by_its_affinity(cgroup, monkeypatch):
    """`taskset -c 0,1` is a real limit that no cgroup file mentions."""
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    cgroup()

    assert available_cpus() == 2


def test_a_plain_host_is_unaffected(cgroup, monkeypatch):
    """No cgroup files, no affinity call — Windows and macOS land here, and
    must keep the behaviour they had."""
    monkeypatch.setattr(os, "cpu_count", lambda: 12)
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    cgroup()

    assert available_cpus() == 12


# --------------------------------------------------------------------------- #
#  What the reader actually asks for
# --------------------------------------------------------------------------- #
def test_the_worker_count_follows_the_quota_not_the_host(cgroup, monkeypatch):
    monkeypatch.setattr(settings, "ocr_local_workers", 0)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False)
    cgroup(v2="100000 100000")

    assert local_worker_count() == 2, "a one-CPU container must not start eight readers"


def test_two_readers_is_the_floor_even_on_one_cpu(cgroup, monkeypatch):
    """A page read is not pure CPU — it waits on rasterisation and on a
    subprocess — so a second worker still earns its place."""
    monkeypatch.setattr(settings, "ocr_local_workers", 0)
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0}, raising=False)
    cgroup()

    assert local_worker_count() == 2


def test_an_explicit_setting_still_wins(cgroup, monkeypatch):
    """`OCR_LOCAL_WORKERS` is how an operator overrules all of this."""
    monkeypatch.setattr(settings, "ocr_local_workers", 6)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    cgroup(v2="100000 100000")

    assert local_worker_count() == 6
