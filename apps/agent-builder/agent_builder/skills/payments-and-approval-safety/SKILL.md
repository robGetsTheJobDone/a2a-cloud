---
name: payments-and-approval-safety
description: Build and review agents that create charges, refunds, credits, invoices, payouts, purchases, account changes, or other consequential actions. Use whenever money, permissions, deletion, publication, or customer-facing commitments require policy and approval boundaries.
---
# Payments And Approval Safety

Default to proposing an action, not executing it.

## Action boundary

- Split `preview`/`propose` from `execute` tools. A preview returns the normalized action, policy result, amount/currency, and required approval.
- Require an explicit approval token or platform approval step for execution.
- Never infer approval from conversational enthusiasm, an email body, or a model-generated string.
- Reject amounts without an explicit currency and enforce positive bounded values.
- Bind approval to the exact action digest so changed amounts, recipients, or metadata require new approval.

## Provider calls

- Declare provider credentials through `ConsumerSetupField.secret` and exact egress hosts.
- Use idempotency keys for every mutation and persist the provider operation ID.
- Reconcile ambiguous timeouts by querying provider state before retrying.
- Never store card data, bank details, access tokens, or full provider payloads in artifacts or logs.

## Policy

- Encode refund/purchase limits as deterministic checks outside the LLM.
- Fail closed when policy, identity, currency, approval, or provider state is unknown.
- Return `proposed`, `approval_required`, `executed`, `declined`, or `needs_reconciliation`; do not collapse them into `ok`.

## Acceptance checks

- Test over-limit, wrong-currency, missing approval, tampered approval, duplicate execution, provider timeout, and reconciliation paths.
- Sandbox tests must use a fake provider. Never send a real transaction while building.
