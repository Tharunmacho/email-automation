"""SMTP and IMAP Email Client.

Provides standard interface for fetching incoming emails via IMAP, parsing MIME messages
and attachments, marking messages read / moving them to folders, and sending auto-replies
via SMTP using username and password / app password credentials.
"""
from __future__ import annotations

import contextlib
import email
import email.header
import email.message
import email.utils
import imaplib
import smtplib
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from app.config import settings
from app.core import message_ids
from app.core.models import Attachment, EmailMessage
from app.email_client import imap_folders
from app.extraction.file_type import ext_for_mime, is_document_mime
from app.logging_config import get_logger

log = get_logger(__name__)


class _BoundedBytesCache(OrderedDict):
    """Fetched message bodies, capped so a long-lived client cannot grow forever.

    A client used to be rebuilt for every poll, which bounded this by accident —
    the whole object was thrown away. Now that clients survive between polls (so
    that their connections do), the bound has to be a real one: the values are
    entire RFC822 messages, attachments included, and a mailbox of scanned
    bundles is tens of megabytes a cycle.

    Small on purpose. The cache exists so that fetching a message and then
    labelling it does not go back to the server twice; it was never meant to
    hold a whole batch.
    """

    _MAX = 8

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self._MAX:
            self.popitem(last=False)


class _ImapPool:
    """Idle connections for one account, capped at what the provider allows.

    `borrow` hands back a live, health-checked connection or opens a new one;
    `give_back` returns it for the next caller. The cap is a semaphore rather
    than a queue size because the expensive thing to bound is *simultaneous*
    connections — Hostinger closes the newest one over its per-account limit,
    which surfaced as random "command timed out" failures mid-batch.
    """

    def __init__(self, max_size: int):
        self._idle: List[imaplib.IMAP4] = []
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, max_size))

    def borrow(self, connect: Callable[[], imaplib.IMAP4]) -> imaplib.IMAP4:
        self._slots.acquire()
        try:
            while True:
                with self._lock:
                    conn = self._idle.pop() if self._idle else None
                if conn is None:
                    return connect()
                if self._alive(conn):
                    return conn
                self._close(conn)
        except BaseException:
            self._slots.release()
            raise

    def give_back(self, conn: imaplib.IMAP4) -> None:
        with self._lock:
            self._idle.append(conn)
        self._slots.release()

    def discard(self, conn: imaplib.IMAP4) -> None:
        self._close(conn)
        self._slots.release()

    @staticmethod
    def _alive(conn: imaplib.IMAP4) -> bool:
        try:
            status, _ = conn.noop()
            return status == "OK"
        except Exception:  # noqa: BLE001 — any failure means "open a fresh one"
            return False

    @staticmethod
    def _close(conn: imaplib.IMAP4) -> None:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


