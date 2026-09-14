# Dynamic Capability Graph Protocol Packs

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

## Decision

Protocol packs are product-facing labels over typed graph templates. They are
not kernel primitives.

The kernel knows nodes, ports, edges, capabilities, signals, processes,
rewrites, policies, budgets, lineage, deletion, routing, and ledger evidence.
Protocol packs combine those primitives into named, inspectable, policy-bound
processes that product surfaces can present as school, market, employment,
certification, training, retirement, dissolution, competition, status games, or
other long-running dynamics.

This preserves two constraints:

- product teams can build expressive "Life Engine" style metaphors later
- the kernel remains small, mechanical, auditable, and not hard-coded to human
  social labels

Protocol packs may request capabilities, activate processes, consume signals,
emit signals, and propose rewrites. They may not bypass kernel policy. Product
status, money, community rank, social standing, taste, or sex appeal must never
become authority.

## Boundary

| Layer | Owns | Must not own |
|---|---|---|
| Kernel | primitive schema, capability algebra, policy, process state, rewrite gates, ledger refs, redaction, invariants | product metaphors or UI copy |
| Template | allowed graph shape, roles, ports, edges, budgets, signals, rewrites, exit conditions | final authority or hidden state |
| Protocol pack | product label, role names, entry rules, consent flow, UI language, appeal/compensation copy, marketplace semantics | capability widening or policy override |
| Product UI | explanation, controls, approvals, dashboards, public presentation | secret material or unexplained decisions |

Rule: protocol packs compile down to kernel primitives and template instances.
If a protocol needs a behavior the kernel cannot express, add a primitive only
when the behavior is general across protocols.

## Protocol Pack Envelope

Conceptual fields:

| Field | Meaning |
|---|---|
| `protocol_id` | Stable id and version, e.g. `market@v1`. |
| `display_name` | Product-facing label. |
| `kernel_template_refs` | Typed subgraph templates used by the pack. |
| `roles` | Product roles mapped to node/port constraints. |
| `entry_conditions` | Signals, user commands, schedule, marketplace event, policy trigger. |
| `consent_requirements` | Owner/user/org/platform approvals and allowed consent duration. |
| `required_capabilities` | Capabilities needed before activation. |
| `minted_capabilities` | Process-local capabilities minted after policy allows. |
| `signals_consumed` | Signal types the protocol observes. |
| `signals_emitted` | Signal types the protocol may emit. |
| `rewrites_proposed` | Rewrite types the protocol may propose. |
| `child_templates` | Subprocesses it may activate. |
| `budgets` | Calls, depth, runtime, LLM spend, traffic share, money, retries. |
| `exit_conditions` | Success, failure, expiry, cancellation, appeal, settlement, dissolution. |
| `rollback_behavior` | Undo, reroute, compensate, revoke, tombstone, sever lineage. |
| `appeal_behavior` | Human/operator review path when applicable. |
| `ledger_requirements` | Required evidence for entry, transition, exit, compensation. |
| `redaction_policy` | What public/owner/internal views may reveal. |
| `ui_language` | Product labels and explanation copy. |
| `invariants` | Safety properties the pack must preserve. |

Pack activation still goes through P5 policy and P6 process scheduling.

## Role Mapping

Roles are labels over node/port constraints.

Examples:

| Protocol role | Kernel mapping |
|---|---|
| student | subject node with learn/attempt/receive-feedback ports |
| teacher | evaluator/planner node with scoped evaluate/signal ports |
| employer | task router/funder node with payment and review policies |
| worker | executable agent with callable skill ports |
| spouse/partner | mutually consenting nodes with shared memory/budget/routing edges |
| market maker | router/pricing/status node with explainable ranking policy |
| certifier | evaluator node with certify port and revocation policy |
| opponent | adversarial evaluator node with bounded test/attack surface |
| executor | mutator/deleter/revoker node with dangerous rewrite gates |

Rules:

- a role is not a privilege
- role assignment requires policy and evidence
- roles may narrow capabilities, not widen them
- role labels must be redacted or localized by product surface
- a node can hold multiple roles only if policy allows conflict-free activation

## Consent And Approval

Protocol packs must declare consent requirements explicitly.

Consent dimensions:

- who consents
- what capability or process is allowed
- what resources are touched
- how long consent lasts
- what child processes may be started
- what can be published
- what can be charged
- what can be revoked
- what appeal/rollback path exists

Rules:

- consent is evidence, not ambient authority
- consent expires
- consent may be narrower than protocol default
- lower policy cannot expand consent beyond owner/platform ceiling
- dangerous protocols require explicit approval and ledger refs
- dissolution, termination, retirement, deletion, and appeal must be modeled

