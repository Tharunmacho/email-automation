"""Thin, typed wrapper over the Gmail API.

Responsibilities: search messages, parse headers/body/attachment metadata into
our EmailMessage model, download attachment bytes on demand, and post-process a
message (mark read / apply a processed label).
"""
from __future__ import annotations

import base64
import threading
from typing import List, Optional

from googleapiclient.discovery import build

from app.config import settings
from app.core.models import Attachment, EmailMessage
from app.gmail.auth import get_credentials
from app.logging_config import get_logger

log = get_logger(__name__)

# label name -> label id, shared by every client in the process. See _ensure_label.
_LABEL_IDS: dict[str, str] = {}
_LABEL_LOCK = threading.Lock()


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_from(value: str) -> tuple[str, Optional[str]]:
    # "Jane Doe <jane@example.com>" → ("jane@example.com", "Jane Doe")
    if "<" in value and ">" in value:
        name = value.split("<", 1)[0].strip().strip('"') or None
        addr = value.split("<", 1)[1].split(">", 1)[0].strip().lower()
        return addr, name
    return value.strip().lower(), None


class GmailClient:
    def __init__(self):
        self._service = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)

    # ---- searching -------------------------------------------------------- #
    def search_message_ids(self, query: str | None = None, max_results: int | None = None) -> List[str]:
        if query is None:
            query = settings.gmail_query
        max_results = max_results or settings.gmail_max_results
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in resp.get("messages", [])]

    # ---- fetching --------------------------------------------------------- #
    def get_message(self, message_id: str) -> EmailMessage:
        msg = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        from_addr, from_name = _parse_from(_header(headers, "From"))

        body_text = self._collect_body_text(payload)
        attachments = self._collect_attachments(payload)

        return EmailMessage(
            message_id=msg["id"],
            thread_id=msg.get("threadId", ""),
            from_addr=from_addr,
            from_name=from_name,
            to_addr=_header(headers, "To"),
            subject=_header(headers, "Subject"),
            date=_header(headers, "Date"),
            snippet=msg.get("snippet", ""),
            body_text=body_text,
            attachments=attachments,
        )

    def _collect_body_text(self, payload: dict) -> str:
        """Walk MIME parts and concatenate text/plain bodies."""
        parts_text: list[str] = []

        def walk(part: dict):
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            if mime == "text/plain" and body.get("data"):
                parts_text.append(_b64url_decode(body["data"]).decode("utf-8", errors="replace"))
            for sub in part.get("parts", []) or []:
                walk(sub)

        walk(payload)
        return "\n".join(parts_text).strip()

    def _collect_attachments(self, payload: dict) -> List[Attachment]:
        found: list[Attachment] = []
        idx = 0

        def walk(part: dict):
            nonlocal idx
            mime_type = (part.get("mimeType") or "").lower()
            filename = part.get("filename") or ""
            body = part.get("body", {})
            attachment_id = body.get("attachmentId")
            headers = part.get("headers", [])

            if not filename and headers:
                cd = _header(headers, "Content-Disposition")
                ct = _header(headers, "Content-Type")
                import re
                m_fn = re.search(r'filename="?([^";\n]+)"?', cd, re.I) or re.search(r'name="?([^";\n]+)"?', ct, re.I)
                if m_fn:
                    filename = m_fn.group(1).strip()

            is_doc_mime = (
                mime_type.startswith("application/pdf")
                or mime_type.startswith("application/msword")
                or mime_type.startswith("application/vnd")
                or mime_type.startswith("application/rtf")
                or mime_type.startswith("image/")
                or (mime_type.startswith("application/") and mime_type not in ("application/json", "application/javascript"))
            )

            if attachment_id or (is_doc_mime and mime_type not in ("text/plain", "text/html")):
                idx += 1
                if not filename:
                    ext = ".pdf" if "pdf" in mime_type else (".docx" if "word" in mime_type or "vnd" in mime_type else ".bin")
                    filename = f"resume_attachment_{idx}{ext}"

                if attachment_id:
                    found.append(
                        Attachment(
                            filename=filename,
                            mime_type=mime_type or "application/octet-stream",
                            size=body.get("size", 0),
                            attachment_id=attachment_id,
                        )
                    )
            for sub in part.get("parts", []) or []:
                walk(sub)

        walk(payload)
        return found

    def download_attachment(self, message_id: str, attachment: Attachment) -> bytes:
        att = (
            self._service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment.attachment_id)
            .execute()
        )
        data = _b64url_decode(att["data"])
        attachment.data = data
        return data

    # ---- post-processing & replies ---------------------------------------- #
    def send_reply(
        self,
        message_id: str,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
    ) -> dict:
        import email.message

        msg = email.message.EmailMessage()
        msg["To"] = to_addr
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Subject"] = reply_subject
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id
        msg.set_content(body_text)

        raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        body = {
            "raw": raw_b64,
            "threadId": thread_id,
        }
        sent = self._service.users().messages().send(userId="me", body=body).execute()
        log.info("Sent reply email to %s (threadId=%s, msgId=%s)", to_addr, thread_id, sent.get("id"))
        return sent

    def mark_read(self, message_id: str) -> None:
        self._service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()

    def apply_label(self, message_id: str, label_name: str) -> None:
        label_id = self._ensure_label(label_name)
        self._service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()

    def remove_label(self, message_id: str, label_name: str) -> None:
        try:
            label_id = self._ensure_label(label_name)
            self._service.users().messages().modify(
                userId="me", id=message_id, body={"removeLabelIds": [label_id]}
            ).execute()
        except Exception as err:
            log.warning("Failed to remove label '%s' from msg %s: %s", label_name, message_id, err)

    def _ensure_label(self, label_name: str) -> str:
        """Resolve a label name to its id, creating the label once if needed.

        Cached process-wide: a poll runs a client per message across a thread
        pool, and without this every mark-done listed the whole label set again.
        Label ids never change, so the cache cannot go stale — only a label
        deleted in Gmail invalidates it, and re-resolving happens on restart.
        """
        cached = _LABEL_IDS.get(label_name)
        if cached:
            return cached

        with _LABEL_LOCK:
            # Another thread may have resolved it while we waited for the lock.
            cached = _LABEL_IDS.get(label_name)
            if cached:
                return cached

            labels = self._service.users().labels().list(userId="me").execute().get("labels", [])
            for lbl in labels:
                if lbl["name"] == label_name:
                    _LABEL_IDS[label_name] = lbl["id"]
                    return lbl["id"]

            created = (
                self._service.users()
                .labels()
                .create(
                    userId="me",
                    body={"name": label_name, "labelListVisibility": "labelShow",
                          "messageListVisibility": "show"},
                )
                .execute()
            )
            log.info("Created Gmail label '%s'", label_name)
            _LABEL_IDS[label_name] = created["id"]
            return created["id"]


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("utf-8"))
