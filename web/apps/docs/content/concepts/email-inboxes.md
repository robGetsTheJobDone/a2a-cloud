# Email inboxes

Every agent can have a **real email address**:

```text
<agent-name>@agents.a2acloud.io
```

Mail to that address becomes a conversation with the agent; the agent's reply
goes back out as email from the same address. Inboxes live on a dedicated mail
subdomain (`agents.a2acloud.io`) with SPF, DKIM, and DMARC managed by the
platform — nothing to configure on your side.

## Enable a mailbox

Declare it in `a2a.yaml`:

```yaml
resources:
  mailbox: true
```

Optionally restrict who may write to the agent. `allowed_senders` is nested
**inside** `resources.mailbox`, so the declaration becomes a mapping:

```yaml
resources:
  mailbox:
    enabled: true
    allowed_senders:
      - alice@example.com
      - reports@partner.co
```

`mailbox: true` is shorthand for `{enabled: true}` with no allowlist. See the
[`a2a.yaml` reference](/reference/a2a-yaml#resources-mailbox).

Or enable it over the API:

```
POST /v1/agents/{name}/mailbox
```

The dashboard shows an **Email inbox** panel on the agent page with the
address, status, sender allowlist, and usage.

## How mail flows

1. An inbound email arrives at the agent's address.
2. Each email conversation (followed via its `References` chain) becomes **one
   dashboard chat thread**, marked with an email badge. The inbound email is
   delivered to the agent as a user message.
3. The agent runs with the **owner's identity and LLM credentials** — an email
   turn is billed and audited like any other turn.
4. The agent's response is sent as an email from the agent's own address, and
   threaded correctly via `In-Reply-To`/`References`, so replies land in the
   sender's existing email thread.

Every step emits audit events — `mail_received`, `mail_sent`, `mail_skipped`,
`mail_send_rate_limited` — which you can query:

```
GET /v1/agents/{name}/mailbox/events
```

## Sender policy

The inbox is **default-deny**:

- The owner's email is always allowed.
- An empty `allowed_senders` list means owner-only.
- Auto-submitted mail (bounces, out-of-office, mailing-list traffic) and mail
  from other agent-domain addresses is filtered out — a loop guard so two
  agents can't email each other into an infinite exchange.
- Outbound replies carry `Auto-Submitted: auto-replied`, so well-behaved
  systems won't auto-respond to your agent either.

## Guardrails

- **50 outbound emails per day per agent.** Over the cap, the agent's replies
  still land in the dashboard thread and are audited
  (`mail_send_rate_limited`) — they just aren't sent as email.
- **100 MiB mailbox quota.**
- **Plan lapse** disables the mailbox; stored mail is kept for 30 days, then
  removed.
- The mailbox is **deleted with the agent**.
- The `From` address is locked to the agent's own address — an agent cannot
  send as anyone else.

## Programmatic access

The runtime injects standard mail credentials into the agent's environment:

```text
A2A_MAIL_ADDRESS      # <agent-name>@agents.a2acloud.io
A2A_MAIL_PASSWORD
A2A_MAIL_IMAP_HOST    # IMAPS
A2A_MAIL_IMAP_PORT    # 993
A2A_MAIL_SMTP_HOST    # STARTTLS
A2A_MAIL_SMTP_PORT    # 587
```

Any IMAP/SMTP library works, but the SDK ships a helper — `ctx.mail`:

```python
@a2a.tool
async def check_inbox(ctx):
    messages = ctx.mail.list_messages(limit=10, unseen_only=True)
    for m in messages:
        body = ctx.mail.read(m.uid)
        ctx.mail.reply(m.uid, f"Got it — processing {m.subject!r}.")
    ctx.mail.send(
        to="reports@partner.co",
        subject="Inbox summary",
        body=f"{len(messages)} new messages handled.",
    )
```

- `list_messages(limit, unseen_only)` — list inbox messages
- `read(uid)` — fetch a message body
- `send(to, subject, body)` — send a new email
- `reply(uid, body)` — reply within an existing thread

## Structured email handler

Instead of receiving email as a plain chat message, an agent can register a
dedicated handler with `@a2a.tool(on_email=True)`. The signature is **forced**
— `(ctx, email: InboundEmailPayload)`:

```python
from a2a_pack import InboundEmailPayload

@a2a.tool(on_email=True)
async def handle_email(ctx, email: InboundEmailPayload):
    if email.attachments:
        names = ", ".join(a["filename"] for a in email.attachments)
        return {
            "subject": f"Re: {email.subject}",
            "body": f"Received {len(email.attachments)} attachment(s): {names}",
        }
    return f"Thanks — I read your message from {email.date}."
```

`InboundEmailPayload` fields:

| Field | Description |
| --- | --- |
| `sender` | The sender's email address |
| `subject` | Subject line |
| `body` | Message body |
| `message_id` | RFC `Message-ID` of the inbound mail |
| `date` | Date header |
| `references` | The `References` chain for the conversation |
| `attachments` | List of `{filename, content_type, size_bytes, content_b64}` — `content_b64` is included for attachments up to 1 MiB, `null` above that |

Return values:

- a `str` — sent as the reply body
- a dict `{"body": ..., "subject": ...}` — reply with an explicit subject
- empty — no reply is sent

One email handler per agent. Agents with a handler advertise the
`a2a:email-handler` tag on their agent card.

## API reference

| Method & path | Purpose |
| --- | --- |
| `GET /v1/agents/{name}/mailbox` | Mailbox status, address, allowlist, usage |
| `POST /v1/agents/{name}/mailbox` | Enable the mailbox |
| `PATCH /v1/agents/{name}/mailbox` | Update settings (e.g. the sender allowlist) |
| `DELETE /v1/agents/{name}/mailbox` | Disable and remove the mailbox |
| `GET /v1/agents/{name}/mailbox/events` | Audit events: `mail_received`, `mail_sent`, `mail_skipped`, `mail_send_rate_limited` |

See also [Agents](/concepts/agents) and
[LLM credentials](/concepts/llm-credentials).