## Protocol Cards

### School

Product label: school, course, training, certification path.

Kernel shape:

```text
subject output
  -> evaluator/curriculum node
  -> feedback signal
  -> remediation planner
  -> practice/task process
  -> proof/review
  -> certification or more remediation
```

Roles:

- learner: subject agent or human
- instructor: evaluator/planner node
- examiner: proof/review node
- certifier: certification node
- sponsor: budget account or owner

Consumes:

- task_failed
- capability_gap
- review_warning
- proof_failed
- user_requested_training

Emits:

- lesson_assigned
- practice_completed
- review_passed
- certification_granted
- certification_revoked
- remediation_required

Rewrites proposed:

- attach_evaluator
- activate_process
- mutate_prompt
- mutate_manifest
- mutate_source
- certify
- increase/decrease routing weight

Invariants:

- instructor cannot mutate learner unless separately authorized
- certification is version/skill scoped
- graduation does not grant broader data authority
- curriculum budget is bounded

### Market

Product label: marketplace, exchange, bidding, agent economy.

Kernel shape:

```text
request
  -> candidate discovery
  -> policy/capability filters
  -> price/trust/status/taste scoring
  -> route selection
  -> execution
  -> receipt/proof
  -> spend/revenue/share ledger
```

Roles:

- buyer/caller
- seller/agent owner
- agent/service
- market router
- budget account
- settlement processor
- reviewer/certifier

Consumes:

- user_feedback
- conversion
- cost
- revenue
- proof_passed
- review_failed
- unsafe_behavior
- taste_signal

Emits:

- route_selected
- purchase_completed
- budget_spent
- revenue_earned
- reputation_delta
- rank_changed

Rewrites proposed:

- increase/decrease route weight
- mutate_price_or_budget
- allocate_budget
- transfer_revenue
- retire listing
- revoke certification

Invariants:

- price/rank/status never grants capability
- revenue does not widen scope
- route must explain policy filters before score
- settlement is idempotent and ledger-backed

### Employment

Product label: job, contract, assignment, task routing, termination.

Kernel shape:

```text
employer request
  -> worker route
  -> scoped task capability
  -> execution receipt
  -> review/payment
  -> renewal, remediation, or termination
```

Roles:

- employer: task source/funder
- worker: agent/person executing skill
- manager: router/reviewer
- payroll: budget/settlement node
- compliance: policy/review node

Consumes:

- task_succeeded
- task_failed
- review_warning
- budget_exceeded
- policy_violation

Emits:

- assignment_started
- assignment_completed
- payment_due
- performance_signal
- termination_notice

Rewrites proposed:

- activate_process
- allocate_budget
- attach_evaluator
- decrease route weight
- revoke task capability
- retire relationship edge

Invariants:

- employment relation does not create data access by itself
- each task needs scoped capability
- termination preserves audit and payment evidence
- worker rank cannot override safety/policy findings

### Certification

Product label: certification, license, badge, reviewer pass.

Kernel shape:

```text
candidate version/skill
  -> certifier evaluator
  -> proof/review evidence
  -> certification edge
  -> renewal/decay/revocation policy
```

Roles:

- subject
- certifier
- verifier
- revoker

Consumes:

- proof_passed
- review_passed
- drift_detected
- unsafe_behavior
- stale evidence

Emits:

- certification_granted
- certification_warning
- certification_revoked
- renewal_required

Rewrites proposed:

- certify edge
- revoke certification
- require proof
- lower route weight

Invariants:

- certification is scoped by version, skill, card hash, dependency set, and
  policy
- certification does not imply owner approval for mutation
- revocation blocks inheritance

### Adversarial Evaluation

Product label: war, competition, red team, challenge, adversarial test.

Kernel shape:

```text
subject
  -> adversarial evaluator
  -> bounded attack/test surface
  -> safety/performance signals
  -> remediation, certification, or freeze
```

Roles:

- subject
- adversary/evaluator
- referee/policy node
- remediator
- operator

Consumes:

- user_requested_test
- certification_request
- unsafe_behavior
- repeated_failure

Emits:

- vulnerability_found
- robustness_passed
- unsafe_behavior
- remediation_required

Rewrites proposed:

- freeze_edge
- activate_mutator
- require_review
- revoke capability
- certify or decertify

Invariants:

- adversary receives only bounded test authority
- adversary cannot exfiltrate secrets
- test output is redacted by security policy
- ceasefire/stop signal kills active adversarial process

### Partnership

Product label: marriage, partnership, shared project, collaboration.

Kernel shape:

```text
node A + node B
  -> mutual consent
  -> shared process-local memory/budget/routing edges
  -> joint outcomes
  -> renewal or dissolution/revocation
```

