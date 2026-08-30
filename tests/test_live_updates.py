"""A candidate that has just been ingested must reach the screen by itself.

The symptom was "the count only goes up when I reload". The cause was one line:
`publish_event` asked for the *running* event loop and returned quietly when
there was none. Ingestion never runs on the event loop — it runs on a worker
thread of the poll batch, or in a Celery process with no loop at all — so the
answer was always "none", and every push was dropped before it was sent.
"""
from __future__ import annotations

import asyncio
import threading

from app.api import websocket as ws


class _Sink:
    """Stands in for the connection manager, recording what it was asked to send."""

    def __init__(self):
        self.to_role: list[tuple[str, dict]] = []
        self.to_user: list[tuple[str, dict]] = []

    async def broadcast_to_role(self, role, message):
        self.to_role.append((role, message))

    async def broadcast_to_user(self, user_id, message):
        self.to_user.append((user_id, message))


def _capture(monkeypatch) -> _Sink:
    sink = _Sink()
    monkeypatch.setattr(ws, "manager", sink)
    return sink


def test_an_event_published_from_a_worker_thread_reaches_the_sockets(monkeypatch):
    """The exact shape of the bug: the publisher is not on the loop's thread."""
    sink = _capture(monkeypatch)
    delivered = threading.Event()

    async def scenario():
        ws.set_publisher_loop(asyncio.get_running_loop())

        def ingest():
            ws.publish_event(ws.candidate_ingested_event({"id": "c-1", "full_name": "Asha"}))
            delivered.set()

        await asyncio.to_thread(ingest)
        assert delivered.wait(timeout=2)
        # The publish is scheduled onto the loop; yield so it can run.
        for _ in range(10):
            await asyncio.sleep(0)
            if sink.to_role:
                break

    asyncio.run(scenario())

    assert sink.to_role, "the ingestion event never reached a socket"
    role, message = sink.to_role[0]
    assert message["type"] == "candidate_ingested"
    assert message["candidate"]["id"] == "c-1"


def test_an_event_published_on_the_loop_still_works(monkeypatch):
    sink = _capture(monkeypatch)

    async def scenario():
        ws.set_publisher_loop(asyncio.get_running_loop())
        ws.publish_event(ws.candidate_assigned_event("staff-1", {"id": "c-2"}))
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert [user for user, _ in sink.to_user] == ["staff-1"]


def test_with_no_loop_at_all_the_event_is_relayed_for_another_process(monkeypatch):
    """A Celery worker owns no sockets, so its only route out is the relay."""
    monkeypatch.setattr(ws, "_LOOP", None)
    relayed: list[dict] = []
    monkeypatch.setattr(ws, "_publish_via_redis", lambda event: relayed.append(event) or True)

    ws.publish_event(ws.candidate_ingested_event({"id": "c-3"}))

    assert [e["candidate"]["id"] for e in relayed] == ["c-3"]


def test_an_unallocated_candidate_is_announced_honestly():
    """No staff member owns it yet, and the message must not claim one does."""
    message = ws.candidate_ingested_event({"id": "c-4", "full_name": "Ravi"})["message"]

    assert "waiting to be allocated" in message
    assert "allocated to staff." not in message


def test_the_pipeline_announces_a_candidate_nobody_was_assigned(monkeypatch):
    """Auto-assignment can decline — no active staff, or it is switched off —
    and the queue on screen still has to show what just arrived."""
    from app.ingestion.pipeline import IngestionPipeline

    published: list[dict] = []
    monkeypatch.setattr(ws, "publish_event", lambda event: published.append(event))

    pipeline = object.__new__(IngestionPipeline)
    profile = type("P", (), {"full_name": "Meena", "email": "meena@example.com"})()
    pipeline._announce("cand-9", profile)

    assert [e["type"] for e in published] == ["candidate_ingested"]
    assert published[0]["candidate"]["full_name"] == "Meena"
