# WhatsApp group intake — timesheets and agent-posted profiles

**Status: blocked by an external dependency.** The receiving half is built,
tested and disabled. Nothing upstream can feed it yet.

## The blocker

The bot service this CRM talks to is built on the **WhatsApp Cloud API**, which
delivers one-to-one conversations only. Its webhook has no group message event,
and no payload it sends carries a group identifier. `WhatsAppReplyPolicyIn` in
`app/api/routes.py` states the same assumption in passing: *"the sender identity
**Meta** supplies with every inbound message."*

Group delivery is therefore not a switch this system can flip. It needs either:

* a provider that exposes group messages — the WhatsApp **On-Premises API**, or
  a broker that bridges one; or
* a different transport entirely.

Either way the change is in the bot service, which owns the Meta credentials,
the agency's number, the send budget and the rate limiter. This system holds
none of those, deliberately — see `settings.wa_bot_url`.

## What is built

| Piece | Where |
| --- | --- |
| Inbound contract (`GroupMessage`) | `app/whatsapp/groups.py` |
| Group registry — which groups, and what for | `app/whatsapp/groups.py` |
| Timesheet handler | `handle_timesheet` |
| Agent-profile handler | `handle_agent_profile` |
| Routing | `route_group_message` |
| HTTP ingress | `POST /whatsapp/group-events` |
| Registry admin | `GET/POST/DELETE /whatsapp/groups` |
| Tests | `tests/test_whatsapp_groups.py` |

`GroupMessage` is **not** a guess at Meta's webhook format. It is what this
system needs to know; mapping a provider's payload onto it is the bot's job,
precisely so a provider change does not reach into the CRM.

## Turning it on

Set `WHATSAPP_GROUP_INTAKE_ENABLED=true` once the bot can forward group
messages. That is the only change required here.

While it is off, `POST /whatsapp/group-events` answers **503** naming this
blocker, rather than accepting traffic that cannot arrive.

## The security model

A group message is an instruction from whoever is in the group, so:

1. **A group does nothing until an administrator registers it** and says what it
   is for (`timesheet` or `agent_profiles`). Without this, being added to a
   group would be enough to punch somebody else's attendance or inject
   candidates.
2. **The sender is authenticated by CRM phone number**, and the name they write
   is checked against their CRM account — a handset proves a phone, not a
   person. This is `resolve_employee`, shared with the private-chat attendance
   route so both agree on what counts as a match.
3. **The message id is the idempotency key**, so a redelivered webhook neither
   punches twice nor creates a second candidate.
4. **Ordinary conversation is ignored**, not rejected. A team group carries
   chatter, and answering "morning all" with an error helps nobody.

## Two things that are not the same

An **agent raising a manpower requirement** is a `b2b_enquiries` row — a
vacancy. An **agent posting somebody's details** is a person, and goes into
`candidates` with everybody else, through `intake_whatsapp_candidate`. That
service already owns identity resolution, passport claiming and de-duplication,
so a candidate posted twice — or posted after registering themselves — is
matched to the existing record rather than added beside it.
