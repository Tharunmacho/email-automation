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
    owner = staff_name or "staff"
    return {
        "type": "candidate_ingested",
        "channel": "admin",
        "target_role": ADMIN_ROLE,
        "candidate": candidate,
        "message": f"{name} was ingested and allocated to {owner}.",
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


def publish_event(event: dict):
    """Publish an event to open WebSockets (best-effort async schedule)."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _dispatch():
        target_user_id = event.get("target_user_id")
        target_role = event.get("target_role")
        if target_user_id:
            await manager.broadcast_to_user(target_user_id, event)
        elif target_role:
            await manager.broadcast_to_role(target_role, event)

    loop.create_task(_dispatch())


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
