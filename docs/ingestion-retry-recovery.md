# Recovering an email intake failure

OCR completion and candidate creation are separate steps. An accepted parser
answer is saved before candidate lookup, file storage, or insertion. Retrying
the same mailbox message, attachment handle, and source bytes reuses that
answer without another OCR request.

## Automatic behavior

- A failed intake attempt waits 90 seconds before retrying; the second failure
  waits 180 seconds. The third failed attempt quarantines the attachment.
- An accepted OCR job that is still queued or running is deferred without
  consuming the intake failure budget or starting a synchronous extraction.
- A live intake lease prevents concurrent processing. Expired workers cannot
  publish cached answers or settle another worker's checkpoint.
- Subsequent polls return an intake error for a quarantined attachment without
  parsing it again. Quarantine itself does not mark mail processed. An email
  whose candidate attachments all fail remains unread for recovery.

## Review and recover

Use an administrator's API session; staff accounts cannot access these routes.

1. Request `GET /ingest/checkpoints/review`. Each item contains its
   `checkpoint_id`, mailbox account, message ID, filename, failed attempt count,
   last error, and timestamps. It does not return extracted profile contents.
2. Resolve the reported candidate data, storage, database, or extraction error.
   Successful OCR alone does not mean the candidate write succeeded.
3. Request `POST /ingest/checkpoints/{checkpoint_id}/retry` for that item.
   This clears quarantine and the intake failure count. The next mailbox poll
   attempts intake again, reusing any unexpired saved answer. A missing or
   non-quarantined ID returns 404.
4. Verify the candidate or intended duplicate verdict and that the mailbox
   message is filed by the normal runner. Review newly reported errors if
   recovery fails again.

OCR worker failures are separate: use `GET /ingest/ocr-state/review` and its
existing row retry endpoint for jobs abandoned by the OCR reconciler. Resetting
an intake checkpoint does not reset a worker job's retry limit.

## Retention and deployment

Parsed answers live in `ingestion_extractions` with a 30-day `expires_at` TTL
index. A settled candidate's cached answer is removed immediately. Supporting
document reads can remain until expiry so a sibling CV retry can reuse them.
Compact status and quarantine metadata live in `ingestion_checkpoints`.

Cache expiry never clears quarantine or automatically purchases another
extraction. A previously saved answer that expired or is missing stops intake
for review. An explicit administrator retry may purchase a fresh extraction
when its previous answer is no longer retained.

Deployment is additive: normal startup `ensure_indexes()` creates the review
and TTL indexes and verifies `expires_at` has `expireAfterSeconds: 0`. A missing
or conflicting TTL index raises an index-setup error; correct it before enabling
mail ingestion. No candidate records or existing OCR rows need a migration.
This cache covers attempts after deployment; it does not reconstruct parser
answers from earlier incident history.

For local backend tests, install `requirements-dev.txt` and run
`python -m pytest tests -q`. The test fixtures use isolated mongomock databases
and refuse connections to the real MongoDB.
