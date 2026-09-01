"""Filing an email, and finding it again after it has been filed.

The reported bug: a processed résumé kept the `Resumes/Processed` label after
its candidate was deleted, instead of moving to `Resumes/Deleted` — on both the
Gmail and the Hostinger account, and in both cases silently. Three causes, one
per layer, all covered here.

1. **The message was addressed by a UID that had already died.** UIDs are per
   folder, so the id recorded at ingestion stopped addressing anything the
   moment the message was first filed. The RFC822 `Message-ID` header is what
   survives the move.
2. **The label was resolved to a folder name by guessing.** Gmail separates
   with `/`, Dovecot with `.` under an `INBOX.` prefix; the server's own `LIST`
   is what settles it.
3. **Remove-then-apply cannot express the change.** On a folder server a
   message is in exactly one folder, so the change is one move — and the old
   `remove_label` did not remove a label at all, it expunged the recruiter's
   copy of the email.
"""
from __future__ import annotations

from app.config import settings
from app.email_client import imap_folders as folders
from app.ingestion.runner import mark_message_done


# --------------------------------------------------------------------------- #
#  A stand-in IMAP server
# --------------------------------------------------------------------------- #
class FakeIMAP:
    """Enough of an IMAP server to hold messages in folders and move them.

    Modelled on Dovecot as cPanel ships it: a `.` delimiter and every folder
    under `INBOX.`, which is the account the bug was reported on.
    """

    def __init__(
        self,
        delimiter: str = ".",
        inbox_prefixed: bool = True,
        header_search: str = "exact",
    ):
        # How this server answers `UID SEARCH HEADER MESSAGE-ID`:
        #   "exact"  — correctly (what the specification implies)
        #   "blind"  — always empty, even for a message it holds. Hostinger.
        #   "fuzzy"  — a message whose id merely starts the same way. Gmail
        #              tokenises the header, and two ids from one sender share
        #              a long prefix.
        self.header_search = header_search
        self.delimiter = delimiter
        self.inbox_prefixed = inbox_prefixed
        # The folders a cPanel mailbox always has. They are what tells the
        # client this server keeps everything under `INBOX.` — with a bare
        # INBOX there is no house style to read, and the code correctly
        # declines to invent one.
        self.folders: dict[str, dict[str, dict]] = {"INBOX": {}}
        if inbox_prefixed:
            for standard in ("Sent", "Drafts", "Trash", "Junk"):
                self.folders[f"INBOX{delimiter}{standard}"] = {}
        self.selected = "INBOX"
        self.expunged: list[tuple[str, str]] = []
        self._next_uid = 1

    # ---- test helpers ---------------------------------------------------- #
    def deliver(self, folder: str, message_id: str, subject: str = "", sender: str = "") -> str:
        uid = str(self._next_uid)
        self._next_uid += 1
        self.folders.setdefault(folder, {})[uid] = {
            "message_id": message_id, "subject": subject, "from": sender, "flags": set(),
        }
        return uid

    def where(self, message_id: str) -> str | None:
        for name, contents in self.folders.items():
            if any(m["message_id"] == message_id for m in contents.values()):
                return name
        return None

    # ---- the IMAP surface ------------------------------------------------ #
    def list(self):
        rows = [
            f'(\\HasNoChildren) "{self.delimiter}" "{name}"'.encode()
            for name in self.folders
        ]
        return "OK", rows

    def create(self, name):
        self.folders.setdefault(name, {})
        return "OK", [b""]

    def subscribe(self, _name):
        return "OK", [b""]

    def select(self, name, readonly=False):
        name = name.strip('"')
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = name
        return "OK", [str(len(self.folders[name])).encode()]

    def expunge(self):
        contents = self.folders[self.selected]
        for uid in [u for u, m in contents.items() if "\\Deleted" in m["flags"]]:
            self.expunged.append((self.selected, contents.pop(uid)["message_id"]))
        return "OK", [b""]

    def noop(self):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]

    def uid(self, command, *args):
        command = command.lower()
        contents = self.folders[self.selected]

        if command == "search":
            _charset, *criteria = args
            return "OK", [b" ".join(u.encode() for u in self._search(contents, criteria))]

        if command == "fetch":
            uid = args[0]
            if uid == "1:*":
                # The header scan: one command, every message in the folder.
                return "OK", [
                    (f"{n} (UID {u} BODY[HEADER.FIELDS (MESSAGE-ID)])".encode(),
                     f"Message-ID: {m['message_id']}\r\n\r\n".encode())
                    for n, (u, m) in enumerate(contents.items(), start=1)
                ]
            if uid not in contents:
                return "OK", [None]
            message = contents[uid]
            header = (
                f"Message-ID: {message['message_id']}\r\n"
                f"Subject: {message['subject']}\r\n"
                f"From: {message['from']}\r\n"
            ).encode()
            return "OK", [(f"{uid} (BODY[HEADER])".encode(), header)]

        if command == "store":
            uid, mode, flags = args
            if uid in contents:
                if mode.startswith("+"):
                    contents[uid]["flags"].add(flags.strip("()"))
            return "OK", [b""]

        if command == "copy":
            uid, target = args[0], args[1].strip('"')
            if uid in contents and target in self.folders:
                self.folders[target][str(self._next_uid)] = dict(contents[uid])
                self._next_uid += 1
                return "OK", [b""]
            return "NO", [b""]

        if command == "move":
            # Deliberately unsupported, so the copy/expunge fallback is what
            # these tests exercise — it is the path Hostinger actually takes.
            raise RuntimeError("MOVE is not supported by this server")

        return "NO", [b""]

    def _search(self, contents: dict, criteria) -> list[str]:
        wanted = [str(c).strip('"') for c in criteria]
        found = []
        for uid, message in contents.items():
            if "MESSAGE-ID" in wanted:
                asked = wanted[wanted.index("MESSAGE-ID") + 1]
                if self.header_search == "blind":
                    continue
                if self.header_search == "fuzzy":
                    # Matches on a shared prefix, the way a tokenising server does.
                    if message["message_id"][:12] == asked[:12]:
                        found.append(uid)
                    continue
                if message["message_id"] == asked:
                    found.append(uid)
            elif "SUBJECT" in wanted:
                if (message["subject"] == wanted[wanted.index("SUBJECT") + 1]
                        and message["from"] == wanted[wanted.index("FROM") + 1]):
                    found.append(uid)
        return found


