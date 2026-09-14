# Operate the runtime

Runtime is the operator view across active work. It connects health, routing,
policy, receipts, memory, dispatch, proofs, and the files or artifacts a run
produced.

## Overview

[Runtime Overview](https://app.a2acloud.io/runtime) shows current health, live
DAG runs, spend posture, and the latest control receipt. Start here when the
question is “what is the platform doing now?”

## Timeline

[Timeline](https://app.a2acloud.io/runtime/timeline) joins:

- active and completed DAG work
- runtime receipts
- agent routing and handoffs
- spend signals and LLM usage
- file operations and artifacts
- proof and deployment status

Receipts are evidence records, not accounting records. Runtime receipts
sign the execution fields that were actually collected. Usage metrics,
evaluations, and reviews remain separately queryable records unless a receipt
explicitly links them.

## Policy

[Policy](https://app.a2acloud.io/runtime/policy) controls:

- budget and spend caps
- approval gates
- agent allowlists
- network and egress posture
- PII-safe handling
- thread or account defaults

Policy denial is a recorded outcome. The runtime does not silently relax a
budget, scope, network, or approval rule to finish a job.

## Proof, evidence, and receipts

Use the agent's **Proofs** section to execute a named tool against a bounded
case. Use **Evidence** for the deployment/proof/run DAG and timeline. Use
**Runs** or Runtime Timeline for an individual execution receipt.

A strong production chain is:

1. source revision
2. deployment
3. live agent card
4. proof run
5. scoped grant
6. execution and artifacts
7. receipt
8. organization audit or compliance record

Missing evidence is shown as a gap; the dashboard does not infer a passing proof
from a healthy endpoint.

## Self-healing

`SelfHealingPolicy` is an opt-in deployment policy with bounded consecutive
failures, time window, cooldown, daily repair limit, turn limit, deployment
timeout, and test requirement. The control plane applies those limits. An agent
without the policy does not automatically rewrite its source.

See [`SelfHealingPolicy`](/reference/runtime#selfhealingpolicy-class).

## Simulations

Simulations are feature-gated. When enabled, the lab can build specs, run
bounded or live-agent scenarios, replay receipts, and inspect evolution
proposals. Simulation evidence is not a production proof until it is explicitly
run and recorded against the intended live agent/version.

## Incident path

When work fails:

1. Find the job in Activity or Runtime Timeline.
2. Check policy, approval, and setup outcomes before assuming agent
   code failed.
3. Inspect the grant and receipt.
4. Open related files, artifacts, proof, review, and deployment evidence.
5. For an opted-in agent, inspect repair attempts and cooldown state.
6. Re-run only after the blocking authority, setup, or code condition is fixed.

For endpoint automation, see [Control-plane API](/reference/control-plane-api).
