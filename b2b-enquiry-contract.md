# B2B enquiries — the contract the WhatsApp bot writes against

The bot talks to two kinds of people. A **candidate** answers questions about
themselves and becomes a row in `candidates`, through `POST /candidates`. An
**agent** — or an association, or a company hiring under contract — describes a
vacancy and asks the agency to fill it, and that becomes a row in
`b2b_enquiries`, through `POST /b2b-enquiries`.

They are two collections because filing a company as a candidate would put it in
a recruiter's review queue and allocate it to a staff member as if it were a
person. Nothing about the candidate flow changes.

The CRM side is built. This file is what the bot has to send.

---

## Before replying to any inbound message

The bot must check the sender before it creates or resumes conversation state,
and before it sends text, a template, a menu, or an acknowledgement:

```http
POST {CRM_API_URL}/whatsapp/reply-policy
X-Service-Key: {WHATSAPP_SERVICE_KEY}
Content-Type: application/json

{"phone":"+919876543210"}
```

The CRM normalises formatting and country-code variants, then checks the number
against every Sourcing Hub contact and every internal user account, including
inactive staff. It returns one of these decisions:

```jsonc
{"should_reply":false,"action":"ignore","reason":"sourcing_contact_number"}
{"should_reply":false,"action":"ignore","reason":"internal_user_number"}
{"should_reply":true,"action":"continue","reason":"external_sender"}
```

`action: "ignore"` means silence: do not send a message and do not mutate that
sender's conversation state. The policy is fail-closed. A timeout, non-2xx
response, malformed body, or `policy_lookup_unavailable` must also be treated as
`ignore`; being unable to check is not permission to reply to a staff member or
commercial contact.

---

## The endpoint

```
POST {CRM_API_URL}/b2b-enquiries
X-Service-Key: {WHATSAPP_SERVICE_KEY}
Content-Type: application/json
```

Same service key as `POST /candidates`, `GET /taxonomy` and
`GET /policy/cv-required`. It is the same system on the other end of the wire,
and a second secret would be a second thing to rotate for no gain in what either
one protects.

### Body

```jsonc
{
  // Required. Unique per ENQUIRY, not per sender — see the note below.
  "idempotency_key": "whatsapp/{phone_number_id}/{wa_user_id}/{message_id}",

  // Required. The only field a row cannot be rendered without. Fall back to the
  // sender's WhatsApp display name, then to their number.
  "contact_name": "Ravi Kumar",

  // Everything below is optional. Send what the conversation produced.
  "party_type": "agent",              // agent | association | client
  "company_name": "Ravi Manpower Services",
  "phone": "+91 98765 43210",
  "phone_e164": "+919876543210",
  "email": "ravi@manpower.example",
  "country": "India",
  "city": "Chennai",

  "requirement": "Need 40 welders for a Qatar site, joining before Ramadan.",
  "job_title": "Structural Welder",
  "job_id": "structural_welder",      // from GET /taxonomy, when they picked
  "headcount": 40,
  "destination_country": "Qatar",
  "salary_budget": "QAR 2,200/month",
  "experience_required": "3+ years",
  "skills": ["welding", "rigging"],   // a comma-separated string is also accepted
  "needed_by": "before Ramadan",      // free text — that is how it gets answered
  "notes": "Prefers candidates with a valid passport.",

  "wa_user_id": "919876543210"
}
```

### Responses

| Status | Meaning |
| --- | --- |
| `201` | Filed. `created: true`. |
| `200` | A replay of an `idempotency_key` already accepted. `created: false`, same `enquiry_id`. |
| `401` | Missing or wrong `X-Service-Key`. |
| `422` | `contact_name` or `idempotency_key` missing, or a field over its length cap. |

```json
{
  "success": true,
  "created": true,
  "enquiry_id": "ENQ-3F9A21C4",
  "status": "new",
  "enquiry": { }
}
```

`enquiry_id` is short enough to read down a phone line. Quote it back to the
agent — it is the reference the agency will use.

---

## The three things that are easy to get wrong

**1. The key is per enquiry, not per sender.**
The candidate intake keys on `whatsapp/{phone_number_id}/{wa_user_id}` because a
person registers once. An agent raises a requirement in March and another in
June, and both are real vacancies. Reusing the sender's key would silently file
the second one as a replay of the first, return `200` with the March enquiry's
id, and nobody would find out until a client asked why nothing had happened.
Put the message id — or a submission counter — on the end.

**2. Fields not on the list are dropped, not rejected.**
The intake model is an allow-list with `extra="ignore"`, exactly like
`WhatsAppProfileIn`. A field the bot adds without a matching CRM change is
**silently discarded** — a `200`/`201` is not evidence it was stored. Changes to
what the bot sends are not done until both sides ship. Verify against the
deployed `openapi.json`, which is public and is the fastest way to see what the
running version actually accepts.

**3. Send absence as absence.**
Do not fill `headcount` with `0` or `1` when the agent did not give a number —
omit it. The CRM stores absence as absence and the screen says "Not stated". A
`0` that survives to a converted job order makes a requisition that is `FILLED`
the moment it is raised and vanishes from the list it was raised to appear on.

---

## What the CRM does and does not do with it

**Does:** stores what was said; matches the sender against `sourcing_clients` by
phone (last ten digits, then company name) so a known agent's enquiry arrives
carrying their name; shows it on the **B2B Enquiries** screen.

**Does not:** create a job order, allocate anyone, or add the sender to the
Sourcing Hub. A number that messaged the bot is not an account the agency has
agreed to work with. Every one of those is a decision, and a recruiter takes it
on the screen — converting an enquiry writes the job order and stamps the
enquiry with its id in one step, and converting twice is refused.

---

## Recruiter-facing endpoints

Admin session, not the service key — the bot cannot reach these, deliberately.
`GET /b2b-enquiries` returns every company that has been in touch and what they
are hiring for.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/b2b-enquiries?status=` | List, newest first, with per-state counts. |
| `POST` | `/b2b-enquiries/manual` | Log one that came in by phone or email. |
| `GET` | `/b2b-enquiries/{id}` | One enquiry. |
| `PATCH` | `/b2b-enquiries/{id}` | Partial edit, or move it along. |
| `POST` | `/b2b-enquiries/{id}/convert` | Raise the job order and stamp the enquiry. |
| `DELETE` | `/b2b-enquiries/{id}` | Remove a duplicate or a wrong number. |

`status` is one of `new`, `reviewing`, `converted`, `closed`. `PATCH` refuses
`converted`: it means a job order exists, and only converting can make that true.

---

## Checking it before shipping the bot side

The cross-repo contract is where this breaks, and reading both schemas and
trusting they agree is how it breaks quietly. Feed a payload the bot actually
builds into the CRM's own `TestClient` — `tests/test_b2b_enquiries.py` has the
fixtures — and assert on the stored document, not on the status code.
