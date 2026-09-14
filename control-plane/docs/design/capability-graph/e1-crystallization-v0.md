# Dynamic Capability Graph E1 Crystallization V0

Date: 2026-06-02

Status: proposal-only gate for epic `1ed3b6ce-0bac-4465-8bc8-3e60471cfb2a`.

## Decision

E1 ships a policy contract for crystallization and evaporation. It does not
create reusable agents, retire agents, mutate manifests, or widen authority.

The implementation lives in `control_plane/self_improvement_proposals.py` and
returns the same audit-only `ProposalDecision` envelope as the v0
self-improvement spine.

## Crystallization

A crystallization candidate compares the originating process manifest with the
proposed reusable manifest. The proposal is denied if the proposed manifest adds
any of these bounds:

- sub-agent dependencies
- child skills
- default argument keys
- memory tiers, namespace, scope, or retention
- runtime tools
- CP JWT or workspace grant scope

Required gates:

- repeated successful evidence count
- owner approval
- review
- proof
- canary plan
- rollback plan
- operator disable availability

## Evaporation

An evaporation candidate returns a dependency preview before any retirement can
be approved. The preview lists dependent processes, grants, memories, and users.

Evaporation remains a high-risk proposal. It requires owner approval, review,
proof, canary, rollback, and operator disable availability before it can become
approved. Even an approved decision has `active_apply_enabled=false`.

## Non-Goals

- no first-class mutation table
- no materialized graph table
- no active manifest mutation
- no automatic agent creation
- no automatic agent retirement
- no marketplace promotion or demotion

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_self_improvement_proposals.py
```
