# VPS deployment — Redis + background ingestion

Runs the Gmail poll off the API's request path. Without this the app still
works: `/ingest/poll` runs a cycle inline and the request is held open for the
whole batch. The worker is an optimisation, not a requirement — set it up when
inline polling starts timing out or blocking the UI.

Written for Ubuntu/Debian. On RHEL-family swap `apt` for `dnf` and the Redis
unit name is `redis` rather than `redis-server`.

Paths below assume the app lives at `/opt/resume-ingest` with a virtualenv at
`/opt/resume-ingest/.venv`. Adjust both service files if yours differ.

## 1. Redis

```bash
sudo apt update && sudo apt install -y redis-server
```

Confirm it only listens locally. In `/etc/redis/redis.conf`:

```
bind 127.0.0.1 ::1
```

This Redis holds the job queue and the poll lock, and it has no password. If it
is reachable from the internet, anyone who finds port 6379 can read and queue
your jobs. Bind it to loopback and leave it there — the worker and the API both
run on the same host, so nothing needs it exposed.

```bash
sudo systemctl enable --now redis-server
redis-cli ping        # -> PONG
```

## 2. Gmail credentials

The OAuth consent flow opens a browser, which a server does not have. Authorise
once on your own machine:

```bash
python -m app.cli auth
```

then copy the resulting `secrets/gmail_token.json` up to the server. Without it
the worker starts cleanly and then fails every poll on missing credentials.

## 3. Environment

In the server's `.env`:

```
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
REDIS_URL=redis://localhost:6379/0
POLL_LOCK_TTL_SECONDS=1800
```

`POLL_LOCK_TTL_SECONDS` is how long one cycle may hold the poll lock before it
is presumed dead. It must exceed a worst-case batch: too short and a second
cycle starts on top of a live one, which is the exact thing the lock prevents.

## 4. Services

```bash
sudo mkdir -p /var/lib/resume-ingest
sudo chown YOUR_SERVICE_USER /var/lib/resume-ingest

sudo cp deploy/resume-worker.service deploy/resume-beat.service /etc/systemd/system/
sudo sed -i 's/REPLACE_WITH_SERVICE_USER/YOUR_SERVICE_USER/' \
    /etc/systemd/system/resume-worker.service \
    /etc/systemd/system/resume-beat.service

sudo systemctl daemon-reload
sudo systemctl enable --now resume-worker resume-beat
```

Check they came up:

```bash
systemctl status resume-worker resume-beat
journalctl -u resume-worker -f
```

The worker log should list three registered tasks: `run_poll_cycle`,
`poll_gmail`, and `process_message`.

Run **one** beat, on one host. A second scheduler means two ticks per interval;
they collide on the poll lock rather than double-ingesting, but then half the
scheduled polls do nothing.

## 5. Verify

```bash
curl -H "Authorization: Bearer $TOKEN" https://your-host/ingest/workers
# {"available": true}
```

`available: false` means the API cannot see a worker, and every sync will fall
back to running inline. Check that the API and the worker read the same
`REDIS_URL`, and that `resume-worker` is actually running.

## Retiring the old watcher

This replaces the `python -m app.cli watch` supervisor loop
(`scripts/run_background_watcher.ps1`). Do not run both — they poll Gmail
independently, and only the Celery path takes the lock, so the watcher can
start a cycle on top of a scheduled one.

The `run-once` and `watch` CLI commands still work and are unaffected.
