"""The mailbox poll task exists, whether or not a timer is calling it.

What this file used to assert — that `poll-gmail` is always on the beat
schedule, at `gmail_poll_interval_seconds` — stopped being true on purpose.
Nothing drains a mailbox until somebody presses Sync, and the timer is put back
only by `mail_autopoll_enabled`. The test went on demanding the old behaviour
and simply failed, against a setting (`gmail_poll_interval_seconds`) that no
longer reaches anything: `_mail_poll_schedule` reads
`mail_poll_interval_seconds`, under the key `poll-mailboxes`.

`tests/test_manual_sync_only.py` owns the schedule now, from both sides — that
the flag is off by default, that the housekeeping sweeps still run without it,
and that turning it on puts the poll back. What is left here is the half that
is still true and is not asserted there: the task has to be registered whether
or not a timer ever calls it, because the manual Sync button dispatches it by
name.
"""

from app.tasks.celery_app import celery_app

TASK_NAME = "app.tasks.jobs.poll_gmail"


def test_mailbox_poll_is_registered_as_a_task():
    import app.tasks.jobs  # noqa: F401

    assert TASK_NAME in celery_app.tasks