def _client(server: FakeIMAP):
    from app.email_client.smtp_imap_client import SMTPIMAPClient

    client = SMTPIMAPClient(config={"imap_server": "imap.example.com",
                                    "imap_username": "recruit@example.com"})
    client._connect_imap = lambda: server
    return client


# --------------------------------------------------------------------------- #
#  Folder naming
# --------------------------------------------------------------------------- #
def test_a_label_becomes_the_folder_name_this_server_actually_uses():
    dovecot = folders.FolderIndex(delimiter=".", folders=["INBOX", "INBOX.Sent"],
                                  inbox_prefixed=True)
    assert dovecot.candidates_for("Resumes/Processed")[0] == "INBOX.Resumes.Processed"

    gmail = folders.FolderIndex(delimiter="/", folders=["INBOX", "[Gmail]/All Mail"],
                                inbox_prefixed=False, is_gmail=True)
    assert gmail.candidates_for("Resumes/Processed")[0] == "Resumes/Processed"


def test_the_delimiter_and_the_prefix_are_read_from_the_server_not_assumed():
    dovecot = folders.read_index(FakeIMAP(delimiter="."))
    assert dovecot.delimiter == "."
    assert dovecot.inbox_prefixed is True

    # A bare mailbox states no house style, and one is not invented for it.
    assert folders.read_index(FakeIMAP(inbox_prefixed=False)).inbox_prefixed is False


def test_a_missing_folder_is_created_with_its_parents():
    server = FakeIMAP()
    server.folders["INBOX.Sent"] = {}
    index = folders.read_index(server)

    target = folders.ensure_folder(server, index, "Resumes/Processed")

    assert target == "INBOX.Resumes.Processed"
    assert "INBOX.Resumes" in server.folders, "the parent folder was never created"


