"""Where a message actually lives on an IMAP server, and how to move it there.

Two facts about IMAP break naive labelling, and both are why a processed email
kept its old label instead of moving to the new one.

**A UID is not an identity.** UIDs are allocated per folder. The moment a
message is moved out of ``INBOX`` into ``Resumes/Processed`` it is given a new
UID, and the id the ingestion ledger recorded no longer addresses anything at
all — a later ``remove_label`` looked for it in the folder it had already left,
found nothing, and reported success. The stable handle is the RFC822
``Message-ID`` header: it survives the move, it is the same string on every
account the mail was delivered to, and the server can search on it directly.

**A label is not a folder name.** Gmail exposes labels over IMAP as folders
separated by ``/``; Hostinger (Dovecot, via cPanel) uses ``.`` under an
``INBOX.`` prefix. The one configured label ``Resumes/Processed`` therefore has
to become ``Resumes/Processed`` on one account and ``INBOX.Resumes.Processed``
on the other. Guessing produced two half-created folder trees, so nothing here
guesses: the delimiter and the prefix in use come out of the server's own
``LIST`` response, and a folder is only created under the shape the server
already uses for everything else.

The unit of work is a *move*, not an add-then-remove. One ``UID MOVE`` (or a
copy/flag/expunge where the server lacks the extension) is what makes "processed
becomes deleted" atomic — the message cannot end up carrying both labels, or
neither, the way two independent calls allowed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.logging_config import get_logger

log = get_logger(__name__)

# `(\HasNoChildren) "." "INBOX.Resumes.Processed"` — flags, delimiter, name.
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+"?(?P<delim>[^"\s]*)"?\s+(?P<name>.*)')


def _unquote(raw: bytes) -> str:
    name = raw.decode("utf-8", errors="replace").strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1]
    return name


@dataclass
class FolderIndex:
    """The folders one account actually has, and the shape of their names."""

    delimiter: str = "."
    folders: List[str] = field(default_factory=list)
    #: True when the server keeps every folder under `INBOX.` (Dovecot/cPanel).
    inbox_prefixed: bool = False
    is_gmail: bool = False
    #: Lower-cased name → the name as the server spells it. IMAP folder names
    #: are case-sensitive to the server but recruiters type them either way.
    _by_lower: Dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_lower = {f.lower(): f for f in self.folders}

    def existing(self, name: str) -> Optional[str]:
        return self._by_lower.get(name.lower())

    def note(self, name: str) -> None:
        if name.lower() not in self._by_lower:
            self.folders.append(name)
            self._by_lower[name.lower()] = name

    def candidates_for(self, label: str) -> List[str]:
        """Every spelling of ``label`` this server might already be using.

        Ordered most-likely first. The first one that exists wins; if none do,
        the first is what gets created.
        """
        parts = [p for p in re.split(r"[/.]", label) if p]
        if not parts:
            return []
        joined = self.delimiter.join(parts)
        prefixed = "INBOX" + self.delimiter + joined
        ordered = [prefixed, joined] if self.inbox_prefixed else [joined, prefixed]
        # The literal label as configured, in case the server takes it verbatim.
        for extra in (label, label.replace("/", "."), label.replace(".", "/")):
            if extra not in ordered:
                ordered.append(extra)
        return ordered


def read_index(mail) -> FolderIndex:
    """Ask the server what folders it has and how it separates their names."""
    delimiter = "."
    names: List[str] = []
    try:
        status, rows = mail.list()
    except Exception as exc:  # noqa: BLE001 — an unreadable LIST is not fatal
        log.debug("IMAP LIST failed (%s); assuming '.' delimiter", exc)
        status, rows = "NO", []

    if status == "OK":
        for row in rows or []:
            if not isinstance(row, (bytes, bytearray)):
                continue
            match = _LIST_RE.match(bytes(row))
            if not match:
                continue
            found = match.group("delim").decode("ascii", errors="replace")
            if found in ("/", "."):
                delimiter = found
            names.append(_unquote(match.group("name")))

    non_inbox = [n for n in names if n.upper() != "INBOX"]
    prefix = ("INBOX" + delimiter).upper()
    prefixed = [n for n in non_inbox if n.upper().startswith(prefix)]
    index = FolderIndex(
        delimiter=delimiter,
        folders=names,
        # Only when it is the house style, not because one stray folder has it.
        inbox_prefixed=bool(non_inbox) and len(prefixed) >= len(non_inbox) / 2,
        is_gmail=any(n.startswith("[Gmail]") or n.startswith("[Google Mail]") for n in names),
    )
    log.debug(
        "IMAP folders: delimiter=%r inbox_prefixed=%s gmail=%s (%d folder(s))",
        index.delimiter, index.inbox_prefixed, index.is_gmail, len(names),
    )
    return index


def ensure_folder(mail, index: FolderIndex, label: str) -> Optional[str]:
    """The server-side name for ``label``, creating it if it is not there yet.

    Parents are created first because a server that auto-creates them is the
    lucky case, not the guaranteed one — ``CREATE INBOX.Resumes.Processed``
    fails outright on a Dovecot that has no ``INBOX.Resumes``.
    """
    options = index.candidates_for(label)
    if not options:
        return None

    for name in options:
        existing = index.existing(name)
        if existing:
            return existing

    target = options[0]
    parts = target.split(index.delimiter)
    for depth in range(1, len(parts) + 1):
        branch = index.delimiter.join(parts[:depth])
        if index.existing(branch):
            continue
        try:
            mail.create(branch)
        except Exception as exc:  # noqa: BLE001 — "already exists" arrives as one
            log.debug("CREATE %s: %s", branch, exc)
        try:
            # Unsubscribed folders are invisible in most desktop clients, which
            # reads to an operator as "the mail was never filed".
            mail.subscribe(branch)
        except Exception:  # noqa: BLE001
            pass
        index.note(branch)

    log.info("Created IMAP folder '%s' for label '%s'", target, label)
    return target


def _quoted(folder: str) -> str:
    return '"' + folder + '"' if " " in folder else folder


def select(mail, folder: str, readonly: bool = False) -> bool:
    try:
        status, _ = mail.select(_quoted(folder), readonly)
        return status == "OK"
    except Exception:  # noqa: BLE001
        return False


def _uid_search(mail, *criteria: str) -> List[str]:
    try:
        status, data = mail.uid("search", None, *criteria)
    except Exception as exc:  # noqa: BLE001
        log.debug("UID SEARCH %s failed: %s", criteria, exc)
        return []
    if status != "OK" or not data or not data[0]:
        return []
    return [u.decode() for u in data[0].split() if u]


def search_order(index: FolderIndex, prefer: Sequence[str] = ()) -> List[str]:
    """Which folders to look in, cheapest and likeliest first.

    The label folders come before INBOX because by the time anything asks where
    a message is, it has usually already been filed.
    """
    order: List[str] = []
    for name in list(prefer) + ["INBOX"]:
        if name and name not in order:
            order.append(name)
    if index.is_gmail:
        for name in index.folders:
            if name.endswith("All Mail") and name not in order:
                order.append(name)
    for name in index.folders:
        upper = name.upper()
        # Trash and Spam are where a message goes to stop mattering; Sent and
        # Drafts never hold received mail. Searching them is pure latency.
        if any(skip in upper for skip in ("TRASH", "SPAM", "JUNK", "SENT", "DRAFT")):
            continue
        if name not in order:
            order.append(name)
    return order


def normalise_message_id(rfc_message_id: str) -> str:
    """``<abc@host>`` — the canonical form, for comparing two header values."""
    value = (rfc_message_id or "").strip()
    if value and not value.startswith("<"):
        value = "<" + value + ">"
    return value


#: How far back a header scan will read. One `UID FETCH` of this many
#: `Message-ID` headers is a single round trip and a few hundred kilobytes; the
#: bound exists so an enormous archive folder cannot stall a delete.
_HEADER_SCAN_LIMIT = 3000

_MESSAGE_ID_HEADER = re.compile(rb"message-id:\s*(<[^>]+>)", re.IGNORECASE)


def message_id_index(mail, limit: int = _HEADER_SCAN_LIMIT) -> Dict[str, str]:
    """``{Message-ID: UID}`` for the selected folder, read in one command.

    This exists because **server-side header search cannot be relied on**.
    Hostinger's IMAP answers `UID SEARCH HEADER MESSAGE-ID "<...>"` with an
    empty set for a message that is demonstrably in the folder — no error, just
    nothing — and every variation of quoting and casing behaves the same way.
    A search that silently finds nothing is worse than one that fails, because
    the caller moves on to its fallback and acts on a guess.

    So the mapping is built here instead: one `UID FETCH` for the
    ``Message-ID`` header of every message in the folder, which is a single
    round trip and a header-only read. Correct on every server, and the only
    thing that makes "find this message again after it was filed" dependable.
    """
    try:
        status, data = mail.uid("fetch", f"1:*", "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    except Exception as exc:  # noqa: BLE001
        log.debug("Header scan failed: %s", exc)
        return {}
    if status != "OK" or not data:
        return {}

    found: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        # The envelope looks like `123 (UID 456 BODY[HEADER.FIELDS ...] {60}`.
        envelope = item[0] if isinstance(item[0], (bytes, bytearray)) else b""
        match = re.search(rb"UID\s+(\d+)", bytes(envelope))
        if not match:
            continue
        uid = match.group(1).decode()
        header = item[1] if isinstance(item[1], (bytes, bytearray)) else b""
        found_id = _MESSAGE_ID_HEADER.search(bytes(header))
        if found_id:
            found[found_id.group(1).decode("utf-8", errors="replace").strip()] = uid
        if len(found) >= limit:
            break
    return found


def _header_of(mail, uid: str) -> str:
    """The Message-ID / Subject / From block of one message, as text."""
    try:
        status, data = mail.uid(
            "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM)])",
        )
    except Exception:  # noqa: BLE001
        return ""
    if status != "OK" or not data or not data[0] or not isinstance(data[0], tuple):
        return ""
    return data[0][1].decode("utf-8", errors="replace")


def find_message(
    mail,
    index: FolderIndex,
    rfc_message_id: str = "",
    uid_hint: str = "",
    prefer: Sequence[str] = (),
    subject: str = "",
    from_addr: str = "",
) -> Optional[Tuple[str, str]]:
    """``(folder, uid)`` for one message, wherever on this account it is.

    The ``Message-ID`` header is the key. It is looked up two ways, because one
    of them does not work everywhere:

    1. A server-side ``UID SEARCH``, which is cheap and correct on Gmail.
    2. A header scan of the folder, when the search finds nothing. Hostinger
       answers that search with an empty set for messages it is plainly
       holding, so "the search found nothing" is not evidence of absence and
       must never be treated as such.

    Nothing is ever returned without its ``Message-ID`` having been *seen* to
    match. The previous version trusted a stale UID inside INBOX without
    checking, and a UID is reassigned to an unrelated message the moment the
    original is filed — so a delete moved whichever innocent email happened to
    hold that number. A message that cannot be positively identified is left
    alone.
    """
    normalised = normalise_message_id(rfc_message_id)
    folders = search_order(index, prefer)

    if normalised:
        for folder in folders:
            if not select(mail, folder, readonly=True):
                continue

            # The server's answer is a *shortlist*, never a verdict. Gmail
            # tokenises the header and will return a message whose id merely
            # begins the same way — two ids from the same sender can share a
            # long prefix — so each hit is read back and compared in full.
            for uid in reversed(_uid_search(mail, "HEADER", "MESSAGE-ID", '"' + normalised + '"')):
                if normalised in _header_of(mail, uid):
                    log.debug("Found %s in '%s' as UID %s", normalised, folder, uid)
                    return folder, uid
                log.debug(
                    "Server matched UID %s in '%s' for %s, but its header says otherwise",
                    uid, folder, normalised,
                )

            uid = message_id_index(mail).get(normalised)
            if uid:
                log.info(
                    "Found %s in '%s' as UID %s by header scan — this server's "
                    "HEADER search did not match it",
                    normalised, folder, uid,
                )
                return folder, uid

        # Every folder has now been searched *and* scanned for an exact header
        # match. There is no weaker test worth running: the same résumé is
        # normally sent to both mailboxes but only ingested once, so "this
        # account does not have it" is the ordinary answer, not a near miss to
        # be resolved by guessing. Falling back to subject-and-sender here is
        # what filed an unrelated email — another "Resume" from the same
        # candidate — as the deleted one.
        log.info(
            "%s is not on this account; nothing to re-file here", normalised,
        )
        return None

    # No Message-ID at all — mail recorded before the header was kept. Both
    # remaining routes verify whatever they find before returning it.
    if subject and from_addr:
        for folder in folders:
            if not select(mail, folder, readonly=True):
                continue
            uids = _uid_search(
                mail,
                "HEADER", "SUBJECT", '"' + subject + '"',
                "HEADER", "FROM", '"' + from_addr + '"',
            )
            for uid in reversed(uids):
                header = _header_of(mail, uid)
                if subject in header and from_addr in header:
                    return folder, uid

    if uid_hint:
        for folder in folders:
            if not select(mail, folder, readonly=True):
                continue
            header = _header_of(mail, uid_hint)
            if not header:
                continue
            if subject and from_addr:
                if subject in header and from_addr in header:
                    return folder, uid_hint
                continue
            if folder.upper() == "INBOX":
                # Nothing to verify against at all. Accepted only in the folder
                # that issued the UID, and only for a message still sitting
                # there unfiled.
                return folder, uid_hint

    return None


def move_message(mail, folder: str, uid: str, target: str) -> bool:
    """Move one message between folders, atomically where the server allows it.

    ``UID MOVE`` (RFC 6851) is one round trip and leaves no window in which the
    message exists in both places or in neither. Servers without it get the
    classic copy / flag / expunge, which is the same thing with a gap in the
    middle.
    """
    if folder == target:
        return True
    if not select(mail, folder):
        return False

    try:
        status, _ = mail.uid("move", uid, _quoted(target))
        if status == "OK":
            log.info("Moved UID %s from '%s' to '%s'", uid, folder, target)
            return True
    except Exception as exc:  # noqa: BLE001 — no MOVE extension; fall through
        log.debug("UID MOVE unavailable on this server (%s); copying instead", exc)

    try:
        status, _ = mail.uid("copy", uid, _quoted(target))
        if status != "OK":
            log.warning("Could not copy UID %s from '%s' to '%s'", uid, folder, target)
            return False
        mail.uid("store", uid, "+FLAGS", "(\\Deleted)")
        mail.expunge()
        log.info("Copied UID %s from '%s' to '%s' and expunged the original", uid, folder, target)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Move of UID %s from '%s' to '%s' failed: %s", uid, folder, target, exc)
        return False
