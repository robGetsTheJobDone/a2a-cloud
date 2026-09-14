# Administer an account

Admin surfaces manage service access, plan and usage, LLM credentials,
organization identity, and compliance evidence. Organization owners and admins
can see more controls than ordinary members.

## Access

[Access](https://app.a2acloud.io/access) inventories managed service state:

- **Langfuse** — observability workspace and login state
- **LiteLLM** — model-routing access and connection state
- **Gitea organizations** — source workspace identity and links
- **Repositories** — agent repository access and managed credential references

Copy values only into the client that needs them. Do not put managed service
credentials in an agent card, source repository, prompt, or browser-visible
frontend configuration.

## LLM keys

[LLM Keys](https://app.a2acloud.io/llm-keys) lets an account:

1. add an OpenAI-compatible provider key and base URL
2. choose a model from the provider-compatible catalog
3. select the default credential
4. replace or remove saved credentials
5. inspect model health

Agents using `ctx.llm` receive a short-lived platform route, not the provider
secret in public metadata. See [LLM credentials](/concepts/llm-credentials).

## Organizations

[Organization](https://app.a2acloud.io/organization) covers:

- verified domains
- SCIM configuration and bearer tokens
- members and owner/admin/member roles
- activation state
- audit history

SCIM tokens are shown only when created. Store them in the identity provider,
rotate them deliberately, and use the organization audit log to verify member
changes.

## Compliance

[Compliance](https://app.a2acloud.io/compliance) provides:

- **Decision Records** — tamper-evident governance decisions
- **Policy** — retention and organization compliance settings
- **Agent Risk** — classification and review state per agent
- **Evidence export** — organization-scoped evidence packages

The dashboard helps assemble EU AI Act readiness evidence; it does not replace
legal classification or an organization's accountable human decision.

## Recommended operating cadence

- Daily: review failed work, spend posture, approval queues, and setup health.
- Before a release: inspect deployment, card, proof, evidence, and policy.
- Monthly: reconcile usage, members, and audit events.
- On personnel or provider change: rotate SCIM, service, imported-agent, and
  LLM credentials.

See the [Control-plane API guide](/platform/api) for automation and the
[complete API inventory](/reference/control-plane-api) for every route.
