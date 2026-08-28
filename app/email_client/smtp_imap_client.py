"""SMTP and IMAP Email Client.

Provides standard interface for fetching incoming emails via IMAP, parsing MIME messages
and attachments, marking messages read / moving them to folders, and sending auto-replies
via SMTP using username and password / app password credentials.
"""
from __future__ import annotations

import email
import email.header
import email.message
import email.utils
import imaplib
import smtplib
from typing import List, Optional

from app.config import settings
from app.core.models import Attachment, EmailMessage
from app.extraction.file_type import ext_for_mime, is_document_mime
from app.logging_config import get_logger

log = get_logger(__name__)


def _decode_header_str(header_value: str | None) -> str:
    if not header_value:
        return ""
    decoded_fragments = []
    for fragment, encoding in email.header.decode_header(header_value):
        if isinstance(fragment, bytes):
            charset = encoding or "utf-8"
            try:
                decoded_fragments.append(fragment.decode(charset, errors="replace"))
            except Exception:
                decoded_fragments.append(fragment.decode("latin1", errors="replace"))
        else:
            decoded_fragments.append(str(fragment))
    return " ".join(decoded_fragments).strip()


def _parse_from(value: str) -> tuple[str, Optional[str]]:
    if not value:
        return "", None
    name, addr = email.utils.parseaddr(value)
    name = _decode_header_str(name) or None
    addr = (addr or value).strip().lower()
    return addr, name