# Guards the one-time folder LIST, which several worker threads sharing a client
# would otherwise all issue at once.
_INDEX_LOCK = threading.Lock()


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
        self._fetched_bytes_cache: dict[str, bytes] = _BoundedBytesCache()

    # ---- message identity -------------------------------------------------- #
    # A UID is only a message *within this account*: every mailbox numbers its
    # own from 1, so two polled accounts hand out the same ids for unrelated
    # mail. Everything downstream — the ledger, the deletion tombstones, the
    # per-message claim — treats the id as global, so the account is attached
    # here, at the one boundary that knows which account this is, and stripped
    # again before anything is said to the server.
    #
    # Doing it here rather than at the call sites is the whole point: a caller
    # cannot forget, because there is no unqualified id to forget to qualify.

    @property
    def account_id(self) -> str:
        """This mailbox, as it appears in every id this client hands out."""
        return self.imap_username or self.smtp_username or ""

    def _uid(self, message_id: str) -> str:
        """The bare UID to send to the server, checked against this account.

        A qualified id belonging to somebody else is a bug in the caller — the
        runner pairing a message with the wrong client — and stripping it
        silently would act on whichever unrelated message happens to hold that
        number here. That is precisely the failure this qualification exists to
        end, so it is raised rather than absorbed.

        An unqualified id is accepted as-is: rows and queued tasks written
        before ids carried an account still name a real UID on this server.
        """
        owner = message_ids.account_of(message_id)
        if owner and owner != self.account_id:
            raise ValueError(
                f"Message {message_id!r} belongs to {owner!r}, not to "
                f"{self.account_id!r}; refusing to act on a UID of that number here."
            )
        return message_ids.local_id_of(message_id)

    def _uid_hint(self, message_id: str) -> str:
        """The UID for the label operations, which try every account on purpose.

        The same résumé is normally delivered to every configured mailbox and
        ingested from one, so filing a delete asks each account in turn and
        "not mine" is an ordinary answer, not an error. A foreign id therefore
        yields no hint rather than raising.

        Yielding *nothing* is the point. `find_message` will, as a last resort
        with no Message-ID to go on, accept a bare UID hint found in INBOX with
        nothing to verify it against — so an unqualified id from another
        account could file whichever unrelated message happened to hold that
        number here. Withholding the hint is what makes that unreachable.
        """
        owner = message_ids.account_of(message_id)
        if owner and owner != self.account_id:
            return ""
        return message_ids.local_id_of(message_id)

    def _connect_imap(self) -> imaplib.IMAP4:
        if self.imap_use_ssl:
            client = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
        else:
            client = imaplib.IMAP4(self.imap_server, self.imap_port)
        if self.imap_username and self.imap_password:
            client.login(self.imap_username, self.imap_password)
        return client

    # ---- connection reuse -------------------------------------------------- #
    # Every fetch, flag and move used to open its own connection: a TCP
    # handshake, a TLS handshake and a LOGIN — three round trips to Hostinger
    # before a single byte of mail moved, repeated four or five times per
    # email. Across a batch that was the largest fixed cost in the poll, and it
    # was paid serially inside each worker thread.
    #
    # So connections are borrowed from a small pool instead. It is bounded
    # rather than thread-local because IMAP providers cap simultaneous
    # connections per account, and that cap has to hold however many worker
    # threads the ingestion runner is using.

    @property
    def _pool(self) -> "_ImapPool":
        """This client's own connections.

        Per instance, not per account. A batch builds one client per mailbox and
        shares it across every worker thread, so an instance-scoped pool gives
        exactly the reuse that matters while keeping one client's connections
        out of another's hands — which is what a second client configured with
        different credentials, or a test that has substituted `_connect_imap`,
        depends on.
        """
        pool = getattr(self, "_imap_pool", None)
        if pool is None:
            pool = _ImapPool(settings.imap_max_connections)
            self._imap_pool = pool
        return pool

    def _account_key(self) -> str:
        return f"{self.imap_server}:{self.imap_port}/{self.imap_username}"

    @contextlib.contextmanager
    def _imap(self):
        """A logged-in connection, returned to the pool instead of logged out.

        A connection that raises is dropped rather than reused: the failure may
        have left the session mid-command, and a poisoned connection handed to
        the next caller is far more expensive than a new handshake.
        """
        pool = self._pool
        conn = pool.borrow(self._connect_imap)
        try:
            yield conn
        except Exception:
            pool.discard(conn)
            raise
        else:
            pool.give_back(conn)

    def _folder_index(self, mail) -> "imap_folders.FolderIndex":
        """This account's folder layout, read once per client and remembered.

        Folder names and the delimiter do not change under a running poll, and
        the LIST that discovers them is a full round trip. Scoped to the client
        rather than to the process for the same reason the connections are: the
        index records folders this client has created, and handing that to a
        client talking to a different server describes a mailbox that does not
        exist.
        """
        with _INDEX_LOCK:
            index = getattr(self, "_imap_index", None)
            if index is None:
                index = imap_folders.read_index(mail)
                self._imap_index = index
            return index

    # ---- searching -------------------------------------------------------- #
    def search_message_ids(self, query: str | None = None, max_results: int | None = None) -> List[str]:
        if not self.imap_server or not self.imap_username:
            log.warning("IMAP server or username not configured.")
            return []

        try:
            with self._imap() as mail:
                mail.select(self.imap_folder)
                # UNSEEN: unread mail is the queue, and a message somebody has
                # opened is one a human has already dealt with. This is the
                # desk's rule and it is deliberate.
                #
                # Know the trade it makes. A résumé that is read before the
                # poller reaches it — somebody checking the inbox on their
                # phone — is skipped from then on, silently, because from here
                # it simply is not in the answer. `ALL` was tried instead and
                # is worse in practice: it re-offers the entire inbox, so the
                # poll spends its batch on old mail that has already been dealt
                # with by hand. Nothing else in the pipeline depends on which
                # of the two is chosen; it is one line, here.
                #
                # The `query` argument is accepted for signature parity with the
                # Gmail client and deliberately unused: Gmail's search syntax
                # means nothing to IMAP, and the label exclusions it carries are
                # already expressed by the message not being in this folder.
                # ...but not the whole history. `SINCE` is evaluated by the
                # server, so mail older than the window is never listed, never
                # fetched and never paid for. Without it the first poll after
                # the UNSEEN change set about OCR'ing years of old inbox mail
                # oldest-first, with today's applicants queued behind it.
                criteria = ["UNSEEN"]
                days = int(getattr(settings, "mail_lookback_days", 0) or 0)
                if days > 0:
                    since = datetime.now(timezone.utc) - timedelta(days=days)
                    # IMAP wants `01-Aug-2026`, in English, always.
                    criteria += ["SINCE", since.strftime("%d-%b-%Y")]

                status, data = mail.uid("search", None, *criteria)
                if status != "OK" or not data or not data[0]:
                    return []

                # Newest first, as it always was. Oldest-first was tried and is
                # wrong here: it is fair to a backlog but it puts today's
                # applicant behind every stale message in the window, so a poll
                # spends its batch on old mail while a CV that arrived a minute
                # ago waits for the next one. The candidate who just applied is
                # the one somebody is waiting to hear about.
                #
                # Nothing is lost to the ordering. What does not fit in a batch
                # stays in the folder and is listed again next poll; the older
                # end of the window drains behind the new mail rather than in
                # front of it.
                raw_uids = data[0].split()
                # Qualified with this account. They really are "just numbers"
                # on the wire, and that was the problem: the caller pools ids
                # from every mailbox into one list and hands them to a ledger
                # that assumed they were unique. They are unique per account,
                # so the account travels with them from here on.
                uids = [
                    message_ids.qualify(self.account_id, u.decode("utf-8"))
                    for u in reversed(raw_uids)
                ]
                # No cap unless one is asked for. These are just numbers; the
                # caller is what decides how many to *work*, after it has
                # dropped the ones it has already judged.
                return uids[:max_results] if max_results else uids
        except Exception as exc:
            log.error("IMAP search_message_ids failed: %s", exc)
            return []

    # ---- fetching --------------------------------------------------------- #
    def get_message(self, message_id: str) -> EmailMessage:
        uid = self._uid(message_id)
        raw_bytes = self._fetched_bytes_cache.get(message_id)
        if not raw_bytes:
            with self._imap() as mail:
                mail.select(self.imap_folder)
                # BODY.PEEK[], never RFC822: a plain RFC822 fetch sets \Seen as
                # a side effect, and `search_message_ids` only ever asks for
                # UNSEEN. Merely *looking* at a message therefore removed it
                # from every future poll — so an email the detector ignored, or
                # one that failed mid-parse, could never be reconsidered. Marking
                # a message read is a decision the runner makes after a
                # successful ingest, not something reading it does by accident.
                status, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
                    raise RuntimeError(f"Could not fetch message body for UID {message_id}")
                raw_bytes = data[0][1]
                self._fetched_bytes_cache[message_id] = raw_bytes

        msg = email.message_from_bytes(raw_bytes)

        raw_from = _decode_header_str(msg.get("From", ""))
        from_addr, from_name = _parse_from(raw_from)
        to_addr = _decode_header_str(msg.get("To", ""))
        subject = _decode_header_str(msg.get("Subject", ""))
        date_str = _decode_header_str(msg.get("Date", ""))
        header_msg_id = _decode_header_str(msg.get("Message-ID", message_id))

        # Attachment handles are built from the bare UID, not the qualified id.
        # They are only ever meaningful inside one message, and the message is
        # already qualified everywhere they are used as part of a key — so
        # repeating the account in them would only lengthen the OCR idempotency
        # keys it appears in.
        body_text, attachments = self._parse_mime_parts(uid, msg)
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
            uid = self._uid(message_id)
            with self._imap() as mail:
                mail.select(self.imap_folder)
                mail.uid("store", uid, "+FLAGS", "(\\Seen)")
        except Exception as exc:
            log.warning("IMAP mark_read failed for UID %s: %s", message_id, exc)

    # ---- labels ------------------------------------------------------------ #
    # On a plain IMAP server a "label" is a folder, and a message is in exactly
    # one of them. That is the whole reason the old add-then-remove pair could
    # not work: applying `Resumes/Deleted` while `Resumes/Processed` was still
    # set is not a state the server can hold, and the two calls addressed the
    # message by a UID that had stopped being valid the moment it was first
    # filed. Both operations are therefore expressed as one move of one message
    # that is *located by its Message-ID header* rather than by a stale UID.

    def _label_folders(self, mail, index) -> List[str]:
        """The folders this deployment files mail into, resolved for this server.

        Searched before the rest of the mailbox because a message being
        re-labelled has almost always been filed once already.
        """
        wanted = [settings.gmail_processed_label, settings.gmail_deleted_label]
        found = []
        for label in wanted:
            if not label:
                continue
            for option in index.candidates_for(label):
                existing = index.existing(option)
                if existing and existing not in found:
                    found.append(existing)
                    break
        return found

    def apply_label(
        self,
        message_id: str,
        label_name: str,
        rfc_message_id: str = "",
        subject: str = "",
        from_addr: str = "",
    ) -> bool:
        """File this message under ``label_name``, wherever it is now.

        Moving is what takes it out of the inbox, and moving *from wherever it
        currently sits* is what lets a message already filed as processed become
        deleted without anyone having to know where it was.

        Returns whether this account ended up holding the message under that
        label — including when it was already there. The same résumé is usually
        delivered to every configured mailbox but ingested once, so "false" is
        an ordinary answer meaning "not mine", and the caller needs to be able
        to tell that apart from "filed", instead of logging success either way.
        """
        if not label_name:
            return False
        try:
            with self._imap() as mail:
                index = self._folder_index(mail)
                target = imap_folders.ensure_folder(mail, index, label_name)
                if not target:
                    log.warning("Could not resolve a folder for label '%s'", label_name)
                    return False

                located = imap_folders.find_message(
                    mail, index,
                    rfc_message_id=rfc_message_id,
                    uid_hint=self._uid_hint(message_id),
                    prefer=self._label_folders(mail, index),
                    subject=subject,
                    from_addr=from_addr,
                )
                if not located:
                    log.info(
                        "%s is not on %s, so there is nothing to file as '%s'",
                        rfc_message_id or f"message {message_id}", self.imap_username, label_name,
                    )
                    return False

                folder, uid = located
                if folder == target:
                    log.info(
                        "Message %s is already filed under '%s' on %s",
                        rfc_message_id or message_id, target, self.imap_username,
                    )
                    return True

                # Flags travel with the message, so \Seen is set before the move
                # rather than after it — afterwards the UID no longer exists.
                if imap_folders.select(mail, folder):
                    try:
                        mail.uid("store", uid, "+FLAGS", "(\\Seen)")
                    except Exception:  # noqa: BLE001 — a read receipt is not the point
                        pass

                if imap_folders.move_message(mail, folder, uid, target):
                    log.info(
                        "Filed %s under '%s' on %s (was in '%s')",
                        rfc_message_id or message_id, target, self.imap_username, folder,
                    )
                    return True
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("IMAP apply_label('%s') failed for %s: %s", label_name, message_id, exc)
        return False

    def remove_label(
        self,
        message_id: str,
        label_name: str,
        rfc_message_id: str = "",
        subject: str = "",
        from_addr: str = "",
    ) -> None:
        """Confirm this message is no longer filed under ``label_name``.

        Every caller pairs this with an `apply_label` for the label the message
        is moving *to*, and on a folder-based server that move is the removal —
        a message is in exactly one folder. So the normal outcome here is to
        find the message already elsewhere and do nothing.

        Two things it deliberately does not do:

        * **Expunge.** The previous implementation flagged the message
          ``\\Deleted`` and expunged it. That is not a label removal; it is the
          recruiter's copy of the application, destroyed.
        * **Return it to the inbox.** Tempting when the paired move failed, but
          an unlabelled message in the inbox is one the next poll picks up and
          ingests again — recreating the candidate the operator just deleted.
          Leaving it where it is keeps it out of the queue and visible to a
          human, which is the right failure.
        """
        if not label_name:
            return
        try:
            with self._imap() as mail:
                index = self._folder_index(mail)
                options = [n for n in (index.existing(o) for o in index.candidates_for(label_name)) if n]
                if not options:
                    return  # No such folder on this account; nothing to leave.

                located = imap_folders.find_message(
                    mail, index,
                    rfc_message_id=rfc_message_id,
                    uid_hint=self._uid_hint(message_id),
                    prefer=options,
                    subject=subject,
                    from_addr=from_addr,
                )
                if not located:
                    return
                folder, uid = located
                if folder not in options:
                    return  # Already somewhere else — the move did its job.

                # Gmail is the one server where a message really can carry two
                # labels at once, so there the removal is a genuine operation.
                if index.is_gmail and imap_folders.select(mail, folder):
                    try:
                        mail.uid("store", uid, "-X-GM-LABELS", label_name)
                        log.info("Removed Gmail label '%s' from %s",
                                 label_name, rfc_message_id or message_id)
                        return
                    except Exception:  # noqa: BLE001 — not a Gmail server after all
                        pass

                log.warning(
                    "%s is still filed under '%s' on %s — the move to its new folder "
                    "did not happen; leaving it there rather than returning it to the "
                    "inbox, where it would be ingested again",
                    rfc_message_id or message_id, folder, self.imap_username,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("IMAP remove_label('%s') failed for %s: %s", label_name, message_id, exc)

    def get_message_by_rfc_id(self, rfc_message_id: str) -> Optional[EmailMessage]:
        """Fetch a message by its Message-ID header, wherever it has been filed.

        `get_message` addresses a UID inside INBOX, which is the right thing
        during a poll and the wrong thing afterwards: once the mail has been
        moved to `Resumes/Processed` that UID belongs to a different message, if
        it exists at all. Recovering a lost résumé months later needs the handle
        that survives filing.
        """
        if not rfc_message_id:
            return None
        try:
            with self._imap() as mail:
                index = self._folder_index(mail)
                located = imap_folders.find_message(
                    mail, index,
                    rfc_message_id=rfc_message_id,
                    prefer=self._label_folders(mail, index),
                )
                if not located:
                    return None

                folder, uid = located
                if not imap_folders.select(mail, folder, readonly=True):
                    return None
                status, data = mail.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
                    return None
                raw = data[0][1]
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch %s on %s: %s", rfc_message_id, self.imap_username, exc)
            return None

        # `get_message` does the parsing; seeding its cache under this UID is
        # what lets it run without going back to the server.
        # Qualified, because the message it hands back is the same kind of
        # object a poll produces and its id has to mean the same thing — this
        # UID is one from whatever folder the message was found in, so it is
        # doubly meaningless without the account attached.
        qualified = message_ids.qualify(self.account_id, uid)
        self._fetched_bytes_cache[qualified] = raw
        try:
            return self.get_message(qualified)
        finally:
            self._fetched_bytes_cache.pop(qualified, None)