# --------------------------------------------------------------------------- #
#  Filing, and re-filing
# --------------------------------------------------------------------------- #
def test_an_ingested_email_is_moved_out_of_the_inbox():
    server = FakeIMAP()
    uid = server.deliver("INBOX", "<cv-1@example.com>", "Resume", "alice@example.com")

    _client(server).apply_label(uid, "Resumes/Processed", rfc_message_id="<cv-1@example.com>")

    assert server.where("<cv-1@example.com>") == "INBOX.Resumes.Processed"
    assert server.folders["INBOX"] == {}, "the mail is still sitting in the inbox"


def test_a_filed_email_can_still_be_found_after_its_uid_has_died():
    """The heart of the bug: the recorded UID is meaningless once it has moved."""
    server = FakeIMAP()
    stale_uid = server.deliver("INBOX", "<cv-2@example.com>", "Resume", "bob@example.com")
    client = _client(server)

    client.apply_label(stale_uid, "Resumes/Processed", rfc_message_id="<cv-2@example.com>")
    # The candidate is deleted. The only handle anyone kept is the stale UID and
    # the header id — and the message is no longer in the folder that issued it.
    client.apply_label(stale_uid, "Resumes/Deleted", rfc_message_id="<cv-2@example.com>")

    assert server.where("<cv-2@example.com>") == "INBOX.Resumes.Deleted"
    assert server.folders["INBOX.Resumes.Processed"] == {}


def test_removing_a_label_never_destroys_the_email():
    """The old implementation flagged \\Deleted and expunged. That is not a
    label removal — it is the recruiter's copy of the application, gone.

    Nor is the message pushed back into the inbox: unlabelled inbox mail is
    exactly what the next poll ingests, which would recreate the candidate the
    operator had just deleted. It stays where it is, and the log says so.
    """
    server = FakeIMAP()
    uid = server.deliver("INBOX", "<cv-3@example.com>", "Resume", "carol@example.com")
    client = _client(server)
    client.apply_label(uid, "Resumes/Processed", rfc_message_id="<cv-3@example.com>")
    # The filing move copies and expunges; what must not happen is a *further*
    # expunge with nowhere for the message to go.
    after_filing = list(server.expunged)

    client.remove_label(uid, "Resumes/Processed", rfc_message_id="<cv-3@example.com>")

    assert server.where("<cv-3@example.com>") == "INBOX.Resumes.Processed"
    assert server.expunged == after_filing, "the email was destroyed"


def test_remove_is_a_no_op_once_the_move_has_already_happened():
    """`apply` then `remove` is the caller's pattern; the second must do nothing."""
    server = FakeIMAP()
    uid = server.deliver("INBOX", "<cv-4@example.com>", "Resume", "dan@example.com")
    client = _client(server)
    client.apply_label(uid, "Resumes/Processed", rfc_message_id="<cv-4@example.com>")

    client.apply_label(uid, "Resumes/Deleted", rfc_message_id="<cv-4@example.com>")
    client.remove_label(uid, "Resumes/Processed", rfc_message_id="<cv-4@example.com>")

    assert server.where("<cv-4@example.com>") == "INBOX.Resumes.Deleted"


def test_a_message_is_never_in_two_label_folders_at_once():
    server = FakeIMAP()
    uid = server.deliver("INBOX", "<cv-5@example.com>", "Resume", "erin@example.com")
    client = _client(server)

    client.apply_label(uid, "Resumes/Processed", rfc_message_id="<cv-5@example.com>")
    client.apply_label(uid, "Resumes/Deleted", rfc_message_id="<cv-5@example.com>")

    holding = [
        name for name, contents in server.folders.items()
        if any(m["message_id"] == "<cv-5@example.com>" for m in contents.values())
    ]
    assert holding == ["INBOX.Resumes.Deleted"], f"the message is in {holding}"


def test_a_message_with_no_header_id_is_matched_on_subject_and_sender():
    """Mail ingested before the header was recorded still has to be re-filed."""
    server = FakeIMAP()
    uid = server.deliver("INBOX", "<cv-6@example.com>", "Welder CV", "frank@example.com")
    client = _client(server)
    client.apply_label(uid, "Resumes/Processed", rfc_message_id="<cv-6@example.com>")

    client.apply_label("9999", "Resumes/Deleted",
                       subject="Welder CV", from_addr="frank@example.com")

    assert server.where("<cv-6@example.com>") == "INBOX.Resumes.Deleted"


