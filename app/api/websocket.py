"""Live push updates over WebSockets (`/ws`).

Clients pass `?token=<jwt>` on connect. Connections are held in a thread-safe
manager and scoped by role:
  * Staff members receive allocation events for their own account only.
  * Admins receive ingestion notifications and SLA breach alerts.

Publishing is safe to call from any thread or process; if no socket is open for
the recipient, the event is silently dropped (notifications store the persistent
record).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Set

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from app.config import settings
from app.core.security import read_token
from app.db.users import ADMIN_ROLE, STAFF_ROLE, UserRepository
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self._connections: Dict[str, Set[WebSocket]] = {}
        self._users: Dict[WebSocket, dict] = {}

    async def connect(self, websocket: WebSocket, user: dict):
        await websocket.accept()
        user_id = user["id"]
        if user_id not in self._connections:
            self._connections[user_id] = set()
        self._connections[user_id].add(websocket)
        self._users[websocket] = user
        log.info(
            "WebSocket connected: user=%s role=%s (%d live)",
            user_id,
            user.get("role"),
            len(self._connections[user_id]),
        )

    def disconnect(self, websocket: WebSocket):
        user = self._users.pop(websocket, None)
        if user:
            user_id = user["id"]
            if user_id in self._connections:
                self._connections[user_id].discard(websocket)
                if not self._connections[user_id]:
                    del self._connections[user_id]
            log.info(
                "WebSocket disconnected: user=%s role=%s (%d live)",
                user_id,
                user.get("role"),
                len(self._connections.get(user_id, set())),
            )

    async def broadcast_to_user(self, user_id: str, message: dict):
        sockets = list(self._connections.get(user_id, set()))
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(ws)

    async def broadcast_to_role(self, role: str, message: dict):
        for ws, user in list(self._users.items()):
            if user.get("role") == role:
                try:
                    await ws.send_json(message)
                except Exception:
                    self.disconnect(ws)


manager = ConnectionManager()


def candidate_assigned_event(staff_id: str, candidate: dict) -> dict:
    # The name falls back to the address: a résumé the parser could not pull a
    # name from still has to be identifiable, or the staff member cannot tell
    # what just arrived.
    name = candidate.get("full_name") or candidate.get("email") or "Unnamed candidate"
    return {
        "type": "candidate_assigned",
        "channel": "staff",
        "staff_id": staff_id,
        "target_user_id": staff_id,
        "candidate": candidate,
        "message": f"🔔 New Candidate Profile Ingested & Assigned: {name}",
    }


def candidate_ingested_event(candidate: dict, staff_name: str | None = None) -> dict:
    name = candidate.get("full_name") or candidate.get("email") or "Unnamed candidate"
    # An unallocated candidate is a real state — no active staff, or the
    # balancer is off — and saying it was "allocated to staff" when it was not
    # sends an admin looking for an owner that does not exist.
    message = (
        f"{name} was ingested and allocated to {staff_name}."
        if staff_name
        else f"{name} was ingested and is waiting to be allocated."
    )
    return {
        "type": "candidate_ingested",
        "channel": "admin",
        "target_role": ADMIN_ROLE,
        "candidate": candidate,
        "message": message,
    }


def sla_alert_event(alerts: list, threshold_hours: float) -> dict:
    count = len(alerts)
    if count == 1:
        alert = alerts[0]
        who = alert.get("assigned_staff_name") or "a staff member"
        what = alert.get("full_name") or alert.get("candidate_name") or "a profile"
        message = (
            f"⚠️ SLA Breach: Staff {who} has not viewed/evaluated "
            f"candidate {what} for >{threshold_hours:g} hours."
        )
    else:
        # One line naming ten candidates is unreadable; the modal lists them.
        message = (
            f"⚠️ SLA Breach: {count} assigned profiles have not been "
            f"viewed/evaluated for >{threshold_hours:g} hours."
        )

    return {
        "type": "sla_alert",
        "channel": "admin",
        "target_role": ADMIN_ROLE,
        "alerts": alerts,
        "count": count,
        "message": message,
    }


#: The API process's event loop, captured at startup. Everything that ingests a
#: résumé runs somewhere else — a worker thread in the poll batch, or a Celery
#: process entirely — and `asyncio.get_running_loop()` raises in both. It
#: raising was the whole bug: `publish_event` caught it, returned quietly, and
#: nothing was ever pushed, so a newly ingested candidate only appeared when the
#: operator reloaded the page by hand.
_LOOP: "asyncio.AbstractEventLoop | None" = None

#: Redis channel used to carry an event from a process that has no socket of its
#: own (a Celery worker) to the API process that does.
EVENT_CHANNEL = "crm:events"


def set_publisher_loop(loop) -> None:
    """Remember the loop that owns the WebSockets. Called once, at startup."""
    global _LOOP
    _LOOP = loop
    log.info("WebSocket publisher bound to the API event loop")


async def _dispatch(event: dict) -> None:
    target_user_id = event.get("target_user_id")
    target_role = event.get("target_role")
    if target_user_id:
        await manager.broadcast_to_user(target_user_id, event)
    elif target_role:
        await manager.broadcast_to_role(target_role, event)


def _publish_via_redis(event: dict) -> bool:
    """Hand the event to the API process, for callers that are not in it.

    A Celery worker holds no WebSocket, so its only route to a browser is the
    Redis both processes already share for the ingestion locks. Best-effort: a
    deployment running without Redis simply falls back to nothing being pushed,
    which is what it had before.
    """
    try:
        from app.tasks.locks import get_redis

        get_redis().publish(EVENT_CHANNEL, json.dumps(event, default=str))
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not relay the event over Redis: %s", exc)
        return False


def publish_event(event: dict):
    """Push an event to open WebSockets, from any thread or any process.

    Three cases, in order:

    1. Called *on* the API's event loop — schedule it directly.
    2. Called from another thread of the API process (the ingestion runner's
       worker pool, the inline poll) — hand it to the loop thread-safely.
    3. Called from another process altogether (a Celery worker) — relay it
       through Redis to whichever process is holding the sockets.
    """
    import asyncio

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is not None:
        running.create_task(_dispatch(event))
        return

    loop = _LOOP
    if loop is not None and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_dispatch(event), loop)
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not schedule the event on the API loop: %s", exc)

    _publish_via_redis(event)


async def relay_redis_events() -> None:
    """Deliver events other processes published, for as long as the API runs.

    Started as a background task at startup. It is the receiving half of
    `_publish_via_redis`; without it a Celery-driven ingestion reaches Redis and
    stops there.
    """
    import asyncio

    while True:
        try:
            from app.tasks.locks import get_redis

            pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(EVENT_CHANNEL)
            log.info("Listening for cross-process events on '%s'", EVENT_CHANNEL)
            while True:
                # `get_message` is blocking, so it runs on a worker thread; the
                # timeout is what lets the task notice cancellation at shutdown.
                message = await asyncio.to_thread(pubsub.get_message, True, 1.0)
                if not message:
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                try:
                    await _dispatch(json.loads(data))
                except Exception as exc:  # noqa: BLE001 — one bad payload, not the loop
                    log.debug("Dropped an unreadable relayed event: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Redis is optional. Retry quietly rather than filling the log: a
            # deployment without it is a supported configuration.
            log.debug("Event relay unavailable (%s); retrying shortly", exc)
            await asyncio.sleep(5)


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
):
    # 4401 — the WebSocket convention for "unauthenticated", closed before
    # accept() so a client with no valid token never reaches a state where it
    # could be sent an event.
    if not token:
        await websocket.close(code=4401, reason="Missing token")
        return

    subject = read_token(token, settings.auth_secret)
    if not subject:
        await websocket.close(code=4401, reason="Invalid token")
        return

    users = UserRepository()
    user = users.get(subject)
    if not user:
        await websocket.close(code=4401, reason="User not found")
        return

    user_dict = user.to_public()
    await manager.connect(websocket, user_dict)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
