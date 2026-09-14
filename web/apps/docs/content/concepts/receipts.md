# Receipts — signed evidence for one run

An **execution receipt** is a small, immutable, Ed25519-signed record of a
single skill invocation: what ran, who called it, under what authority, what it
touched, and what came out.

A log answers "what does the operator's database say happened?". A receipt
answers a different question: **"can I check that myself?"** The receipt is
signed over its own canonical payload, so anyone holding the token and the
public key can confirm it has not been altered since it was sealed — without an
account, without an API call, and without trusting the party that handed it to
them. Change one byte and verification fails.

That cuts both ways: a token is readable by whoever holds it, and it carries
short previews of the call's input and result. Handing one out is a disclosure
as well as a proof — see
[what a token discloses](#verify-a-receipt) before forwarding one.

The schema and the sign/verify primitives live in `a2a_pack.receipts`. Storage
and retrieval are the control plane's job; the receipt itself is designed to be
inspectable on the wire with no database round-trip.

## Fields

Every field below is declared on `ExecutionReceipt` (a frozen Pydantic model,
`extra="forbid"`). Field order in the table is the declaration order, which is
also the key order used when the payload is serialized for signing.

### Identity

| Field | Type | What it holds |
| --- | --- | --- |
| `receipt_id` | `str` | Random 8-byte hex id, assigned at seal time. |
| `schema_version` | `int` | Currently `1`. |
| `agent_name` | `str` | The agent that executed the call. |
| `agent_version` | `str` | Agent version string; empty when the card declares none. |
| `caller` | `str` | Caller agent name, a user id such as `user:42`, or empty. |
| `task_id` | `str` | Task correlation id, or empty. |

### What ran

| Field | Type | What it holds |
| --- | --- | --- |
| `skill_name` | `str` | The tool/skill that was invoked. |
| `input_hash` | `str` | Hex SHA-256 of the inputs after JSON canonicalization (sorted keys, compact separators). **A hash — not the inputs.** |
| `input_preview` | `str` | The inputs rendered short: a `str` is stored as-is, anything else is JSON-serialized; either way truncated to 240 characters with a trailing `…` when cut. **A preview — not the whole input.** |

Inputs are never stored in full on the receipt. `input_hash` lets someone who
already has the input prove it is the one that ran; it does not let anyone
recover an input they do not have.

### Authority

| Field | Type | What it holds |
| --- | --- | --- |
| `grant_ids` | `tuple[str, ...]` | Ids of the [grants](/concepts/grants) the run carried. Ids only — the grant bodies live in the grant audit trail. |

### Effects

| Field | Type | What it holds |
| --- | --- | --- |
| `file_ops` | `FileOps` | `reads`, `writes`, `bytes_read`, `bytes_written` (counts are authoritative) plus `read_paths_preview` / `write_paths_preview` (truncated, best-effort). |
| `tool_calls` | `tuple[ToolCall, ...]` | Per call: `name`, `args_hash`, `status`, `elapsed_ms`. Arguments are stored **by hash, never by payload**. |
| `artifacts` | `tuple[ArtifactRef, ...]` | Per artifact: `path`, `mime_type`, `bytes`. A pointer, not the bytes. |
| `handoffs` | `tuple[Handoff, ...]` | Per agent-to-agent call: `callee`, `skill`, `grant_id`, `elapsed_ms`, `status`. |

### Outcome

| Field | Type | What it holds |
| --- | --- | --- |
| `status` | `str` | `"ok"`, `"error"`, `"cancelled"`, or `"partial"`. |
| `error_type` | `str` | Exception/error class name when the run failed. |
| `result_preview` | `str` | The result rendered the same way: a `str` is stored as-is (no quotes, no escaping), anything else is JSON-serialized; truncated to 240 characters. **A preview — not the result.** |
| `eval_score` | `float \| None` | Score attached by an evaluator, when one ran. |
| `reviewer` | `str` | Identifier of the reviewer/evaluator, when one ran. |

### Timing

| Field | Type | What it holds |
| --- | --- | --- |
| `started_at` | `int` | Unix seconds. |
| `ended_at` | `int` | Unix seconds. |
| `elapsed_ms` | `int` | Duration in milliseconds. |
| `nonce` | `str` | Random 8-byte hex, so two otherwise-identical runs never seal to the same payload. |

### What is populated today

Sealing is done by whoever observed the run, and an observer only fills what it
actually saw. On hosted a2a cloud, receipts for governed calls are sealed by the
gateway *after* the response completes, from outside the agent process. That
observer fills identity, what-ran, outcome, and timing. It does **not** fill
`file_ops`, `tool_calls`, `artifacts`, `handoffs`, `eval_score`, or `reviewer` —
those arrive at their empty defaults. Treat them as schema fields that a
richer observer can populate, not as a promise about hosted runs today.

Two more honest details about the hosted path:

- Request bodies larger than the gateway's capture limit are not hashed or
  previewed. `input_preview` then reads
  `[omitted: request body unavailable or exceeded capture limit]` and
  `input_hash` is empty.
- Oversized responses yield a `result_preview` of
  `{"response_preview": "[omitted: response exceeded capture limit]"}`.

## Wire format

```text
<base64url(json(payload))>.<base64url(ed25519_signature(payload))>
```

Both segments are unpadded base64url. The payload is the model's
`model_dump_json()` output — stable key order from the field declarations — and
the signature covers those exact bytes. Decoding is strict: a non-canonical
base64url segment is rejected before the signature is even checked.

A decoded payload looks like this:

```json
{
  "receipt_id": "4b4d56883bb4d526",
  "schema_version": 1,
  "agent_name": "research-agent",
  "agent_version": "",
  "caller": "user:42",
  "task_id": "",
  "skill_name": "ask",
  "input_hash": "ad6b174eb01357d875c98f53aef35ca9c96437ed54de7974b507a5221563a435",
  "input_preview": "{\"prompt\": \"say hello\"}",
  "grant_ids": [],
  "file_ops": {
    "reads": 0,
    "writes": 0,
    "bytes_read": 0,
    "bytes_written": 0,
    "read_paths_preview": [],
    "write_paths_preview": []
  },
  "tool_calls": [],
  "artifacts": [],
  "handoffs": [],
  "status": "ok",
  "error_type": "",
  "result_preview": "hello",
  "eval_score": null,
  "reviewer": "",
  "started_at": 1717439700,
  "ended_at": 1717439703,
  "elapsed_ms": 3000,
  "nonce": "d6290a0f68fcbca4"
}
```

## Where receipts show up

### Response headers

Every governed call through the gateway returns receipt headers:

```text
X-A2A-Receipt-ID:         4b4d56883bb4d526
X-A2A-Receipt-URL:        https://api.a2acloud.io/v1/agents/research-agent/receipts/4b4d56883bb4d526
X-A2A-Receipt-Token:      eyJyZWNlaXB0X2lkIjoiNGI0ZDU2ODgzYmI0ZDUyNiIs….3Qm…
X-A2A-Replay-Session-ID:  0f1c…
X-A2A-Replay-URL:         https://api.a2acloud.io/v1/sessions/0f1c…
X-A2A-Replay-Token:       eyJzZXNzaW9uX2lkIjoi….9aB…
```

Streaming (`text/event-stream`) responses are the exception: headers flush
before the run ends, so an SSE response carries only the `-ID` and `-URL`
headers. The signed tokens arrive at the end of the stream in a terminal
`a2a.evidence` event.

### Inline in the response body

JSON responses also carry the same bundle in the body: `a2a_evidence` on
`/invoke` and other JSON transports, and `result._meta.a2aCloudEvidence` for
MCP. Each bundle holds the receipt id, its signed token, and its retrieval URL —
plus the matching replay session.

### Control-plane endpoints

| Method | Path | Returns |
| --- | --- | --- |
| `GET` | `/v1/public/receipt-keys` | The public verifying key(s). No auth. |
| `GET` | `/v1/agents/{name}/receipts` | Newest-first receipt headers (`limit`, `before` paging). |
| `GET` | `/v1/agents/{name}/receipts/{receipt_id}` | One receipt: full payload plus its `signed_token`. |
| `POST` | `/v1/agents/{name}/receipts` | Self-reported receipt from an agent. The control plane verifies the signature and stores it; it never re-signs. |

The full generated route inventory is in the
[control-plane API reference](/reference/control-plane-api).

### Dashboard

Receipts surface in **Activity** (the account-wide ledger) and in **Runtime**
(per-run timeline, alongside the grant and the replay session). See
[Operate the runtime](/platform/runtime) and
[Workspace](/platform/workspace).

## Verify a receipt

### 1. Fetch the public key

```bash
curl -sS https://api.a2acloud.io/v1/public/receipt-keys
```

```json
{
  "active_kid": "56475aa75463474c",
  "keys": [
    {
      "kid": "56475aa75463474c",
      "alg": "Ed25519",
      "public_key": "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=",
      "use": "receipt"
    }
  ]
}
```

`public_key` is base64 of the raw 32-byte Ed25519 public key. `kid` is the first
16 hex characters of `sha256(raw public key bytes)`, so you can derive it
yourself and confirm the key you were handed is the key that is published. The
endpoint needs no auth and is safe to cache. It returns `503` when no signing
key is configured — it never invents one.

> The key material shown throughout this page comes from a locally generated
> demo keypair, so the values are illustrative. Fetch the real one from the
> endpoint above.

### 2. Get a receipt

`a2a call` and the typed `a2a <agent> <tool>` commands read the receipt headers
off every governed response, cache the receipt under `~/.a2a/receipts/<id>.json`
(override with `A2A_RECEIPTS_DIR`), and print one dim pointer line on **stderr**
— so piping the result into `jq` still works:

```bash
a2a call research-agent ask prompt="say hello"
```

```text
receipt  4b4d56883bb4d526  ->  a2a receipt verify 4b4d56883bb4d526
```

Agents that return no receipt headers — local `a2a dev --local` runs, ungoverned
imports — print nothing.

The line is deliberately terse here, and that is a real limitation rather than a
style choice. `a2a call` negotiates SSE for every tool call, so the gateway can
only send the `-ID` and `-URL` headers before the stream opens; the signed token
is not in the headers it sees. The CLI therefore caches an id and a URL but no
token, which has two consequences worth knowing:

- The pointer line can only show the id. Status and elapsed time are read out of
  the token, so they are omitted.
- `a2a receipt verify <id>` after a call is **not** offline. With no cached
  token it fetches the stored receipt from the control plane, which needs
  `a2a login` and ownership of the agent.

To hold the token yourself — which is what makes verification offline and
hand-off-able — read it off a response that is not streamed. `POST /invoke/…`
returns JSON unless you ask for `text/event-stream`, so the full header set is
present:

```bash
curl -sS -D headers.txt -o result.json \
  -X POST https://research-agent.a2acloud.io/invoke/ask \
  -H 'Content-Type: application/json' \
  -d '{"arguments": {"prompt": "say hello"}}'

grep -i '^x-a2a-receipt-token:' headers.txt | cut -d' ' -f2- | tr -d '\r' > receipt.token
```

> **A receipt token is not redacted.** It embeds `input_preview` (up to 240
> characters of the caller's input), `result_preview` (up to 240 characters of
> the output) and the run's `grant_ids` — all readable by anyone who can base64-
> decode, before any signature check. That is exactly why the control plane
> keeps stored receipts owner-only and publishes no public receipt view. Handing
> a token to a customer or an auditor proves the run happened *and* discloses
> those previews. Check what is in yours with `a2a receipt show` before you
> forward it.

### 3. Verify it

```bash
a2a receipt verify 4b4d56883bb4d526
```

```text
PASS 4b4d56883bb4d526  research-agent.ask · ok · 3000ms
  Ed25519 signature valid · key from https://api.a2acloud.io/v1/public/receipt-keys (kid 56475aa75463474c)
```

Verifying *by id* resolves the token first: from the local cache when one is
there, otherwise from the control plane, which needs `a2a login` and ownership
of the agent. Verifying a token you already hold (next section) skips that step
entirely.

`PASS` exits `0`, `FAIL` exits `1`. The last line always names the key source,
because a signature check is only as meaningful as the key it used. Sources are
tried in this order:

1. `A2A_RECEIPT_VERIFYING_KEY` in the environment
2. `--key <base64>`
3. `A2A_RECEIPT_SIGNING_KEY` in the environment (its public half)
4. `GET {api}/v1/public/receipt-keys`

Everything above the last one needs no network, so this verifies fully offline:

```bash
a2a receipt verify --token @receipt.token --key "$(cat pub.txt)"
```

Verification is pure cryptography over bytes you already hold; it works for
receipts sealed years earlier, and asks the control plane for nothing but the
public key.

Related commands:

```bash
a2a receipt show 4b4d56883bb4d526          # decoded fields, grouped, plus PASS/FAIL
a2a receipt verify --token @receipt.token  # verify a raw token (@file, - for stdin)
a2a receipt verify <id> --agent my-agent   # fetch from the platform, then verify
a2a receipt list                           # receipts this CLI has seen
a2a receipt list my-agent                  # recent receipts from the platform
```

`a2a receipt show` prints the same field groups used above. Verified fully
offline here, against a local demo key:

```bash
a2a receipt show --token @receipt.token --key "$(cat pub.txt)"
```

```text
identity
  receipt       4b4d56883bb4d526
  agent         research-agent
  caller        user:42
  task          -

call
  skill         ask
  input hash    ad6b174eb01357d875c98f53aef35ca9c96437ed54de7974b507a5221563a435
  input         {"prompt": "say hello"}

authority
  grants        -

effects
  files         0 read / 0 written (0 B in, 0 B out)
  tools         -
  artifacts     -
  handoffs      -

outcome
  status        ok
  error         -
  result        hello
  eval score    -
  reviewer      -

timing
  started       2024-06-03T18:35:00Z
  ended         2024-06-03T18:35:03Z
  elapsed       3000ms

  signature PASS Ed25519 · key from --key
  source: --token
```

The same check without the CLI, using the SDK primitive the CLI itself calls:

```python
import base64
import hashlib
import json
import os
import urllib.request

from a2a_pack.receipts import ReceiptInvalid, verify_receipt

with urllib.request.urlopen("https://api.a2acloud.io/v1/public/receipt-keys") as resp:
    published = json.load(resp)

active = next(k for k in published["keys"] if k["kid"] == published["active_kid"])
assert hashlib.sha256(base64.b64decode(active["public_key"])).hexdigest()[:16] == active["kid"]

# verify_receipt reads the key from the environment at call time.
os.environ["A2A_RECEIPT_VERIFYING_KEY"] = active["public_key"]

token = open("receipt.token").read().strip()
try:
    receipt = verify_receipt(token)
except ReceiptInvalid as exc:
    raise SystemExit(f"receipt rejected: {exc}")

print(receipt.receipt_id, receipt.agent_name, receipt.skill_name, receipt.status, receipt.elapsed_ms)
```

```text
4b4d56883bb4d526 research-agent ask ok 3000
```

## Tamper demo

Take a receipt that says `"status": "ok"` and rewrite it to say `"status":
"error"`, keeping the original signature — the only thing an attacker who
lacks the private key can do:

```python
import base64

token = open("receipt.token").read().strip()
payload_b64, sig_b64 = token.rsplit(".", 1)
payload = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))

tampered = payload.replace(b'"status":"ok"', b'"status":"error"')
new_b64 = base64.urlsafe_b64encode(tampered).rstrip(b"=").decode()
open("tampered.token", "w").write(f"{new_b64}.{sig_b64}")
```

```bash
a2a receipt verify --token @tampered.token --key "$(cat pub.txt)"
echo "exit=$?"
```

```text
FAIL 4b4d56883bb4d526  receipt signature mismatch
  not signed by the key from --key — do not trust it
exit=1
```

The payload is still perfectly valid JSON and still parses into a receipt. It
just is not the payload that was signed, and there is no way to make it one
without the private key.

Underneath, `verify_receipt` raises `ReceiptInvalid` — a `ValueError` subclass —
for every failure mode, and the message tells you which one:

| Message | Cause |
| --- | --- |
| `malformed receipt token` | Empty token, or no `.` separator. |
| `receipt decode failed: …` | A segment is not canonical unpadded base64url. |
| `receipt signature mismatch` | The signature does not cover these payload bytes, or it was made by a different key. |
| `receipt payload invalid: …` | Signature is good but the JSON does not match the `ExecutionReceipt` schema. |

There is no partial pass. A receipt either verifies exactly as sealed, or it
does not verify at all.

## What a receipt does not prove

Being precise here matters more than sounding impressive.

- **It attests what the runtime observed, not ground truth.** A receipt records
  what the sealing observer saw. If the observer was wrong or incomplete, the
  receipt faithfully records something wrong or incomplete.
- **It is not third-party notarization.** The signing key belongs to the
  platform, which is a party to the transaction — not a neutral notary. A valid
  signature proves the record came from that platform key and has not been
  altered since. It does not prove the platform is disinterested.
- **It is not the full inputs or outputs.** Hashes and 240-character previews
  only. You cannot reconstruct a run from a receipt; you can only check that a
  run you already know about matches.
- **It does not prove the answer was correct.** `status: "ok"` means the call
  completed without raising, not that the output is right. Quality signals live
  in `eval_score` / `reviewer`, and only when an evaluator actually ran.
- **It does not prove absence.** Receipts you hold say nothing about calls whose
  receipts you were never given. Completeness is a property of the ledger, not
  of any one receipt.
- **It carries no freshness or revocation semantics.** `verify_receipt`
  deliberately does not check expiry — receipts are historical artifacts meant
  to verify long after the run. It also has no revocation list.
- **There is no key rotation support yet.** `/v1/public/receipt-keys` derives
  exactly one key from the configured signer and always returns a
  single-element `keys` array; the CLI picks `active_kid` (else the first entry)
  and never tries the others. The array is shaped for multiple keys, but nothing
  populates or consumes more than one. Rotating the signing key therefore breaks
  verification of every previously sealed receipt via the published key — keep
  the retired public key yourself and pass it with `--key` to check old
  receipts.
- **Retention, access review, and policy are separate systems.** How long
  receipts are kept, who may read them, and what policy governed the call are
  answered by the control plane's compliance and governance surfaces — not by
  the signature. See [Operate the runtime](/platform/runtime).
- **A receipt is evidence, not an accounting record.** The signed payload
  carries **no economics at all** — no price, no cost, no currency;
  `ExecutionReceipt` is `extra="forbid"` and the field tables above are the
  complete list. Usage metrics are stored separately on the control plane and
  are never part of the signature.

## Related

- [Grants](/concepts/grants) — the scoped, signed authority a run carries.
- [Operate the runtime](/platform/runtime) — receipts, proofs, and replay in the dashboard.
- [`a2a receipt`](/reference/cli#a2a-receipt) — generated CLI reference for `show`, `verify`, and `list`.
- [Control-plane API](/reference/control-plane-api) — the generated route inventory.