# --------------------------------------------------------------------------- #
#  What the runner asks for
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def mark_read(self, message_id):
        self.calls.append(("mark_read", message_id))

    def apply_label(self, message_id, label, **kwargs):
        self.calls.append(("apply", message_id, label, kwargs))

    def remove_label(self, message_id, label, **kwargs):
        self.calls.append(("remove", message_id, label, kwargs))


class _Email:
    message_id = "42"
    thread_id = "<cv-7@example.com>"
    subject = "Resume"
    from_addr = "gina@example.com"


def test_the_runner_passes_the_header_id_so_the_message_can_be_found():
    recorder = _Recorder()

    mark_message_done(recorder, "42", "processed", email=_Email())

    applied = [c for c in recorder.calls if c[0] == "apply"]
    assert applied[0][2] == settings.gmail_processed_label
    assert applied[0][3]["rfc_message_id"] == "<cv-7@example.com>"


def test_deleted_is_applied_before_processed_is_removed():
    """Order matters: applying the label *is* the move on a folder server."""
    recorder = _Recorder()

    mark_message_done(recorder, "42", "suppressed", email=_Email())

    steps = [(c[0], c[2]) for c in recorder.calls if c[0] in ("apply", "remove")]
    assert steps == [
        ("apply", settings.gmail_deleted_label),
        ("remove", settings.gmail_processed_label),
    ]


def test_a_failed_email_is_left_in_the_inbox_to_be_retried():
    """An error is retryable. Filing it as processed hides it from every later
    poll, which is how a transient OCR outage silently lost applications."""
    recorder = _Recorder()

    mark_message_done(recorder, "42", "error", email=_Email())

    assert not [c for c in recorder.calls if c[0] == "apply"]


def test_a_failed_email_is_not_even_marked_read():
    """Unread *is* the queue: `search_message_ids` asks for UNSEEN, so marking a
    failed message read retires it exactly as thoroughly as labelling it would.
    A resume whose OCR failed once must still be offered to the next poll."""
    recorder = _Recorder()

    mark_message_done(recorder, "42", "error", email=_Email())

    assert not [c for c in recorder.calls if c[0] == "mark_read"]


def test_a_client_that_wants_no_extras_still_gets_a_plain_two_argument_call():
    recorder = _Recorder()

    mark_message_done(recorder, "42", "processed", email=None)

    applied = [c for c in recorder.calls if c[0] == "apply"]
    assert applied[0][3] == {}


# --------------------------------------------------------------------------- #
#  Servers that answer the header search badly
# --------------------------------------------------------------------------- #
def test_a_server_whose_header_search_finds_nothing_is_not_believed():
    """Hostinger returns an empty set for a message it is plainly holding.

    That is not an error and not an absence, and treating it as either is what
    left a deleted candidate's resume sitting in Processed. The folder is
    scanned instead, and the message is found and moved.
    """
    server = FakeIMAP(header_search="blind")
    uid = server.deliver("INBOX", "<cv-8@example.com>", "Resume", "hal@example.com")
    client = _client(server)

    client.apply_label(uid, "Resumes/Processed", rfc_message_id="<cv-8@example.com>")
    client.apply_label("stale-uid", "Resumes/Deleted", rfc_message_id="<cv-8@example.com>")

    assert server.where("<cv-8@example.com>") == "INBOX.Resumes.Deleted"


def test_a_server_that_matches_the_wrong_message_is_not_believed_either():
    """Gmail tokenises the header, so a search can return a near-miss.

    Two applications from one candidate share a long Message-ID prefix. Acting
    on the server's answer without reading it back moves the wrong email.
    """
    server = FakeIMAP(header_search="fuzzy")
    other = server.deliver("INBOX", "<CABy-uRAAAA@example.com>", "Resume", "ivy@example.com")
    wanted = server.deliver("INBOX", "<CABy-uRBBBB@example.com>", "Resume", "ivy@example.com")
    client = _client(server)

    client.apply_label(wanted, "Resumes/Deleted", rfc_message_id="<CABy-uRBBBB@example.com>")

    assert server.where("<CABy-uRBBBB@example.com>") == "INBOX.Resumes.Deleted"
    assert server.where("<CABy-uRAAAA@example.com>") == "INBOX", (
        "the other application was moved instead"
    )
    assert other  # the first message is the decoy


