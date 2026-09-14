# Grants — scoped, signed, time-bound

Every cross-agent call carries a **grant token** — an Ed25519-signed blob
that names *who* called, *whom*, against *which bucket*, with *what
read/write patterns*, at *what mode*, for *how long*.

## Wire format

```
<base64url(json(payload))>.<base64url(ed25519_signature(payload))>
```

Payload shape:

```json
{
  "grant_id": "abc12345",
  "issuer": "main-agent:user-7",
  "audience": "graph-agent",
  "bucket": "user-7-files",
  "mode": "read_write_overlay",
  "allow_patterns": ["**"],
  "deny_patterns": [],
  "outputs_prefix": "outputs/",
  "write_prefixes": ["outputs/", "reports/"],
  "expires_at": 1717440000,
  "issued_at": 1717439700,
  "nonce": "e7f9..."
}
```

Receiving agents call [`verify_grant`](/reference/grants) — bad
signature, missing audience, or expired TTL → 403.

## Runtime scope negotiation

Tools that opt in via `@a2a.tool(allow_scope_expansion=True)` can call
`await ctx.request_scope(...)`, `await ctx.ensure_read(...)`, or
`await ctx.ensure_write(...)` mid-execution. The orchestrator runs
`decide_extension` — hard ceilings always
deny (cross-bucket); risk-0 read-only short-TTL auto-approves in
auto-mode; any mode upgrade or new `write_prefixes` entry always asks
the user.

`outputs_prefix` remains the legacy primary write location. New code
should read `write_prefixes`; runtimes derive `write_prefixes` from
`outputs_prefix` for older grants.

Sandbox rootfs capture writes to the reserved
`outputs/rootfs-captures/**` prefix through a backend-owned grant path,
so diagnostics keep working without expanding the caller grant.

The flow surfaces on the dashboard as a `ScopeCard` with reason +
diff (old vs requested patterns) + approve/deny buttons.

## Audit trail

Every minted grant and every extension writes a row to `GrantAudit`,
linked via `parent_grant_id`. Query the chain:

```
GET /v1/me/grants/{grant_id}
```

returns the row and every transitive extension in one shot.

## Reference

- [`a2a_pack.grants`](/reference/grants) — SDK mint + verify
- [`a2a_pack.workspace`](/reference/workspace) — `WorkspaceClient`, `install_grant`
- Concepts: scope negotiation *(coming)*
