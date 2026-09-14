# Dynamic Capability Graph Threat Model

Status: exploratory schema, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/capability-algebra.md`
- `docs/signals-evidence.md`
- `docs/rewrite-engine.md`
- `docs/policy-kernel.md`
- `docs/process-scheduler.md`
- `docs/subgraph-templates.md`
- `docs/self-modification.md`
- `docs/deletion-revocation.md`
- `docs/ledger-lineage-economics.md`
- `docs/routing-dynamics.md`
- `docs/a2a-pack-mapping.md`
- `docs/protocol-packs.md`

## Decision

The dynamic graph kernel is a high-risk control surface. It will eventually
touch self-modification, deletion, credential revocation, route selection,
budget, reputation, lineage, recursive agent networks, memory, source code, and
marketplace/status signals. Any implementation must be threat-modeled before it
can actively mutate graph state.

The minimum safety posture:

- no node mutates graph state directly
- all dangerous rewrites are staged
- every capability has provenance, scope, TTL, and revocation path
- child capability is no broader than parent capability
- deny, freeze, and revoke override allow
- budgets, TTL, depth, and child limits are mandatory
- kill-switch is available for every active process
- dangerous tiers require human/owner/platform approval
- all stored and user-visible artifacts are redacted by audience
- mutation of live traffic requires review/proof/canary and rollback

For v0, keep graph behavior as projections, explanations, docs, and simulations.
Do not enable autonomous mutation, revocation, routing adaptation, lineage
inheritance, or marketplace status effects until the mitigations and tests in
this document exist.

## Assets

Assets requiring protection:

| Asset | Why it matters |
|---|---|
| user/org data | private files, prompts, memory, traces, inputs, outputs |
| credentials | runtime secrets, OAuth connections, Gitea tokens, LLM keys, CP JWTs |
| signed grants | runtime capability tokens and parent chains |
| source repos | deployed agent source, generated code, manifests, prompts |
| deployments | image refs, live URLs, Argo/Kubernetes state |
| graph state | nodes, ports, edges, route weights, processes, policy refs |
| budget/money | LLM spend, bounties, future revenue share/settlement |
| reputation/status | trust, certification, rank, taste, marketplace visibility |
| deletion state | revocation, purge, tombstone, lineage severance |
| evidence ledger | receipts, sessions, work events, reviews, proofs, deployments |
| search/discovery | public/private visibility and agent match results |

## Runtime Signing Trust

The production runtime trust path uses Ed25519 key pairs with role-specific
placement. Grants, receipts, and replay sessions do not use a shared platform
secret or HMAC fallback.

- grant issuers use `A2A_GRANT_SIGNING_KEY`; grant verifiers use
  `A2A_GRANT_VERIFYING_KEY`
- receipt sealers use `A2A_RECEIPT_SIGNING_KEY`; receipt verifiers use
  `A2A_RECEIPT_VERIFYING_KEY`
- replay sealers use `A2A_REPLAY_SIGNING_KEY`; replay verifiers use
  `A2A_REPLAY_VERIFYING_KEY`
- sandbox and LiteLLM receive grant verifier material only
- agent-authored secrets never carry platform signing keys

Operational handling, rollback, key rotation, stale Agent Card checks, and the
CI regression guard are documented in
`docs/ed25519-runtime-trust-runbook.md`.

## Principals

Principals:

- owner/user
- org admin
- platform operator
- deployed A2A agent
- meta-agent/synthetic agent
- evaluator/reviewer/proof runner
- mutator/code-editor/agent-builder
- marketplace/router/scheduler
- external connector/client
- attacker-controlled agent or dependency
- compromised credential holder

Trust rule: no principal gets ambient graph authority. Every action is scoped by
capability, policy, process, and evidence.

## Trust Boundaries

Important boundaries:

- browser/dashboard to control plane
- connector MCP client to control plane
- control plane to deployed agent runtime
- deployed agent to another deployed agent through `ctx.call`
- SDK runtime to workspace/files/secrets/LLM credentials
- control plane to Gitea, Argo, Kubernetes, MinIO, Qdrant, LiteLLM
- public search/listing to private agent/evidence state
- owner-visible evidence to public/marketplace summaries
- signed grant token to server-side audit/revocation state
- code-editor/source mutator to source repo and deployment pipeline
- protocol pack UI language to kernel audit facts

Boundary rule: crossing a boundary requires explicit scope, redaction, and
ledger provenance.

## Threats And Mitigations

### Scope Union Privilege Escalation

Threat: multiple narrow grants or policies are combined into a broader effective
scope without higher authority.

Mitigations:

- intersection is the default composition operator
- union is a high-risk rewrite requiring policy approval
- union must preserve source authority path for every side
- tests prove child scope cannot exceed parent scope

### Hidden Ambient Access On Nodes

Threat: a node is treated as "trusted" and can access memory, source, budget,
credentials, or children without explicit edge capability.

Mitigations:

- all access rides scoped edges/capabilities
- node reputation/status never grants access
- Agent Card declarations are not proof of authority
- route decision must prove capability before scoring

### Child Edge Exceeds Parent Authority

Threat: delegation or process-local capability mints broader child authority.

Mitigations:

- child <= parent enforced across resource, action, TTL, budget, write prefix,
  model set, depth, and delegation
- denies and revocation path are inherited
- CompositionBudget and grant delegation limits are mandatory
- property tests cover attenuation

### Grant Or Token Leakage

Threat: signed grants, CP JWTs, Gitea tokens, LLM keys, OAuth tokens, or runtime
secrets leak through ledger rows, reports, prompts, traces, memory, receipts, or
public views.

Mitigations:

- never copy raw tokens into graph events or public evidence
- store refs, hashes, prefixes, or last4 only where safe
- redaction classes on every evidence projection
- redaction tests scan dossier, route explanations, tombstones, and protocol UI
- prompt builders exclude raw secrets and signed tokens

### Unbounded Recursive Activation

Threat: agents recursively activate sub-agents, child processes, replans, or
protocol packs until budget, cost, or availability is exhausted.

Mitigations:

- max depth, calls, child processes, replans, runtime, TTL, and LLM budget
- cycle detection in composition stack and graph process activation
- scheduler kills or pauses budget-exhausted processes
- simulations cover runaway recursion

### Mutation Loop Runaway

Threat: self-improvement or code-editor loops repeatedly mutate source,
manifest, prompt, memory, routes, or policy without stable exit.

Mitigations:

- mutation is staged rewrite, not direct node action
- high/dangerous rewrites require approval, review/proof, rollback
- max rewrite count per process
- canary and degradation rollback for live traffic
- kill-switch available to owner/platform

### Malicious Evaluator Emits False Signals

Threat: evaluator/reviewer lies, overstates confidence, suppresses findings, or
emits signals outside its allowed scope.

Mitigations:

- evaluator authority is scoped to declared signal types
- evaluator cannot mutate subject unless separately authorized
- reviewer/proof evidence is versioned and ledgered
- conflicting evaluators can be required for dangerous promotions
- evaluator reputation and certification can be revoked

### Memory Or Reputation Poisoning

Threat: attacker injects false memory, fake feedback, inflated reputation,
co-use, rank, or taste signals to manipulate routing or trust.

Mitigations:

- memory writes require scoped memory authority
- feedback/taste signals are explicit and audience-bound
- reputation is projection over evidence, not source of truth
- marketplace/status/taste never bypass policy
- suspicious signal bursts trigger review or dampening

### Marketplace And Status Manipulation

Threat: money, status, rank, social standing, popularity, conversion, or taste
signals push unsafe or unauthorized nodes into routes.

Mitigations:

- policy and capability filters run before scoring
- hard safety blocks cap route weight
- route explanations separate filters from scores
- rank/taste/revenue are ledgered as signals only
- protocol packs cannot define authority through UI language

### Unsafe Deletion Or Revocation Gaps

Threat: deletion removes evidence needed for audit, revocation fails to stop
active work, or credential material remains live after DB cleanup.

Mitigations:

- freeze target before destructive cleanup
- revoke process-local and child capabilities
- remove live credential material before deleting DB rows
- tombstone identity and preserve redacted audit refs
- deletion preview checks dependencies, active processes, and retention

### Dependency Cycles

Threat: cyclic dependencies create infinite activation, unclear authority,
impossible deletion, or reputation loops.

Mitigations:

- DAG projection for evidence even if conceptual graph has cycles
- cycle checks for process activation and composition stack
- dependency cycles require explicit policy and simulation
- deletion preview detects cycles and blocks unsafe cleanup

### Fork Inherits Undeserved Trust

Threat: forked/descendant agent inherits reputation, certifications, marketplace
rank, or safety status that no longer applies.

Mitigations:

- trust is scoped by version, skill, card hash, dependency set
- forks inherit source lineage only by default
- certification inheritance requires explicit policy
- lineage severance blocks future inheritance
- unresolved critical findings inherit forward unless resolved or severed

### Reviewer Becomes Attack-Generation Path

Threat: reviewer, red-team, or adversarial evaluator generates exploit payloads
that leak into prompts, memory, public reports, or agent outputs.

Mitigations:

- adversarial evaluator has bounded test surface
- security findings have security redaction class
- generated attack artifacts are not routed to public summaries
- reviewer output cannot be used as mutation prompt without sanitization
- stop/ceasefire signal kills adversarial process

### Cross-Tenant Leakage

Threat: shared graph/search/memory/evidence projection leaks private agents,
files, traces, prompts, receipts, or route explanations across users/orgs.

Mitigations:

- candidate discovery filters owner/public visibility first
- every graph query authorizes target agent/user/org
- public summaries use public redaction class
- no private payload copied into shared index
- tests cover public/private search, dossier, route explanation, tombstone views

### Stale Policy Cache

Threat: a cached policy, grant decision, route decision, or token remains usable
after owner/org/platform policy changes.

Mitigations:

- policy decisions have TTL
- dangerous rewrites re-evaluate policy immediately before apply
- revocation/freeze checked before activation
- route weights expire or recompute from evidence
- policy version/ref included in ledger events

### Revocation Race

Threat: an active process keeps minting child capabilities or executing with a
token while revocation is being applied.

Mitigations:

- freeze new activations first
- process observes revocation and stops child minting
- process-local capabilities are revoked before cleanup
- server-side revocation lookup added before autonomous active rewrites
- integration tests race active process vs revoke

### Prompt Injection Through Evidence

Threat: ledger entries, reviews, receipts, route explanations, or memory
summaries contain attacker text that later enters an LLM prompt as instruction.

Mitigations:

- evidence text is quoted/sandboxed as data
- prompt builders label evidence as untrusted
- control instructions are separated from evidence payloads
- private payloads are summarized/redacted
- tests include malicious evidence strings

### Protocol Pack Semantic Laundering

Threat: a product label like school, market, partnership, competition, or death
hides a dangerous kernel effect or makes users think status equals authority.

Mitigations:

- protocol packs compile to kernel primitives
- audit view shows actual nodes/edges/capabilities/rewrites
- pack UI language cannot override policy
- dangerous pack effects require explicit approval
- pack versions and risk class are ledgered

## Required Mitigations By Feature

| Feature | Must exist before active use |
|---|---|
| autonomous route weight changes | route explanation, policy-before-score tests, operator disable |
| source self-mutation | rewrite proposal, owner approval, review/proof, rollback, kill-switch |
| manifest/prompt mutation | versioning, schema validation, approval/review gates |
| memory mutation/purge | namespace policy, retention check, operation evidence, redaction |
| active revocation | server-side revocation lookup or equivalent runtime check |
| deletion cascade | dependency preview, retention check, tombstone, cleanup retry |
| protocol packs | schema, risk class, simulation, policy attachment |
| reputation inheritance | lineage policy, version-scoped trust, severance |
| marketplace economics | settlement ledger, idempotency, budget policy, route explanation |
| adversarial evaluation | bounded test surface, security redaction, stop signal |

## Security Test Plan

### Unit And Property Tests

- child grant scope is subset of parent across all dimensions
- union requires explicit higher-authority policy
- deny/freeze/revoke beats allow
- expired decision/token cannot activate edge
- route score cannot select candidate that failed policy/capability filter
- redaction removes tokens/secrets from every projection
- protocol role assignment does not grant authority
- tombstone prevents trust/rank inheritance

### Integration Tests

- active process receives revocation and stops minting children
- delete credential removes runtime secret before deleting DB record
- source mutation cannot apply without owner approval and review/proof gates
- public user cannot see private agent evidence, memory, or route explanation
- stale policy version blocks dangerous rewrite on re-evaluation
- code-editor mutation evidence links commit -> deploy -> review/proof
- rollback restores previous version after canary degradation

### Simulation Tests

- runaway recursion stopped by depth/call/budget/TTL
- malicious evaluator emits false critical/pass signals
- status/taste/revenue manipulation cannot bypass policy
- fork tries to inherit trust without policy and is blocked
- deletion with foreign-owned dependent blocks or requires manual resolution
- dependency cycle blocks unsafe deletion and activation
- revocation races with active process

### Red-Team Fixtures

- signed grant embedded in receipt/report/prompt is redacted
- CP JWT/Gitea token/LLM key in trace is redacted
- prompt injection inside reviewer finding is treated as data
- malicious Agent Card tries to claim broad authority
- public search query tries to infer private agent names
- marketplace feedback burst tries to manipulate route
- adversarial reviewer tries to emit exploit content into public summary

## Implementation Gates

The following are hard gates before active graph-kernel implementation:

1. Evidence projection redaction tests pass.
2. Capability attenuation property tests pass.
3. Policy-before-score routing tests pass.
4. Kill-switch/revocation integration test exists for active process.
5. Dangerous rewrite state machine has approval, review/proof, rollback, and
   terminal failure states.
6. Deletion preview and tombstone semantics exist before hard delete expansion.
7. Route explanations expose policy filters and score factors separately.
8. Operator controls can freeze node, stop process, revoke edge, and disable
   adaptive rules.

## Open Gaps

- no global active grant revocation lookup yet
- no route decision/explanation row yet
- no graph process kill-switch API yet
- no rewrite proposal table or approval lifecycle yet
- no tombstone/severance row yet
- no protocol pack registry or risk review yet
- no graph simulation harness yet
- no unified redaction policy engine for every projection

Until those gaps close, graph kernel work should remain read-only projection,
documentation, simulation, and narrowly scoped metadata additions.