class SMTPIMAPClient:
    """IMAP (receiving) + SMTP (sending) client using standard email credentials."""

    def __init__(self, config: dict | None = None):
        if config:
            self.imap_server = config.get("imap_server", "")
            self.imap_port = config.get("imap_port", 993)
            self.imap_username = config.get("imap_username", "")
            self.imap_password = config.get("imap_password", "")
            self.imap_use_ssl = config.get("imap_use_ssl", True)
            self.imap_folder = config.get("imap_folder", "INBOX")

            self.smtp_server = config.get("smtp_server", "")
            self.smtp_port = config.get("smtp_port", 465)
            self.smtp_username = config.get("smtp_username", "")
            self.smtp_password = config.get("smtp_password", "")
            self.smtp_use_tls = config.get("smtp_use_tls", False)
            self.smtp_use_ssl = config.get("smtp_use_ssl", True)
        else:
            self.imap_server = settings.imap_server
            self.imap_port = settings.imap_port
            self.imap_username = settings.imap_username
            self.imap_password = settings.imap_password
            self.imap_use_ssl = settings.imap_use_ssl
            self.imap_folder = settings.imap_folder or "INBOX"

            self.smtp_server = settings.smtp_server
            self.smtp_port = settings.smtp_port
            self.smtp_username = settings.smtp_username
            self.smtp_password = settings.smtp_password
            self.smtp_use_tls = settings.smtp_use_tls
            self.smtp_use_ssl = settings.smtp_use_ssl

        # In-memory cache for fetched messages during batch run to avoid re-fetching
        self._fetched_bytes_cache: dict[str, bytes] = {}

    def _connect_imap(self) -> imaplib.IMAP4:
        if self.imap_use_ssl:
            client = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
        else:
            client = imaplib.IMAP4(self.imap_server, self.imap_port)
        if self.imap_username and self.imap_password:
            client.login(self.imap_username, self.imap_password)
        return client

    # ---- searching -------------------------------------------------------- #
    def search_message_ids(self, query: str | None = None, max_results: int | None = None) -> List[str]:
        if not self.imap_server or not self.imap_username:
            log.warning("IMAP server or username not configured.")
            return []

        max_results = max_results or settings.gmail_max_results
        try:
            mail = self._connect_imap()
            try:
                mail.select(self.imap_folder)
                status, data = mail.uid("search", None, "UNSEEN")
                if status != "OK":
                    status, data = mail.uid("search", None, "ALL")

                if status != "OK" or not data or not data[0]:
                    return []

                raw_uids = data[0].split()
                # Process up to max_results in reverse order (newest first)
                uids = [u.decode("utf-8") for u in reversed(raw_uids[-max_results:])]
                return uids
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as exc:
            log.error("IMAP search_message_ids failed: %s", exc)
            return []

    # ---- fetching --------------------------------------------------------- #
    def get_message(self, message_id: str) -> EmailMessage:
        raw_bytes = self._fetched_bytes_cache.get(message_id)
        if not raw_bytes:
            mail = self._connect_imap()
            try:
                mail.select(self.imap_folder)
                # BODY.PEEK[], never RFC822: a plain RFC822 fetch sets \Seen as
                # a side effect, and `search_message_ids` only ever asks for
                # UNSEEN. Merely *looking* at a message therefore removed it
                # from every future poll — so an email the detector ignored, or
                # one that failed mid-parse, could never be reconsidered. Marking
                # a message read is a decision the runner makes after a
                # successful ingest, not something reading it does by accident.
                status, data = mail.uid("fetch", message_id, "(BODY.PEEK[])")
                if status != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
                    raise RuntimeError(f"Could not fetch message body for UID {message_id}")
                raw_bytes = data[0][1]
                self._fetched_bytes_cache[message_id] = raw_bytes
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass

        msg = email.message_from_bytes(raw_bytes)

        raw_from = _decode_header_str(msg.get("From", ""))
        from_addr, from_name = _parse_from(raw_from)
        to_addr = _decode_header_str(msg.get("To", ""))
        subject = _decode_header_str(msg.get("Subject", ""))
        date_str = _decode_header_str(msg.get("Date", ""))
        header_msg_id = _decode_header_str(msg.get("Message-ID", message_id))

        body_text, attachments = self._parse_mime_parts(message_id, msg)
        snippet = body_text[:200].replace("\n", " ").strip() if body_text else ""

        return EmailMessage(
            message_id=message_id,
            thread_id=header_msg_id or message_id,
            from_addr=from_addr,
            from_name=from_name,
            to_addr=to_addr,
            subject=subject,
            date=date_str,
            snippet=snippet,
            body_text=body_text,
            attachments=attachments,
        )

    def _parse_mime_parts(self, message_id: str, msg: email.message.Message) -> tuple[str, List[Attachment]]:
        body_parts: list[str] = []
        attachments: list[Attachment] = []

        part_idx = 0
        for part in msg.walk():
            part_idx += 1
            if part.is_multipart():
                continue

            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            filename = part.get_filename()

            if filename:
                filename = _decode_header_str(filename)

            # A part is an attachment if it is *marked* as one, if it is named,
            # or if it simply carries a document or an image. That last clause
            # is what catches the CV pasted in as an inline image and the scan
            # sent by a client that omitted Content-Disposition entirely —
            # both of which used to fall through to the body branch, be
            # discarded as non-text, and leave the mail looking attachment-free.
            disposition = content_disposition.lower()
            is_body_text = content_type in ("text/plain", "text/html")
            is_attachment = (
                "attachment" in disposition
                or bool(filename)
                or ("inline" in disposition and not is_body_text)
                or (not is_body_text and is_document_mime(content_type))
            )

            if is_attachment:
                payload = part.get_payload(decode=True) or b""
                if not payload:
                    continue
                att_id = f"{message_id}_{part_idx}"
                if not filename:
                    filename = f"document_{part_idx}{ext_for_mime(content_type)}"
                att = Attachment(
                    filename=filename,
                    mime_type=content_type or "application/octet-stream",
                    size=len(payload),
                    attachment_id=att_id,
                    data=payload,
                )
                attachments.append(att)
            elif content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body_parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        body_parts.append(payload.decode("latin1", errors="replace"))
            elif content_type == "text/html" and not body_parts:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    raw_html = payload.decode(charset, errors="replace")
                    import re
                    clean_text = re.sub(r"<[^>]+>", " ", raw_html)
                    body_parts.append(clean_text)

        return "\n".join(body_parts).strip(), attachments

    def download_attachment(self, message_id: str, attachment: Attachment) -> bytes:
        if attachment.data is not None:
            return attachment.data
        self.get_message(message_id)
        return attachment.data or b""

    # ---- post-processing & replies ---------------------------------------- #
    def send_reply(
        self,
        message_id: str,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
    ) -> dict:
        if not self.smtp_server or not self.smtp_username:
            log.warning("SMTP server or username not configured. Skipping send_reply.")
            return {}

        msg = email.message.EmailMessage()
        msg["To"] = to_addr
        msg["From"] = self.smtp_username
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Subject"] = reply_subject
        if thread_id:
            msg["In-Reply-To"] = thread_id
            msg["References"] = thread_id
        msg.set_content(body_text)

        if self.smtp_use_ssl:
            server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=15)

        try:
            if not self.smtp_use_ssl and self.smtp_use_tls:
                server.starttls()
            if self.smtp_username and self.smtp_password:
                server.login(self.smtp_username, self.smtp_password)
            server.send_message(msg)
            log.info("Sent SMTP reply email to %s for message %s", to_addr, message_id)
            return {"status": "sent", "to": to_addr}
        finally:
            try:
                server.quit()
            except Exception:
                pass

    def mark_read(self, message_id: str) -> None:
        try:
            mail = self._connect_imap()
            try:
                mail.select(self.imap_folder)
                mail.uid("store", message_id, "+FLAGS", "(\\Seen)")
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as exc:
            log.warning("IMAP mark_read failed for UID %s: %s", message_id, exc)

    def apply_label(self, message_id: str, label_name: str) -> None:
        try:
            mail = self._connect_imap()
            try:
                mail.select(self.imap_folder)
                # Always mark as read (\\Seen) in INBOX first
                mail.uid("store", message_id, "+FLAGS", "(\\Seen)")
                
                # Adapt folder name for Hostinger IMAP folder conventions
                target_folder = label_name.replace("/", ".")
                try:
                    mail.create(target_folder)
                except Exception:
                    pass
                res, _ = mail.uid("copy", message_id, target_folder)
                if res == "OK":
                    mail.uid("store", message_id, "+FLAGS", "(\\Deleted)")
                    mail.expunge()
                    log.info("Moved IMAP message UID %s to folder '%s'", message_id, target_folder)
                else:
                    # Fallback try with INBOX prefix
                    inbox_target = f"INBOX.{target_folder}"
                    try:
                        mail.create(inbox_target)
                    except Exception:
                        pass
                    res_inbox, _ = mail.uid("copy", message_id, inbox_target)
                    if res_inbox == "OK":
                        mail.uid("store", message_id, "+FLAGS", "(\\Deleted)")
                        mail.expunge()
                        log.info("Moved IMAP message UID %s to folder '%s'", message_id, inbox_target)
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as exc:
            log.warning("IMAP apply_label '%s' failed for UID %s: %s", label_name, message_id, exc)

    def remove_label(self, message_id: str, label_name: str) -> None:
        try:
            mail = self._connect_imap()
            try:
                target_folder = label_name.replace("/", ".")
                candidate_folders = [target_folder, f"INBOX.{target_folder}"]
                
                header_msg_id = ""
                header_subject = ""
                header_from = ""
                for check_f in [self.imap_folder, f"INBOX.{self.imap_folder}", "Resumes.Deleted", "INBOX.Resumes.Deleted"]:
                    try:
                        st_s, _ = mail.select(check_f)
                        if st_s != "OK":
                            continue
                        st_f, fetch_d = mail.uid("fetch", message_id, "(BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM)])")
                        if st_f == "OK" and fetch_d and fetch_d[0] and isinstance(fetch_d[0], tuple):
                            hdr_txt = fetch_d[0][1].decode(errors="ignore")
                            for line in hdr_txt.splitlines():
                                l_lower = line.lower()
                                if l_lower.startswith("message-id:"):
                                    header_msg_id = line.split(":", 1)[1].strip()
                                elif l_lower.startswith("subject:"):
                                    header_subject = line.split(":", 1)[1].strip()
                                elif l_lower.startswith("from:"):
                                    header_from = line.split(":", 1)[1].strip()
                            if header_msg_id or header_subject:
                                break
                    except Exception:
                        continue

                for folder in candidate_folders:
                    try:
                        res_sel, _ = mail.select(folder)
                        if res_sel != "OK":
                            continue

                        st, fetch_d = mail.uid("fetch", message_id, "(FLAGS)")
                        if st == "OK" and fetch_d and fetch_d[0] and isinstance(fetch_d[0], tuple):
                            mail.uid("store", message_id, "+FLAGS", "(\\Deleted)")
                            mail.expunge()
                            log.info("Removed IMAP message UID %s from folder '%s'", message_id, folder)
                            break

                        st_all, search_d = mail.uid("search", None, "ALL")
                        if st_all == "OK" and search_d and search_d[0]:
                            folder_uids = [u.decode() for u in search_d[0].split()]
                            for f_uid in folder_uids:
                                st_hdr, fetch_hdr = mail.uid("fetch", f_uid, "(BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM)])")
                                if st_hdr == "OK" and fetch_hdr and fetch_hdr[0] and isinstance(fetch_hdr[0], tuple):
                                    hdr_text = fetch_hdr[0][1].decode(errors="ignore")
                                    match = False
                                    if header_msg_id and header_msg_id in hdr_text:
                                        match = True
                                    elif header_subject and header_from and (header_subject in hdr_text and header_from in hdr_text):
                                        match = True
                                    elif message_id in hdr_text:
                                        match = True

                                    if match:
                                        mail.uid("store", f_uid, "+FLAGS", "(\\Deleted)")
                                        mail.expunge()
                                        log.info("Removed matching IMAP message UID %s (key=%s) from folder '%s'", f_uid, message_id, folder)
                                        break
                    except Exception:
                        continue
            finally:
                try:
                    mail.logout()
                except Exception:
                    pass
        except Exception as exc:
            log.warning("IMAP remove_label '%s' failed for key %s: %s", label_name, message_id, exc)
