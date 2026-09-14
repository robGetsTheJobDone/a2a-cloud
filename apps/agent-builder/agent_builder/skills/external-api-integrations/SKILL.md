---
name: external-api-integrations
description: Build and review agents that call external HTTP APIs, use API keys or OAuth tokens, receive webhooks, or require caller-specific integration setup. Use for GitHub, Slack, CRM, calendar, storage, custom REST/GraphQL APIs, and any workflow that crosses an egress boundary.
---
# External API Integrations

Design the integration contract before writing request code.

## Setup contract

- Declare caller-specific values with `ConsumerSetup` and `ConsumerSetupField`.
- Use `ConsumerSetupField.secret(...)` for tokens and client secrets.
- Use `ConsumerSetupField.config(...)` for base URLs, repository names, tenant IDs, and non-secret options.
- Read values with `ctx.consumer_secret("NAME")` and `ctx.consumer_config("NAME")`.
- Never accept credentials as public tool arguments, write them to files, or return them.
- Declare exact outbound hosts with `EgressPolicy`; do not allow arbitrary user-supplied hosts.

## HTTP behavior

- Use an async client with explicit connect/read/total timeouts.
- Send bounded request bodies and validate response content type and size.
- Distinguish authentication, rate-limit, validation, upstream, and timeout failures in structured results.
- Retry only idempotent reads or writes carrying a provider idempotency key. Bound retries and honor `Retry-After`.
- Redact authorization headers, query tokens, webhook signatures, and provider response secrets from errors.

## OAuth and webhooks

- Treat OAuth setup as incomplete until the caller has supplied the required token/tenant fields. Return a setup-required result instead of fabricating success.
- Verify webhook signatures before parsing or acting on a payload.
- Deduplicate webhook events by provider event ID.
- Keep webhook ingestion separate from consequential actions so policy checks remain explicit.

## Acceptance checks

- Agent Card publishes the consumer setup metadata but no configured values.
- Missing setup returns a clear, non-crashing response.
- Mock success, 401/403, 429, timeout, malformed JSON, and oversized response paths.
- Sandbox tests must not contact production providers or require real credentials.
