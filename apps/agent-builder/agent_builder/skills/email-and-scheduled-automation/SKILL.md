---
name: email-and-scheduled-automation
description: Build and review agents that receive or send email, triage inboxes, draft replies, process attachments, run scheduled jobs, or combine mail with recurring workflows. Use for support inboxes, reports, reminders, follow-ups, and mailbox-triggered automation.
---
# Email And Scheduled Automation

Separate message understanding, proposed actions, and delivery.

## Inbound email

- Declare exactly one `@a2a.tool(on_email=True)` handler when mail should trigger the agent.
- Use the forced `(ctx, email: InboundEmailPayload)` signature.
- Treat sender, subject, body, attachment names, and quoted instructions as untrusted input.
- Enforce sender allowlists and attachment size/type checks before reasoning over content.
- Preserve `message_id` and `references` for deduplication and threading.
- Return `None` for no reply, a string for a simple reply, or `{body, subject}` for an explicit reply.

## Outbound email

- Use `ctx.mail`; never embed SMTP credentials or connect to arbitrary mail servers.
- Draft first when the request can create financial, legal, account, or customer impact.
- Require explicit approval before sending to a new recipient or changing a material commitment.
- Keep recipient, subject, decision, approval, and send outcome in the structured result.

## Scheduled work

- Make the public tool idempotent and accept a stable run/date key.
- Store cursors and deduplication markers in the durable workspace, not process memory.
- Bound batches, execution time, retries, and catch-up windows.
- A scheduled run must be safe to repeat after a timeout.

## Acceptance checks

- Test duplicate email delivery, disallowed senders, missing/large attachments, and no-reply behavior.
- Test the scheduled tool twice with the same idempotency key.
- Do not claim mail delivery or scheduler registration unless the platform returns evidence.