def test_a_message_that_is_not_on_this_account_is_left_completely_alone():
    """The same résumé goes to both mailboxes but is ingested once, so a delete
    asks both accounts to re-file it. The account that does not have it must do
    nothing — not find the closest thing and move that."""
    server = FakeIMAP()
    decoy = server.deliver("INBOX", "<someone-else@example.com>", "Resume", "jan@example.com")
    client = _client(server)

    # Same subject, same sender, different message: everything a guess needs.
    client.apply_label("595", "Resumes/Deleted",
                       rfc_message_id="<not-here@example.com>",
                       subject="Resume", from_addr="jan@example.com")

    assert server.where("<someone-else@example.com>") == "INBOX", (
        "an unrelated email was filed as the deleted candidate's"
    )
    assert server.folders.get("INBOX.Resumes.Deleted", {}) == {}
    assert decoy  # the decoy is the point


def test_a_stale_uid_is_never_trusted_when_a_header_id_is_known():
    """A UID is reassigned the moment the message that held it is filed, so an
    id from the ledger addresses somebody else's mail. It moved UID 591 — an
    unrelated inbox message — into Deleted, in production."""
    server = FakeIMAP()
    innocent = server.deliver("INBOX", "<innocent@example.com>", "Invoice", "billing@example.com")
    client = _client(server)

    client.apply_label(innocent, "Resumes/Deleted", rfc_message_id="<gone@example.com>")

    assert server.where("<innocent@example.com>") == "INBOX"


# --------------------------------------------------------------------------- #
#  A permanent refusal must stop being re-read
# --------------------------------------------------------------------------- #
class _Att:
    """Stands in for an AttachmentResult; only `status` is read here."""

    def __init__(self, status):
        self.status = status


def test_a_nationality_rejected_resume_is_filed_so_it_is_not_read_again():
    """The bug this pins: the same CV was OCR'd on every poll, for ever.

    A résumé refused on nationality is a permanent decision — the pipeline says
    so in as many words — but the message came back `skipped`, and `skipped` had
    no branch here at all. So it was never labelled, the next poll's query
    returned it again, and it paid for a full local OCR and a Veris parse to
    arrive at the same refusal.
    """
    recorder = _Recorder()

    mark_message_done(
        recorder, "42", "skipped", email=_Email(),
        attachments=[_Att("rejected_nationality")],
    )

    applied = [c for c in recorder.calls if c[0] == "apply"]
    assert applied, "a permanently refused résumé must be filed as processed"
    assert applied[0][2] == settings.gmail_processed_label


def test_a_message_that_was_never_a_resume_is_left_alone():
    """No attachment verdicts means the detector never accepted it: somebody's
    ordinary mail, which we do not label or mark read."""
    recorder = _Recorder()

    mark_message_done(recorder, "42", "skipped", email=_Email())

    assert recorder.calls == []


def test_a_duplicate_file_is_filed_rather_than_re_fetched_for_ever():
    recorder = _Recorder()

    mark_message_done(
        recorder, "42", "skipped", email=_Email(), attachments=[_Att("duplicate")],
    )

    applied = [c for c in recorder.calls if c[0] == "apply"]
    assert applied[0][2] == settings.gmail_processed_label


def test_an_errored_attachment_still_beats_a_refused_one_to_the_inbox():
    """A message mixing a refusal with a retryable failure arrives here as
    `error`, never `skipped`, so it stays unlabelled and is retried."""
    recorder = _Recorder()

    mark_message_done(
        recorder, "42", "error", email=_Email(),
        attachments=[_Att("rejected_nationality"), _Att("error")],
    )

    assert not [c for c in recorder.calls if c[0] == "apply"]
