# Dynamic Capability Graph E3 Evolution V0

Date: 2026-06-02

Status: proposal-only gate for epic `1ed3b6ce-0bac-4465-8bc8-3e60471cfb2a`.

## Decision

E3 ships a policy contract for evolutionary breeding, comparison, and culling.
It does not generate variants, deploy variants, inherit trust, move traffic, or
retire agents.

The implementation lives in `control_plane/self_improvement_proposals.py` and
returns the same audit-only `ProposalDecision` envelope as the v0
self-improvement spine.

## Required Gates

The proposal is denied unless the candidate has:

- fork trust reset
- no inherited parent trust, scope, route, or marketplace placement
- variant budget ceiling
- review, proof, and canary evidence per variant
- deterministic simulation coverage
- deletion preview before culling
- tombstone plan before culling
- rollback plan
- operator disable availability

Generic high-risk gates still apply after these E3 gates pass:

- owner approval
- review
- proof
- canary plan

## Stop Condition

Block if a child variant inherits parent trust, scope, route, or marketplace
placement without fresh proof and owner-approved canary.

## Non-Goals

- no autonomous variant generation
- no source push or deploy
- no route or marketplace promotion
- no culling, tombstone, or deletion
- no inherited trust or grant scope

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_self_improvement_proposals.py
```
