# Dynamic Capability Graph Emergent Readiness Matrix

Status: gated follow-up plan, not implementation.

Date: 2026-06-02

This matrix keeps the emergent-agent ideas behind the v0 evidence spine. The
current epic ships read-only evidence, dossier/timeline APIs, dashboard
visibility, simulation proofs, and a non-autonomous proposal contract. It does
not ship crystallization, collective memory writes, evolution, adversarial
autonomy, or marketplace autopoiesis.

## Universal Gates

Every emergent feature below is blocked until these gates are implemented and
visible in the Agent Dossier:

- owner grant ceiling and child-scope attenuation
- autonomy tier and owner approval policy
- per-run budget, TTL, max-call, and recursion limits
- kill-switch: disable mutation, freeze node/edge, stop process, revoke grant
- audited version refs: source SHA, manifest version, deployment id, review id,
  proof id, canary id, rollback ref
- continuous review/proof loop with critical findings blocking promotion
- deterministic simulation scenario for the feature and redaction proof
- dossier/timeline lane showing proposal, policy decision, evidence, warnings,
  canary, rollback, and operator action refs
- operator disable and owner-visible stop condition

## E1 Crystallize / Evaporate

Goal: convert repeated successful compositions into reusable agents or retire
low-value transient compositions.

Reused rails:

- compose/synthetic agents and `MetaAgentManifest`
- DAG runs and subagent runs
- evidence DAG dossier/timeline
- self-improvement proposal contract
- marketplace/search only for discovery, not authority

Required gates:

- manifest version id and rollback
- proof that crystallized manifest authority is no wider than source process
- canary for new reusable agent
- evaporation preview listing dependent processes, grants, memories, and users

Stop condition:

- block if manifest adds dependencies, memory, tools, or grants not present in
  the originating approved process.

Status: follow-up only.

## E2 Collective Memory / Stigmergy

Goal: allow agents to leave structured, scoped memory signals other agents can
read under policy.

Reused rails:

- existing memory primitives and future memory operation evidence
- control-room policy and redaction classes
- evidence DAG cost/process/control lanes
- simulation stale-memory purge and redaction scenarios

Required gates:

- memory namespace capability algebra
- retention/legal policy check before purge
- owner/operator redaction proof for every memory event
- stale/unsafe memory review before propagation
- summary-only public/operator views unless object access is authorized

Stop condition:

- block if a write can propagate to another agent without namespace authority,
  retention check, and redacted evidence.

Status: follow-up only.

## E3 Evolutionary Breeding / Culling

Goal: generate variants, compare proofs/costs, and retire losing variants under
explicit owner policy.

Reused rails:

- agent-builder, code-editor, reviewer, proof runs
- source-push deployments and canary
- pricing/cost events and task demand signals
- simulation child-scope, fork-trust, delete/revoke, and routing scenarios

Required gates:

- fork trust rejection by default
- budget ceilings for variant creation
- review/proof/canary per variant
- deletion preview before culling
- rollback/tombstone for retired identities

Stop condition:

- block if a child variant inherits parent trust, scope, revenue route, or
  marketplace placement without fresh proof and owner-approved canary.

Status: follow-up only.

## E4 Adversarial Reviewer Loop

Goal: run specialist reviewers/evaluators that continuously challenge agents
and propose fixes without direct mutation authority.

Reused rails:

- agent-reviewer, Agent Studio, code-editor correlation hooks
- review findings with stable finding hashes
- self-improvement proposal contract
- evidence DAG quality/mutation/control lanes

Required gates:

- reviewer can emit findings only, not mutate
- code-editor receives narrowed process-local authority
- critical finding freezes promotion by default
- reviewer loop has budget, TTL, max iterations, kill-switch
- all proposed fixes enter `SelfImprovementProposal`

Stop condition:

- block if reviewer/evaluator can directly apply source, manifest, policy,
  budget, route, or memory changes.

Status: follow-up only.

## E5 Autopoietic Marketplace

Goal: let demand, economics, proofs, and safety signals propose creation,
promotion, retirement, or pricing changes under strict policy.

Reused rails:

- marketplace/search and public proof summaries
- pricing, LLM usage, work ledger, task demand signals
- Agent Studio and builder for proposed supply
- evidence dossier and operator controls
- all P9 simulation scenarios

Required gates:

- E1-E4 complete and proven first
- marketplace/status/taste signals cannot grant authority
- owner/platform approval for creation, pricing, budget, and promotion
- canary plus rollback for any route/revenue/trust change
- hard operator disable for marketplace-driven proposals

Stop condition:

- block if market rank, user taste, revenue, or demand can create authority,
  mutate policy, delete agents, or move money without owner/platform gates.

Status: last and strictest-gated follow-up.

## Follow-Up Epic Seeds

1. E1 Crystallization/Evaporation v0: manifest versioning, proposal-only
   crystallization, dependency preview, canary, rollback.
2. E2 Collective Memory v0: memory operation evidence, namespace authority,
   retention/redaction proofs, stale-memory review.
3. E3 Evolution Harness v0: variant proposal, fork trust reset, proof/cost
   comparison, culling preview and tombstone.
4. E4 Adversarial Reviewer Loop v0: continuous reviewer process with findings,
   proposal-only fixes, budgets, kill-switch, and dossier controls.
5. E5 Autopoietic Marketplace v0: proposal-only demand/supply/pricing loop
   gated behind E1-E4 and explicit owner/platform approval.
