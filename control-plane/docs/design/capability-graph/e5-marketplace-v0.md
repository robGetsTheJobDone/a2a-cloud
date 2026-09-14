# Dynamic Capability Graph E5 Autopoietic Marketplace V0

Date: 2026-06-02

Status: proposal-only gate for epic `1ed3b6ce-0bac-4465-8bc8-3e60471cfb2a`.

## Decision

E5 ships a policy contract for marketplace-driven creation, promotion,
retirement, pricing, and routing proposals. Demand, rank, revenue, and taste
signals are evidence only. They cannot grant authority, mutate policy, delete
agents, or move money.

The implementation lives in `control_plane/self_improvement_proposals.py` and
returns the same audit-only `ProposalDecision` envelope as the v0
self-improvement spine.

## Required Gates

The proposal is denied unless the candidate has:

- E1 through E4 gates passed
- no signal-driven authority grant
- no signal-driven policy mutation
- no signal-driven deletion
- no signal-driven money movement
- platform approval
- owner approval
- review
- proof
- canary plan
- rollback plan
- operator disable availability

## Stop Condition

Block if market rank, user taste, revenue, or demand can create authority,
mutate policy, delete agents, or move money without owner and platform gates.

## Non-Goals

- no autonomous supply creation
- no autonomous pricing or payout
- no autonomous promotion or retirement
- no marketplace rank becoming authority
- no policy mutation from demand, revenue, taste, or status

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_self_improvement_proposals.py
```