Roles:

- partner A
- partner B
- shared memory/resource
- budget account
- mediator/policy node

Consumes:

- mutual_consent
- task_succeeded
- budget_exceeded
- policy_violation
- dissolution_requested

Emits:

- partnership_started
- shared_resource_used
- partnership_updated
- partnership_dissolved

Rewrites proposed:

- add shared edge
- allocate budget
- mutate shared memory policy
- revoke shared edge
- tombstone partnership

Invariants:

- mutual consent is required
- shared edges are scoped and revocable
- dissolution revokes process-local capabilities
- one partner cannot expand shared scope unilaterally

### Death And Retirement

Product label: death, retirement, archival, deletion.

Kernel shape:

```text
retirement signal/request
  -> dependency check
  -> active process check
  -> freeze/revoke/purge
  -> tombstone
  -> lineage severance
```

Roles:

- target
- owner
- dependency checker
- revoker
- retention checker
- tombstone writer

Consumes:

- owner_delete_request
- unsafe_behavior
- idle_decay
- policy_violation
- retention_expired

Emits:

- node_retired
- credential_revoked
- data_purged
- identity_tombstoned
- lineage_severed

Rewrites proposed:

- freeze_node
- revoke_edge
- revoke_credential
- purge_namespace
- hard_delete_node
- tombstone_identity
- sever_lineage

Invariants:

- deletion follows P9 dependency and retention checks
- audit evidence is preserved or tombstoned by policy
- lineage severance is explicit
- hard delete is not the first step for active nodes

## Protocol Pack Lifecycle

```text
draft
  -> schema_validated
  -> policy_reviewed
  -> sandbox_simulated
  -> enabled_private
  -> enabled_public
  -> deprecated
  -> retired
```

Lifecycle rules:

- new packs start disabled
- dangerous packs require policy/security review
- public packs need redaction review
- pack versions are immutable once enabled
- deprecated packs continue to explain historical process evidence
- retired packs cannot start new processes but can remain in ledger refs

## Pack Registry

For v0, the registry can be docs plus versioned schemas and tests.

Later registry fields:

| Field | Meaning |
|---|---|
| `protocol_id` | Pack id and version. |
| `template_refs` | Kernel templates used. |
| `risk_class` | Highest risk effect the pack can propose. |
| `policy_refs` | Required platform/org/owner policies. |
| `enabled_for` | Platform/org/marketplace/user scope. |
| `owner` | Maintainer or org. |
| `public_payload` | UI-safe pack summary. |
| `redaction_policy` | Audience-specific output rules. |
| `simulation_refs` | Scenario set proving invariants. |
| `deprecated_at` | Optional deprecation timestamp. |
| `retired_at` | Optional retirement timestamp. |

Registry responsibilities:

- validate pack shape
- bind pack version to process evidence
- expose UI-safe description
- enforce enablement and deprecation
- provide simulation fixtures
- link to policy gates

## UI And Language

Protocol packs own display language. The kernel owns explanation facts.

UI may say:

- "course started"
- "agent certified"
- "market route selected"
- "partnership dissolved"
- "agent retired"

Kernel explanation must say:

- which node/edge/process changed
- which capability allowed it
- which policy allowed or blocked it
- which signals and evidence refs were used
- which rewrite was proposed or applied
- which data was retained, revoked, deleted, or tombstoned

Rule: UI copy can be expressive, but the audit view must always reduce it to
kernel primitives.

## V0 Build Guidance

Do not build a protocol-pack runtime first.

Practical order:

1. Keep protocol packs as docs/schemas/examples.
2. Reuse P7 typed subgraph templates as the mechanical layer.
3. Use P18 simulations to validate pack invariants.
4. Let Agent Studio and meta-agent flows continue on current rails.
5. Add pack ids as optional metadata on future process/evidence rows only when
   product surfaces need them.
6. Add a registry only after more than one product pack needs enablement,
   versioning, or UI discovery.

Suggested minimal pack metadata:

```text
protocol_ref:
  id: market
  version: 1
  template_refs:
    - route_selection@v1
    - budget_settlement@v1
  display_name: Marketplace routing
  risk_class: high
```

## Open Gaps

- no protocol pack schema file yet
- no signed/validated pack registry
- no process rows that carry protocol pack id/version
- no pack-level simulation suite
- no UI explorer for protocol instances
- no enablement/deprecation workflow
- no policy attachment per protocol pack
- no product feedback/taste/market ledgers yet
- no settlement ledger for market/employment packs

These gaps are expected. Protocol packs should stay above the kernel until the
evidence DAG, route explanations, policy explanations, and simulations prove the
common substrate.
