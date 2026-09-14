# Dynamic Capability Graph E2 Collective Memory V0

Date: 2026-06-02

Status: proposal-only gate for epic `1ed3b6ce-0bac-4465-8bc8-3e60471cfb2a`.

## Decision

E2 ships a policy contract for collective memory and stigmergic memory
propagation. It does not write memory, propagate memory, purge memory, create
shared namespaces, or grant cross-agent read/write authority.

The implementation lives in `control_plane/self_improvement_proposals.py` and
returns the same audit-only `ProposalDecision` envelope as the v0
self-improvement spine.

## Collective Memory

A collective-memory candidate describes a proposed memory operation:

- write
- summarize
- redact
- purge
- propagate

The evaluator emits a deterministic memory operation id and a redacted evidence
payload containing namespace, target agent refs, memory refs, authority preview,
retention preview, redaction preview, stale/unsafe review status, and
dossier/timeline visibility flags.

## Required Gates

The proposal is denied unless the candidate has:

- scoped memory namespace authority
- retention/legal policy check
- redaction proof
- summary-only projection
- stale memory review before propagation
- unsafe memory review before propagation
- dossier and timeline visibility
- operator disable availability
- rollback plan

Generic high-risk gates still apply after these E2 gates pass:

- owner approval
- review
- proof
- canary plan

## Stop Condition

Block if a memory write can propagate to another agent without namespace
authority, retention check, redaction proof, and stale/unsafe review.

Purge is blocked under legal hold. Raw memory payload projection is blocked
unless future object-access policy explicitly models it.

## Non-Goals

- no first-class memory operation table
- no automatic memory write or propagation
- no automatic purge, summarization, or redaction
- no shared namespace creation
- no cross-agent authority grant
- no public raw memory view

## Verification

Run:

```bash
.venv/bin/python -m pytest tests/test_self_improvement_proposals.py
```
