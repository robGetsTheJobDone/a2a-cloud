# Dynamic Capability Graph E4 Adversarial Review V0

Date: 2026-06-02

Status: proposal-only gate for epic `1ed3b6ce-0bac-4465-8bc8-3e60471cfb2a`.

## Decision

E4 ships a policy contract for continuous adversarial reviewer loops. Reviewers
can propose findings and self-improvement proposals, but cannot mutate source,
manifest, policy, budget, route, or memory directly.

The implementation lives in `control_plane/self_improvement_proposals.py` and
returns the same audit-only `ProposalDecision` envelope as the v0
self-improvement spine.

## Required Gates

The proposal is denied unless the candidate has:

- findings-only reviewer authority
- no reviewer or evaluator direct-apply authority
- narrowed process-local code-editor authority
- promotion freeze for critical findings
- loop budget ceiling
- TTL
- max iteration count
- all proposed fixes entering `SelfImprovementProposal`
- kill-switch availability
- dossier and timeline visibility
- rollback plan
- operator disable availability

Generic high-risk gates still apply after these E4 gates pass:

- owner approval
- review
- proof
- canary plan

## Stop Condition

Block if a reviewer or evaluator can directly apply source, manifest, policy,
budget, route, or memory changes.

## Non-Goals

- no autonomous source editing
- no direct policy, budget, route, or memory mutation
- no unbounded reviewer loop
- no hidden findings or unredacted payloads

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_self_improvement_proposals.py
```
