"""Thin, typed wrapper over the Gmail API.

Responsibilities: search messages, parse headers/body/attachment metadata into
our EmailMessage model, download attachment bytes on demand, and post-process a
message (mark read / apply a processed label).
"""
from __future__ import annotations

import base64
import re
import threading
from typing import List, Optional
from urllib.parse import unquote

from googleapiclient.discovery import build

from app.config import settings
from app.core.models import Attachment, EmailMessage
from app.extraction.file_type import ext_for_mime
from app.gmail.auth import get_credentials
from app.logging_config import get_logger

log = get_logger(__name__)

# label name -> label id, shared by every client in the process. See _ensure_label.
_LABEL_IDS: dict[str, str] = {}
_LABEL_LOCK = threading.Lock()

# Serialises credential loading only. An expired token refreshed by ten threads
# at once is ten refresh calls against Google for one new token.
_CREDS_LOCK = threading.Lock()


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
    """One client object, one Gmail transport *per thread*.

    A poll runs its messages across a thread pool and every worker reaches
    through the same client. The service that ``build()`` returns wraps a single
    ``httplib2.Http``, which holds a single TLS socket and is documented as not
    thread-safe: two workers issuing requests at the same moment interleave
    their reads on that one socket and both come back as

        ssl.SSLError: [SSL: WRONG_VERSION_NUMBER] wrong version number

    — the socket handing one thread the middle of the other's response. One
    email in a batch always worked; two raced, and the loser was logged as a
    failed message and silently left un-ingested until someone noticed the count
    did not match the inbox.

    So the service is thread-local: built on first use in each worker and reused
    for that worker's lifetime. Credentials are loaded under a lock and shared,
    because the OAuth token is process-wide state and refreshing it ten times
    concurrently is ten round-trips for one token.
    """

    def __init__(self):
        self._local = threading.local()
        # Surface a missing/invalid token here, at construction, rather than
        # inside a pool worker where it would read as a per-message failure.
        _ = self._service

    @property
    def _service(self):
        service = getattr(self._local, "service", None)
        if service is None:
            with _CREDS_LOCK:
                credentials = get_credentials()
            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            self._local.service = service
        return service

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
        """Every non-body part of the message, however it was encoded.

        Two things here are load-bearing, and neither used to be:

        * **Inline bodies.** Gmail only mints an ``attachmentId`` for parts above
          roughly 50 KB. Anything smaller comes back with the bytes sitting in
          ``body["data"]`` instead, and requiring an ``attachmentId`` dropped
          those on the floor — a 30 KB phone scan of a CV was simply invisible
          to the pipeline.
        * **Missing filenames.** A part with no ``filename`` is not a part with
          no content. Clients that send a CV as an inline image, and forwards
          that lose the disposition header, both produce exactly that. The name
          is reconstructed from ``Content-Disposition``, then from the MIME
          type; it is only ever used for display and for a type *hint*, never as
          a reason to discard.
        """
        found: list[Attachment] = []
        counter = {"n": 0}

        def walk(part: dict):
            _collect_one(part)
            for sub in part.get("parts", []) or []:
                walk(sub)

        def _collect_one(part: dict):
            mime = (part.get("mimeType") or "").lower()
            body = part.get("body", {}) or {}
            filename = part.get("filename") or ""
            attachment_id = body.get("attachmentId")
            inline_data = body.get("data")

            # A container holds no bytes of its own.
            if mime.startswith("multipart/"):
                return
            if not attachment_id and not inline_data:
                return

            headers = part.get("headers", []) or []
            disposition = _header(headers, "Content-Disposition")

            # The message body itself is not an attachment — unless the sender
            # explicitly attached it, which is how some clients send a .txt CV.
            if mime in ("text/plain", "text/html") and not filename:
                if "attachment" not in disposition.lower():
                    return

            if not filename:
                filename = _filename_from_disposition(disposition)
            if not filename:
                counter["n"] += 1
                filename = f"document_{counter['n']}{ext_for_mime(mime)}"

            data: bytes | None = None
            size = body.get("size", 0) or 0
            if not attachment_id and inline_data:
                # Small part: the bytes are already here, so there is nothing to
                # fetch later. `size` from Gmail is the encoded length, so take
                # the decoded length as the truth the size floor is judged on.
                try:
                    data = _b64url_decode(inline_data)
                    size = len(data)
                except Exception as err:  # noqa: BLE001 — a bad part is not a bad message
                    log.warning("Could not decode inline part '%s': %s", filename, err)
                    return

            found.append(
                Attachment(
                    filename=filename,
                    mime_type=part.get("mimeType", "application/octet-stream"),
                    size=size,
                    attachment_id=attachment_id or "",
                    data=data,
                )
            )

        walk(payload)
        return found

    def download_attachment(self, message_id: str, attachment: Attachment) -> bytes:
        # Small parts arrive with their bytes inline and have no attachment id
        # to fetch by; asking the API for id="" is a 400.
        if attachment.data is not None:
            return attachment.data
        if not attachment.attachment_id:
            raise ValueError(
                f"Attachment '{attachment.filename}' has neither inline data nor an "
                "attachment id — nothing to download."
            )
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

    def apply_label(
        self,
        message_id: str,
        label_name: str,
        rfc_message_id: str = "",
        subject: str = "",
        from_addr: str = "",
    ) -> None:
        """Label the message and take it out of the inbox in the same call.

        Adding a label leaves `INBOX` in place, so a processed résumé stayed in
        the recruiter's inbox wearing a label nobody looks at. Archiving is the
        visible half of "this one is handled".

        The three trailing arguments exist for the IMAP client, which needs the
        Message-ID header to find a message it has already filed. Gmail's own
        ids never move, so they are accepted and ignored here — the two clients
        are called through the same signature.
        """
        label_id = self._ensure_label(label_name)
        self._service.users().messages().modify(
            userId="me", id=message_id,
            body={"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]},
        ).execute()

    def remove_label(
        self,
        message_id: str,
        label_name: str,
        rfc_message_id: str = "",
        subject: str = "",
        from_addr: str = "",
    ) -> None:
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
    # Gmail strips base64 padding on some parts; restore it before decoding.
    raw = data.encode("utf-8")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


_DISPOSITION_FILENAME = re.compile(
    r"""filename\*?\s*=\s*(?:"([^"]+)"|'([^']+)'|([^;\r\n]+))""", re.IGNORECASE,
)


def _filename_from_disposition(disposition: str) -> str:
    """Pull a filename out of a Content-Disposition / Content-Type header."""
    match = _DISPOSITION_FILENAME.search(disposition or "")
    if not match:
        return ""
    name = next((g for g in match.groups() if g), "").strip()
    # RFC 5987: `filename*=UTF-8''resume%20final.pdf`
    if "''" in name:
        name = name.split("''", 1)[1]
    return unquote(name).strip().strip('"')
