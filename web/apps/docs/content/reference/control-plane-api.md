# Control-plane API

The control plane powers the dashboard, CLI, agent lifecycle,
governance, workspaces, and orchestration. This inventory is generated
directly from the FastAPI route declarations in the matching monorepo
revision; it is not a separately maintained endpoint list.

## Canonical schema and clients

- Base URL: `https://api.a2acloud.io`
- [OpenAPI 3 schema](https://api.a2acloud.io/openapi.json)
- [Swagger UI](https://api.a2acloud.io/docs)
- [ReDoc](https://api.a2acloud.io/redoc)
- Python automation should normally use the `a2a` CLI or
  `a2a_pack.cli.api_client.ControlPlaneClient`.

The OpenAPI document is canonical for request and response schemas. This
page is the reviewable route map and names the monorepo file each route is
declared in. The monorepo is not public, so those paths are identifiers,
not links.

## Authentication and safety

Send `Authorization: Bearer <token>` for authenticated API calls. The CLI
obtains and refreshes this token through `a2a login`. Browser-session
mutations also enforce CSRF protection. `/v1/public/*` is intentionally
public; account, authenticated, SCIM, platform, and operator routes require
the corresponding identity or service authority.

Do not call **Platform** or **Operator** routes from ordinary integrations.
They are listed so the platform contract is complete, not because they are
public extension points.

## Route inventory

- 281 route declarations across 45 API groups
- 281 included in OpenAPI; 0 hidden/internal
- Route-contract fingerprint: `760a387ac14891a72c684c4ec3015f5e0a3b5e2ccacbf998f6a5712874105aa3`

### admin

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/admin/auth/keycloak` | Operator | Authenticate keycloak admin | `control-plane/control_plane/routes/admin.py` |
| `GET` | `/v1/admin/feature-flags` | Operator | List feature flags | `control-plane/control_plane/routes/feature_flags.py` |
| `POST` | `/v1/admin/feature-flags` | Operator | Upsert feature flag | `control-plane/control_plane/routes/feature_flags.py` |
| `DELETE` | `/v1/admin/feature-flags/{flag_key}` | Operator | Delete feature flag | `control-plane/control_plane/routes/feature_flags.py` |
| `GET` | `/v1/admin/settings` | Operator | List every known setting, falling back to the platform default when no | `control-plane/control_plane/routes/admin.py` |
| `GET` | `/v1/admin/settings/{key}` | Operator | Read setting | `control-plane/control_plane/routes/admin.py` |
| `PUT` | `/v1/admin/settings/{key}` | Operator | Write setting | `control-plane/control_plane/routes/admin.py` |
| `DELETE` | `/v1/admin/users` | Operator | Purge users and agents | `control-plane/control_plane/routes/admin.py` |
| `GET` | `/v1/admin/users` | Operator | List users | `control-plane/control_plane/routes/admin.py` |
| `GET` | `/v1/admin/users/{user_id}/control-policy` | Operator | Read user control policy | `control-plane/control_plane/routes/admin.py` |
| `PUT` | `/v1/admin/users/{user_id}/control-policy` | Operator | Write user control policy | `control-plane/control_plane/routes/admin.py` |
| `GET` | `/v1/admin/users/{user_id}/feature-flags` | Operator | Read user feature flags | `control-plane/control_plane/routes/feature_flags.py` |
| `PUT` | `/v1/admin/users/{user_id}/feature-flags` | Operator | Write user feature flags | `control-plane/control_plane/routes/feature_flags.py` |
| `POST` | `/v1/admin/users/{user_id}/platform-token` | Operator | Create user platform token | `control-plane/control_plane/routes/admin.py` |
### adversarial-review-loops

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/review-loops` | Authenticated | List review loops | `control-plane/control_plane/routes/adversarial_review_loops.py` |
| `POST` | `/v1/agents/{name}/review-loops` | Authenticated | Create review loop | `control-plane/control_plane/routes/adversarial_review_loops.py` |
| `GET` | `/v1/agents/{name}/review-loops/{job_id}` | Authenticated | Get review loop | `control-plane/control_plane/routes/adversarial_review_loops.py` |
| `POST` | `/v1/agents/{name}/review-loops/{job_id}/events` | Authenticated | Record review loop event | `control-plane/control_plane/routes/adversarial_review_loops.py` |
| `POST` | `/v1/agents/{name}/review-loops/{job_id}/stop` | Authenticated | Stop review loop | `control-plane/control_plane/routes/adversarial_review_loops.py` |
### agent-auth

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/auth` | Authenticated | Get agent auth status | `control-plane/control_plane/routes/agent_auth.py` |
| `POST` | `/v1/agents/{name}/auth` | Authenticated | Connect agent auth | `control-plane/control_plane/routes/agent_auth.py` |
| `DELETE` | `/v1/agents/{name}/auth/{connection_id}` | Authenticated | Delete agent auth connection | `control-plane/control_plane/routes/agent_auth.py` |
### agent-evidence

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/dossier` | Authenticated | Get agent dossier | `control-plane/control_plane/routes/agent_evidence.py` |
| `GET` | `/v1/agents/{name}/evidence-dag` | Authenticated | Get agent evidence dag | `control-plane/control_plane/routes/agent_evidence.py` |
| `GET` | `/v1/agents/{name}/evidence-timeline` | Authenticated | Get agent evidence timeline | `control-plane/control_plane/routes/agent_evidence.py` |
### agent-insights

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/insights/calls` | Authenticated | List agent call logs | `control-plane/control_plane/routes/agent_insights.py` |
### agent-mailboxes

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `DELETE` | `/v1/agents/{name}/mailbox` | Authenticated | Delete mailbox | `control-plane/control_plane/routes/mailboxes.py` |
| `GET` | `/v1/agents/{name}/mailbox` | Authenticated | Get mailbox | `control-plane/control_plane/routes/mailboxes.py` |
| `PATCH` | `/v1/agents/{name}/mailbox` | Authenticated | Update mailbox | `control-plane/control_plane/routes/mailboxes.py` |
| `POST` | `/v1/agents/{name}/mailbox` | Authenticated | Request a mailbox without redeploying (equivalent to declaring | `control-plane/control_plane/routes/mailboxes.py` |
| `GET` | `/v1/agents/{name}/mailbox/events` | Authenticated | List mailbox events | `control-plane/control_plane/routes/mailboxes.py` |
| `GET` | `/v1/platform/mailboxes/health` | Platform | Mailbox health | `control-plane/control_plane/routes/mailboxes.py` |
### agent-memory

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/memory` | Authenticated | List agent memory | `control-plane/control_plane/routes/memory.py` |
| `POST` | `/v1/agents/{name}/memory` | Authenticated | Upsert agent memory | `control-plane/control_plane/routes/memory.py` |
| `PUT` | `/v1/agents/{name}/memory` | Authenticated | Upsert agent memory | `control-plane/control_plane/routes/memory.py` |
| `GET` | `/v1/agents/{name}/memory/search` | Authenticated | Search agent memory | `control-plane/control_plane/routes/memory.py` |
| `DELETE` | `/v1/agents/{name}/memory/{namespace}/{key:path}` | Authenticated | Delete agent memory | `control-plane/control_plane/routes/memory.py` |
| `GET` | `/v1/agents/{name}/memory/{namespace}/{key:path}` | Authenticated | Get agent memory | `control-plane/control_plane/routes/memory.py` |
### agent-proofs

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/agent-proofs` | Account | List my agent proofs | `control-plane/control_plane/routes/agent_proofs.py` |
| `POST` | `/v1/me/agent-proofs/{name}/run` | Account | Run agent proof | `control-plane/control_plane/routes/agent_proofs.py` |
### agent-receipts

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/receipts` | Authenticated | List agent receipts | `control-plane/control_plane/routes/agent_receipts.py` |
| `POST` | `/v1/agents/{name}/receipts` | Authenticated | Post agent receipt | `control-plane/control_plane/routes/agent_receipts.py` |
| `GET` | `/v1/agents/{name}/receipts/{receipt_id}` | Authenticated | Get agent receipt | `control-plane/control_plane/routes/agent_receipts.py` |
### agent-secrets

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/secrets` | Authenticated | List agent secrets | `control-plane/control_plane/routes/agent_secrets.py` |
| `POST` | `/v1/agents/{name}/secrets` | Authenticated | Upsert agent secret | `control-plane/control_plane/routes/agent_secrets.py` |
| `DELETE` | `/v1/agents/{name}/secrets/{key}` | Authenticated | Delete agent secret | `control-plane/control_plane/routes/agent_secrets.py` |
### agent-sessions

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/sessions` | Authenticated | List agent sessions | `control-plane/control_plane/routes/agent_sessions.py` |
| `POST` | `/v1/agents/{name}/sessions` | Authenticated | Post agent session | `control-plane/control_plane/routes/agent_sessions.py` |
| `GET` | `/v1/sessions/{session_id}` | Authenticated | Stream session header + events as ``application/x-ndjson``. | `control-plane/control_plane/routes/agent_sessions.py` |
### agent-studio

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/studio/autopilot` | Authenticated | Get studio autopilot | `control-plane/control_plane/routes/agent_studio.py` |
| `PUT` | `/v1/agents/studio/autopilot` | Authenticated | Update studio autopilot | `control-plane/control_plane/routes/agent_studio.py` |
| `GET` | `/v1/agents/studio/autopilot/proposals` | Authenticated | List studio upgrade proposals | `control-plane/control_plane/routes/agent_studio.py` |
| `GET` | `/v1/agents/studio/autopilot/proposals/{proposal_id}` | Authenticated | Get studio upgrade proposal | `control-plane/control_plane/routes/agent_studio.py` |
| `POST` | `/v1/agents/studio/autopilot/proposals/{proposal_id}/decision` | Authenticated | Decide studio upgrade proposal | `control-plane/control_plane/routes/agent_studio.py` |
| `POST` | `/v1/agents/studio/autopilot/run` | Authenticated | Run studio autopilot now | `control-plane/control_plane/routes/agent_studio.py` |
| `POST` | `/v1/agents/studio/idea-factory` | Authenticated | Score 100 mini-startups and optionally queue the best three builds. | `control-plane/control_plane/routes/agent_studio.py` |
| `POST` | `/v1/agents/studio/resolve` | Authenticated | Resolve reusable agents before any source or deployment is mutated. | `control-plane/control_plane/routes/agent_studio.py` |
| `POST` | `/v1/agents/studio/runs` | Authenticated | Start studio run | `control-plane/control_plane/routes/agent_studio.py` |
| `GET` | `/v1/agents/studio/runs/{run_id}` | Authenticated | Get studio run | `control-plane/control_plane/routes/agent_studio.py` |
| `GET` | `/v1/agents/studio/runs/{run_id}/stream` | Authenticated | Stream studio run | `control-plane/control_plane/routes/agent_studio.py` |
### agents

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents` | Authenticated | List agents | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents` | Authenticated | Register | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/compose` | Authenticated | Generate editable A2APack source from a composition manifest and deploy it. | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/from-openapi` | Authenticated | Generate editable A2APack source from OpenAPI and deploy it. | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/from-source` | Authenticated | Provision an editable source repo for ``body.name``. | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/from-tarball` | Authenticated | Receive user source, then wire a hidden runtime repo for build/deploy. | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/import` | Authenticated | Import external agent | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/mine` | Authenticated | Agents owned by the caller (built by them or deployed via CLI). | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/mine/summary` | Authenticated | List my agent summaries | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/mine/{name}` | Authenticated | Get my agent | `control-plane/control_plane/routes/agents.py` |
| `PATCH` | `/v1/agents/mine/{name}/reuse-policy` | Authenticated | Set source-fork permission independently from public visibility. | `control-plane/control_plane/routes/agents.py` |
| `PATCH` | `/v1/agents/mine/{name}/visibility` | Authenticated | Publish or unlist an owned agent without rebuilding its runtime. | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/openapi/preview` | Authenticated | Preview openapi agent | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/search` | Authenticated | Search agents | `control-plane/control_plane/routes/agents.py` |
| `DELETE` | `/v1/agents/{name}` | Authenticated | Delete AGENT. | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}` | Authenticated | Get agent | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/account-access` | Authenticated | Get agent account access | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/api-tokens` | Authenticated | List agent api tokens | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/api-tokens` | Authenticated | Create agent api token | `control-plane/control_plane/routes/agents.py` |
| `DELETE` | `/v1/agents/{name}/api-tokens/{token_id}` | Authenticated | Revoke agent api token | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/api/files/{path:path}` | Authenticated | Download agent api file | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/api/invoke/{skill_name}` | Authenticated | Invoke agent api | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/api/openapi.json` | Authenticated | Agent api openapi alias | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/api/runs/{run_id}` | Authenticated | Get agent api run | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/api/runs/{skill_name}/start` | Authenticated | Start agent api run | `control-plane/control_plane/routes/agents.py` |
| `DELETE` | `/v1/agents/{name}/code-editor` | Authenticated | Disable agent code editor | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/code-editor` | Authenticated | Get agent code editor | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/code-editor` | Authenticated | Enable agent code editor | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/deployments` | Authenticated | List agent deployments | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/deployments/{deploy_id}` | Authenticated | Get agent deployment | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/deployments/{deploy_id}/logs` | Authenticated | Get agent deployment logs | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/deployments/{deploy_id}/stream` | Authenticated | Stream agent deployment | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/domains` | Authenticated | List agent custom domains | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/domains` | Authenticated | Add agent custom domain | `control-plane/control_plane/routes/agents.py` |
| `DELETE` | `/v1/agents/{name}/domains/{hostname}` | Authenticated | Delete agent custom domain | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/domains/{hostname}/verify` | Authenticated | Verify agent custom domain | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/integration-links` | Authenticated | List agent integration links | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/integration-links` | Authenticated | Create agent integration link | `control-plane/control_plane/routes/agents.py` |
| `DELETE` | `/v1/agents/{name}/integration-links/{link_id}` | Authenticated | Revoke agent integration link | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/mcp` | Authenticated | External agent mcp | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/openapi.json` | Authenticated | Agent api openapi | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/review-runs` | Authenticated | List review runs | `control-plane/control_plane/routes/agent_reviews.py` |
| `GET` | `/v1/agents/{name}/review-runs/latest` | Authenticated | Latest review run | `control-plane/control_plane/routes/agent_reviews.py` |
| `GET` | `/v1/agents/{name}/review-runs/{review_id}` | Authenticated | Get review run | `control-plane/control_plane/routes/agent_reviews.py` |
| `POST` | `/v1/agents/{name}/runtime-upgrade` | Authenticated | Upgrade agent runtime | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/self-healing` | Authenticated | Owner-visible, sanitized repair history for a manifest-opted-in agent. | `control-plane/control_plane/routes/agents.py` |
| `GET` | `/v1/agents/{name}/source` | Authenticated | Export agent source | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/source/deploy` | Authenticated | Deploy agent source | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/ssh` | Authenticated | Provision (or wake) the agent's throwaway dev box and return the WSS | `control-plane/control_plane/routes/agents.py` |
| `POST` | `/v1/agents/{name}/template-update` | Authenticated | Request agent template update | `control-plane/control_plane/routes/agents.py` |
### auth

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/auth/agent-session/authorize` | Authenticated | Hand a signed-in dashboard visitor off to an agent's own origin. | `control-plane/control_plane/routes/auth.py` |
| `POST` | `/v1/auth/agent-session/exchange` | Authenticated | Redeem a hand-off code for a token scoped to the calling agent. | `control-plane/control_plane/routes/auth.py` |
| `POST` | `/v1/auth/cli-session` | Authenticated | Mint a short-lived browser exchange code from a bearer credential. | `control-plane/control_plane/routes/auth.py` |
| `POST` | `/v1/auth/cli-session/confirm` | Authenticated | Confirm cli session | `control-plane/control_plane/routes/auth.py` |
| `GET` | `/v1/auth/cli-session/redeem` | Authenticated | Consume a one-time code and establish the browser session cookie. | `control-plane/control_plane/routes/auth.py` |
| `POST` | `/v1/auth/logout` | Authenticated | Logout | `control-plane/control_plane/routes/auth.py` |
| `GET` | `/v1/auth/oidc/callback` | Authenticated | Oidc callback | `control-plane/control_plane/routes/auth.py` |
| `GET` | `/v1/auth/oidc/start` | Authenticated | Oidc start | `control-plane/control_plane/routes/auth.py` |
| `GET` | `/v1/auth/session` | Authenticated | Auth session | `control-plane/control_plane/routes/auth.py` |
| `GET` | `/v1/me` | Account | Me | `control-plane/control_plane/routes/auth.py` |
### bounties

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/bounties` | Authenticated | List bounties the current user can see (all open, plus their own). | `control-plane/control_plane/routes/bounties.py` |
| `POST` | `/v1/bounties` | Authenticated | Post a new bounty. Slug is generated from the title. | `control-plane/control_plane/routes/bounties.py` |
| `DELETE` | `/v1/bounties/{slug}` | Authenticated | Poster cancels the bounty. Terminal. | `control-plane/control_plane/routes/bounties.py` |
| `GET` | `/v1/bounties/{slug}` | Authenticated | Public-readable bounty detail (no auth required for v1). | `control-plane/control_plane/routes/bounties.py` |
| `POST` | `/v1/bounties/{slug}/claim` | Authenticated | Link a user-owned, deployed, public agent to an open bounty. | `control-plane/control_plane/routes/bounties.py` |
| `POST` | `/v1/bounties/{slug}/fulfill` | Authenticated | Poster marks the bounty as fulfilled. Settlement happens out of band. | `control-plane/control_plane/routes/bounties.py` |
### chat

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/me/chat` | Account | Chat | `control-plane/control_plane/routes/chat.py` |
| `POST` | `/v1/me/chat/approvals/{approval_id}` | Account | User decision on a pending cross-agent handoff. | `control-plane/control_plane/routes/chat.py` |
| `POST` | `/v1/me/chat/input-requests/{request_id}` | Account | User response to a callee's ``ctx.collect()`` form request. | `control-plane/control_plane/routes/chat.py` |
| `POST` | `/v1/me/chat/questions/{question_id}` | Account | User reply to a callee's ``ctx.ask()`` prompt. | `control-plane/control_plane/routes/chat.py` |
| `POST` | `/v1/me/chat/scope-approvals/{approval_id}` | Account | User decision on a pending mid-skill scope-expansion request. | `control-plane/control_plane/routes/chat.py` |
### chat-threads

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/threads` | Account | List threads | `control-plane/control_plane/routes/threads.py` |
| `POST` | `/v1/me/threads` | Account | Create thread | `control-plane/control_plane/routes/threads.py` |
| `DELETE` | `/v1/me/threads/{thread_id}` | Account | Delete thread | `control-plane/control_plane/routes/threads.py` |
| `PATCH` | `/v1/me/threads/{thread_id}` | Account | Rename thread | `control-plane/control_plane/routes/threads.py` |
| `GET` | `/v1/me/threads/{thread_id}/activity` | Account | Get thread activity | `control-plane/control_plane/routes/threads.py` |
| `GET` | `/v1/me/threads/{thread_id}/messages` | Account | Get thread messages | `control-plane/control_plane/routes/threads.py` |
### collective-runtime

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/collective-runtime` | Account | Collective runtime overview | `control-plane/control_plane/routes/collective_runtime.py` |
| `GET` | `/v1/me/collective-runtime/memories` | Account | List collective runtime memories | `control-plane/control_plane/routes/collective_runtime.py` |
| `POST` | `/v1/me/collective-runtime/memories/extract` | Account | Extract collective runtime memories | `control-plane/control_plane/routes/collective_runtime.py` |
| `POST` | `/v1/me/collective-runtime/plan` | Account | Plan collective runtime topology | `control-plane/control_plane/routes/collective_runtime.py` |
| `GET` | `/v1/me/collective-runtime/protocols` | Account | List collective runtime protocols | `control-plane/control_plane/routes/collective_runtime.py` |
| `GET` | `/v1/me/collective-runtime/registry` | Account | Collective runtime registry | `control-plane/control_plane/routes/collective_runtime.py` |
| `GET` | `/v1/me/collective-runtime/runs` | Account | List collective runtime runs | `control-plane/control_plane/routes/collective_runtime.py` |
| `POST` | `/v1/me/collective-runtime/runs` | Account | Create collective runtime run | `control-plane/control_plane/routes/collective_runtime.py` |
| `GET` | `/v1/me/collective-runtime/runs/{dag_run_id}` | Account | Get collective runtime run | `control-plane/control_plane/routes/collective_runtime.py` |
| `POST` | `/v1/me/collective-runtime/runs/{dag_run_id}/advance` | Account | Advance collective runtime run | `control-plane/control_plane/routes/collective_runtime.py` |
| `POST` | `/v1/me/collective-runtime/runs/{dag_run_id}/approve` | Account | Approve collective runtime run | `control-plane/control_plane/routes/collective_runtime.py` |
| `GET` | `/v1/me/collective-runtime/scorecards` | Account | Collective runtime scorecards | `control-plane/control_plane/routes/collective_runtime.py` |
### compliance

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/organizations/{slug}/compliance/agents` | Account | List agent profiles | `control-plane/control_plane/routes/compliance.py` |
| `PUT` | `/v1/me/organizations/{slug}/compliance/agents/{agent_name}` | Account | Put agent profile | `control-plane/control_plane/routes/compliance.py` |
| `GET` | `/v1/me/organizations/{slug}/compliance/decision-records` | Account | List decision records | `control-plane/control_plane/routes/compliance.py` |
| `GET` | `/v1/me/organizations/{slug}/compliance/decision-records/{kind}/{record_id}` | Account | Get decision record | `control-plane/control_plane/routes/compliance.py` |
| `POST` | `/v1/me/organizations/{slug}/compliance/export` | Account | Export evidence pack | `control-plane/control_plane/routes/compliance.py` |
| `GET` | `/v1/me/organizations/{slug}/compliance/policy` | Account | Get policy | `control-plane/control_plane/routes/compliance.py` |
| `PUT` | `/v1/me/organizations/{slug}/compliance/policy` | Account | Put policy | `control-plane/control_plane/routes/compliance.py` |
| `POST` | `/v1/me/organizations/{slug}/compliance/retention/enforce` | Account | Enforce retention | `control-plane/control_plane/routes/compliance.py` |
| `GET` | `/v1/me/organizations/{slug}/compliance/status` | Account | Get status | `control-plane/control_plane/routes/compliance.py` |
### consumer-setup

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/consumer-setup` | Authenticated | Get consumer setup | `control-plane/control_plane/routes/consumer_setup.py` |
| `PUT` | `/v1/agents/{name}/consumer-setup` | Authenticated | Upsert user consumer setup | `control-plane/control_plane/routes/consumer_setup.py` |
| `POST` | `/v1/agents/{name}/consumer-setup/invocation` | Authenticated | Get consumer setup invocation | `control-plane/control_plane/routes/consumer_setup.py` |
| `PUT` | `/v1/agents/{name}/consumer-setup/org` | Authenticated | Upsert org consumer setup | `control-plane/control_plane/routes/consumer_setup.py` |
| `DELETE` | `/v1/agents/{name}/consumer-setup/{field_name}` | Authenticated | Delete consumer setup | `control-plane/control_plane/routes/consumer_setup.py` |
### control-room

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/control-room` | Account | Get control room | `control-plane/control_plane/routes/control_room.py` |
| `PATCH` | `/v1/me/control-room/policy` | Account | Update control policy | `control-plane/control_plane/routes/control_room.py` |
| `GET` | `/v1/me/control-room/receipts/{source}/{item_id}` | Account | Get receipt | `control-plane/control_plane/routes/control_room.py` |
### dag-runs

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/dag-runs` | Account | List dag runs | `control-plane/control_plane/routes/dag_runs.py` |
| `GET` | `/v1/me/dag-runs/{dag_run_id}` | Account | Get dag run | `control-plane/control_plane/routes/dag_runs.py` |
### databases

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/databases` | Account | List database projects | `control-plane/control_plane/routes/databases.py` |
| `POST` | `/v1/me/databases` | Account | Create database project | `control-plane/control_plane/routes/databases.py` |
| `GET` | `/v1/me/databases/provisioner-status` | Account | Get database provisioner status | `control-plane/control_plane/routes/databases.py` |
| `GET` | `/v1/me/databases/{project_id}` | Account | Get database project | `control-plane/control_plane/routes/databases.py` |
### feature-flags

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/feature-flags` | Account | Current feature flags | `control-plane/control_plane/routes/feature_flags.py` |
### files

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/files` | Account | List my files | `control-plane/control_plane/routes/files.py` |
| `POST` | `/v1/me/files` | Account | Upload | `control-plane/control_plane/routes/files.py` |
| `POST` | `/v1/me/files/move` | Account | Move (rename) a single object inside the user's bucket. | `control-plane/control_plane/routes/files.py` |
| `DELETE` | `/v1/me/files/{path:path}` | Account | Remove | `control-plane/control_plane/routes/files.py` |
| `GET` | `/v1/me/files/{path:path}` | Account | Download | `control-plane/control_plane/routes/files.py` |
### gitea-webhooks

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/platform/gitea/webhooks/runtime-push` | Platform | Runtime push webhook | `control-plane/control_plane/routes/gitea_webhooks.py` |
| `POST` | `/v1/platform/gitea/webhooks/source-push` | Platform | Source push webhook | `control-plane/control_plane/routes/gitea_webhooks.py` |
### grants

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/grants` | Account | Return the most recent ``limit`` grant rows for the current user. | `control-plane/control_plane/routes/grants.py` |
| `GET` | `/v1/me/grants/{grant_id}` | Account | Return the audit chain for a given grant: the row itself plus every | `control-plane/control_plane/routes/grants.py` |
### installed-agents

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/installed-agents` | Authenticated | List installed agents | `control-plane/control_plane/routes/consumer_setup.py` |
| `POST` | `/v1/installed-agents/{name}` | Authenticated | Install marketplace agent | `control-plane/control_plane/routes/consumer_setup.py` |
### kernel-evolution

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/kernel-evolution/runs` | Account | List user kernel evolution runs | `control-plane/control_plane/routes/user_kernel_evolution.py` |
| `POST` | `/v1/me/kernel-evolution/runs` | Account | Run user kernel evolution | `control-plane/control_plane/routes/user_kernel_evolution.py` |
| `GET` | `/v1/me/kernel-evolution/runs/{job_id}` | Account | Get user kernel evolution run | `control-plane/control_plane/routes/user_kernel_evolution.py` |
| `POST` | `/v1/me/kernel-evolution/runs/{job_id}/replay` | Account | Replay user kernel evolution run | `control-plane/control_plane/routes/user_kernel_evolution.py` |
### kernel-simulations

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/kernel-simulations/runs` | Account | List user kernel simulation runs | `control-plane/control_plane/routes/user_kernel_simulations.py` |
| `POST` | `/v1/me/kernel-simulations/runs` | Account | Run user kernel simulation | `control-plane/control_plane/routes/user_kernel_simulations.py` |
| `GET` | `/v1/me/kernel-simulations/runs/{job_id}` | Account | Get user kernel simulation run | `control-plane/control_plane/routes/user_kernel_simulations.py` |
| `POST` | `/v1/me/kernel-simulations/runs/{job_id}/replay` | Account | Replay user kernel simulation run | `control-plane/control_plane/routes/user_kernel_simulations.py` |
| `GET` | `/v1/me/kernel-simulations/templates` | Account | List user kernel simulation templates | `control-plane/control_plane/routes/user_kernel_simulations.py` |
### llm-creds

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/llm-creds` | Account | List creds | `control-plane/control_plane/routes/llm_creds.py` |
| `POST` | `/v1/me/llm-creds` | Account | Create OR replace by ``name``. Most users only have one entry | `control-plane/control_plane/routes/llm_creds.py` |
| `GET` | `/v1/me/llm-creds/catalog` | Account | Llm model catalog | `control-plane/control_plane/routes/llm_creds.py` |
| `DELETE` | `/v1/me/llm-creds/{name}` | Account | Delete creds | `control-plane/control_plane/routes/llm_creds.py` |
### llm-usage

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/llm-usage` | Account | List llm usage | `control-plane/control_plane/routes/llm_usage.py` |
### meta-agent-runs

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/meta-runs` | Authenticated | List meta runs | `control-plane/control_plane/routes/meta_runs.py` |
| `POST` | `/v1/agents/{name}/meta-runs` | Authenticated | Create meta run | `control-plane/control_plane/routes/meta_runs.py` |
| `GET` | `/v1/agents/{name}/meta-runs/{run_id}` | Authenticated | Get meta run | `control-plane/control_plane/routes/meta_runs.py` |
| `PATCH` | `/v1/agents/{name}/meta-runs/{run_id}` | Authenticated | Patch meta run | `control-plane/control_plane/routes/meta_runs.py` |
### onboarding

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/onboarding` | Account | Get onboarding state | `control-plane/control_plane/routes/onboarding.py` |
| `PATCH` | `/v1/me/onboarding` | Account | Update onboarding state | `control-plane/control_plane/routes/onboarding.py` |
### organizations

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/organizations` | Account | List organizations | `control-plane/control_plane/routes/organizations.py` |
| `POST` | `/v1/me/organizations` | Account | Create organization | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/me/organizations/{slug}/audit-log` | Account | List organization audit log | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/me/organizations/{slug}/domains` | Account | List organization domains | `control-plane/control_plane/routes/organizations.py` |
| `POST` | `/v1/me/organizations/{slug}/domains` | Account | Add organization domain | `control-plane/control_plane/routes/organizations.py` |
| `DELETE` | `/v1/me/organizations/{slug}/domains/{domain}` | Account | Delete organization domain | `control-plane/control_plane/routes/organizations.py` |
| `POST` | `/v1/me/organizations/{slug}/domains/{domain}/verify` | Account | Verify organization domain | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/me/organizations/{slug}/members` | Account | List organization members | `control-plane/control_plane/routes/organizations.py` |
| `POST` | `/v1/me/organizations/{slug}/members` | Account | Add organization member | `control-plane/control_plane/routes/organizations.py` |
| `DELETE` | `/v1/me/organizations/{slug}/members/{member_id}` | Account | Delete organization member | `control-plane/control_plane/routes/organizations.py` |
| `PATCH` | `/v1/me/organizations/{slug}/members/{member_id}` | Account | Update organization member | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/me/organizations/{slug}/scim` | Account | Get scim config | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/me/organizations/{slug}/scim/tokens` | Account | List scim tokens | `control-plane/control_plane/routes/organizations.py` |
| `POST` | `/v1/me/organizations/{slug}/scim/tokens` | Account | Create scim token | `control-plane/control_plane/routes/organizations.py` |
| `DELETE` | `/v1/me/organizations/{slug}/scim/tokens/{token_id}` | Account | Revoke scim token | `control-plane/control_plane/routes/organizations.py` |
### platform

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/platform/agent-session/verify` | Platform | Resolve the caller behind an agent-origin session cookie. | `control-plane/control_plane/routes/platform.py` |
| `POST` | `/v1/platform/gitea-token` | Platform | Mint a short-lived Gitea token scoped to a single ``(owner, repo)``. | `control-plane/control_plane/routes/platform.py` |
| `DELETE` | `/v1/platform/gitea-token/{token_name}` | Platform | Revoke a previously minted Gitea token. Idempotent. | `control-plane/control_plane/routes/platform.py` |
| `POST` | `/v1/platform/llm-grant` | Platform | Return caller-funded LLM credentials for platform-declared agents. | `control-plane/control_plane/routes/platform.py` |
### protocol-simulations

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/agents/{name}/protocol-simulations` | Authenticated | List protocol simulations | `control-plane/control_plane/routes/protocol_simulations.py` |
| `POST` | `/v1/agents/{name}/protocol-simulations` | Authenticated | Create protocol simulation | `control-plane/control_plane/routes/protocol_simulations.py` |
| `POST` | `/v1/agents/{name}/protocol-simulations/custom-runs` | Authenticated | Run custom protocol simulation | `control-plane/control_plane/routes/protocol_simulations.py` |
| `GET` | `/v1/agents/{name}/protocol-simulations/custom-suite-templates` | Authenticated | List custom protocol simulation suite templates | `control-plane/control_plane/routes/protocol_simulations.py` |
| `POST` | `/v1/agents/{name}/protocol-simulations/custom-suites` | Authenticated | Run custom protocol simulation suite | `control-plane/control_plane/routes/protocol_simulations.py` |
| `GET` | `/v1/agents/{name}/protocol-simulations/custom-templates` | Authenticated | List custom protocol simulation templates | `control-plane/control_plane/routes/protocol_simulations.py` |
| `GET` | `/v1/agents/{name}/protocol-simulations/protocol-registry` | Authenticated | List protocol pack registry | `control-plane/control_plane/routes/protocol_simulations.py` |
| `POST` | `/v1/agents/{name}/protocol-simulations/runtime-readiness` | Authenticated | Check protocol runtime readiness | `control-plane/control_plane/routes/protocol_simulations.py` |
| `GET` | `/v1/agents/{name}/protocol-simulations/scenarios` | Authenticated | List protocol simulation scenarios | `control-plane/control_plane/routes/protocol_simulations.py` |
| `GET` | `/v1/agents/{name}/protocol-simulations/{job_id}` | Authenticated | Get protocol simulation | `control-plane/control_plane/routes/protocol_simulations.py` |
| `POST` | `/v1/agents/{name}/protocol-simulations/{job_id}/events` | Authenticated | Record protocol simulation event | `control-plane/control_plane/routes/protocol_simulations.py` |
| `POST` | `/v1/agents/{name}/protocol-simulations/{job_id}/scenario-runs` | Authenticated | Record protocol simulation scenario run | `control-plane/control_plane/routes/protocol_simulations.py` |
| `POST` | `/v1/agents/{name}/protocol-simulations/{job_id}/stop` | Authenticated | Stop protocol simulation | `control-plane/control_plane/routes/protocol_simulations.py` |
### public

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/public/agent-proofs` | Public | List public agent proofs | `control-plane/control_plane/routes/agent_proofs.py` |
| `GET` | `/v1/public/agents` | Public | Public registry feed, best listings first. | `control-plane/control_plane/routes/public.py` |
| `GET` | `/v1/public/agents/{name}` | Public | Get public agent | `control-plane/control_plane/routes/public.py` |
| `GET` | `/v1/public/agents/{name}/proof/latest` | Public | Get public agent proof | `control-plane/control_plane/routes/agent_proofs.py` |
| `GET` | `/v1/public/agents/{name}/proofs/{proof_id}` | Public | Get public agent proof run | `control-plane/control_plane/routes/agent_proofs.py` |
| `GET` | `/v1/public/bounties` | Public | Open + claimed bounties anyone can see (fulfilled/cancelled hidden). | `control-plane/control_plane/routes/public.py` |
| `GET` | `/v1/public/bounties/{slug}` | Public | Get public bounty | `control-plane/control_plane/routes/public.py` |
| `GET` | `/v1/public/receipt-keys` | Public | Publish the receipt verifying key so third parties can check receipts. | `control-plane/control_plane/routes/public.py` |
### schedules

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/schedules` | Account | List schedules | `control-plane/control_plane/routes/schedules.py` |
| `POST` | `/v1/me/schedules` | Account | Create schedule | `control-plane/control_plane/routes/schedules.py` |
| `DELETE` | `/v1/me/schedules/{schedule_id}` | Account | Delete schedule | `control-plane/control_plane/routes/schedules.py` |
| `PATCH` | `/v1/me/schedules/{schedule_id}` | Account | Update schedule | `control-plane/control_plane/routes/schedules.py` |
| `POST` | `/v1/me/schedules/{schedule_id}/run` | Account | Run schedule now | `control-plane/control_plane/routes/schedules.py` |
### scim

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/scim/{slug}/v2/Groups` | SCIM | Scim groups | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/scim/{slug}/v2/ResourceTypes` | SCIM | Scim resource types | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/scim/{slug}/v2/Schemas` | SCIM | Scim schemas | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/scim/{slug}/v2/ServiceProviderConfig` | SCIM | Scim service provider config | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/scim/{slug}/v2/Users` | SCIM | List scim users | `control-plane/control_plane/routes/organizations.py` |
| `POST` | `/v1/scim/{slug}/v2/Users` | SCIM | Create scim user | `control-plane/control_plane/routes/organizations.py` |
| `DELETE` | `/v1/scim/{slug}/v2/Users/{user_id}` | SCIM | Delete scim user | `control-plane/control_plane/routes/organizations.py` |
| `GET` | `/v1/scim/{slug}/v2/Users/{user_id}` | SCIM | Get scim user | `control-plane/control_plane/routes/organizations.py` |
| `PATCH` | `/v1/scim/{slug}/v2/Users/{user_id}` | SCIM | Patch scim user | `control-plane/control_plane/routes/organizations.py` |
| `PUT` | `/v1/scim/{slug}/v2/Users/{user_id}` | SCIM | Replace scim user | `control-plane/control_plane/routes/organizations.py` |
### service-access

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/service-access` | Account | List service access | `control-plane/control_plane/routes/service_access.py` |
### subagent-runs

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/subagent-runs` | Account | List subagent runs | `control-plane/control_plane/routes/subagent_runs.py` |
| `POST` | `/v1/me/subagent-runs/track` | Account | Track subagent event route | `control-plane/control_plane/routes/subagent_runs.py` |
| `GET` | `/v1/me/subagent-runs/{grant_id}` | Account | Get subagent run | `control-plane/control_plane/routes/subagent_runs.py` |
| `POST` | `/v1/me/subagent-runs/{grant_id}/rerun` | Account | Rerun subagent run | `control-plane/control_plane/routes/subagent_runs.py` |
### trial-rooms

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/trial-rooms` | Account | List trial rooms | `control-plane/control_plane/routes/trial_rooms.py` |
| `POST` | `/v1/me/trial-rooms` | Account | Create trial room | `control-plane/control_plane/routes/trial_rooms.py` |
| `GET` | `/v1/me/trial-rooms/{slug}` | Account | Get trial room | `control-plane/control_plane/routes/trial_rooms.py` |
| `POST` | `/v1/me/trial-rooms/{slug}/runs` | Account | Run trial agent | `control-plane/control_plane/routes/trial_rooms.py` |
| `POST` | `/v1/me/trial-rooms/{slug}/runs/{run_id}/rate` | Account | Rate trial run | `control-plane/control_plane/routes/trial_rooms.py` |
| `POST` | `/v1/me/trial-rooms/{slug}/runs/{run_id}/select` | Account | Select trial winner | `control-plane/control_plane/routes/trial_rooms.py` |
### work-ledger

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `GET` | `/v1/me/activity` | Account | List my activity | `control-plane/control_plane/routes/work_ledger.py` |
| `GET` | `/v1/me/jobs/{job_id}` | Account | Get my job | `control-plane/control_plane/routes/work_ledger.py` |
| `POST` | `/v1/me/jobs/{job_id}/cancel` | Account | Cancel my job | `control-plane/control_plane/routes/work_ledger.py` |
| `GET` | `/v1/me/jobs/{job_id}/events` | Account | List my job events | `control-plane/control_plane/routes/work_ledger.py` |
| `GET` | `/v1/me/jobs/{job_id}/events/stream` | Account | Stream my job events | `control-plane/control_plane/routes/work_ledger.py` |
### workspace-grants

| Method | Path | Scope | Purpose | Source |
| --- | --- | --- | --- | --- |
| `POST` | `/v1/workspace-grants/delegate` | Authenticated | Mint a mechanically bounded child without exposing the platform key. | `control-plane/control_plane/routes/workspace_grants.py` |
| `GET` | `/v1/workspace-grants/files` | Authenticated | List grant files | `control-plane/control_plane/routes/workspace_grants.py` |
| `DELETE` | `/v1/workspace-grants/files/{path:path}` | Authenticated | Delete grant file | `control-plane/control_plane/routes/workspace_grants.py` |
| `GET` | `/v1/workspace-grants/files/{path:path}` | Authenticated | Read grant file | `control-plane/control_plane/routes/workspace_grants.py` |
| `HEAD` | `/v1/workspace-grants/files/{path:path}` | Authenticated | Stat grant file | `control-plane/control_plane/routes/workspace_grants.py` |
| `PUT` | `/v1/workspace-grants/files/{path:path}` | Authenticated | Write grant file | `control-plane/control_plane/routes/workspace_grants.py` |

## Error shape

Validation and HTTP failures use an HTTP status plus a JSON `detail`
field. Platform middleware also emits a structured `error` object when
available. Treat status codes as the stable branch condition and render
the server message for operators rather than parsing human text.
