# `a2a_pack.grants`

Signed grant tokens for cross-agent workspace handoff.

A grant is a small, self-contained, signed claim issued by one agent that
the platform (or the receiving agent) can verify without a registry round-trip.

Wire format::

    "<base64url(json(payload))>.<base64url(signature)>"

The payload describes *what* the callee is allowed to do, *whose* workspace
they can see, and *for how long*. The runtime on the receiving side
materializes a :class:`WorkspaceClient` scoped to that grant.

Production deployments set ``A2A_GRANT_SIGNING_KEY`` only on trusted
platform components and ``A2A_GRANT_VERIFYING_KEY`` on agents that need to
verify grants. Grants are Ed25519-only.

## `Grant`  *(class)*

```python
Grant(*, grant_id: str, issuer: str, audience: str, bucket: str, mode: a2a_pack.workspace.WorkspaceMode = <WorkspaceMode.READ_ONLY: 'read_only'>, allow_patterns: tuple[str, ...] = ('**',), deny_patterns: tuple[str, ...] = (), outputs_prefix: str | None = None, write_prefixes: tuple[str, ...] = (), llm_models: tuple[str, ...] = (), llm_max_budget_usd: Optional[Annotated[float, Ge(ge=0)]] = None, llm_rpm_limit: Optional[Annotated[int, Gt(gt=0)]] = None, llm_tpm_limit: Optional[Annotated[int, Gt(gt=0)]] = None, source_grants: tuple[a2a_pack.grants.SourceGrant, ...] = (), parent_grant_id: str | None = None, delegation_depth: Annotated[int, Ge(ge=0)] = 0, max_delegation_depth: Annotated[int, Gt(gt=0)] = 40, expires_at: Annotated[int, Ge(ge=0)] = 0, issued_at: Annotated[int, Ge(ge=0)] = 0, nonce: str = <factory>) -> None
```

The payload of a signed grant token.

A grant binds *who* (issuer) gave *whom* (audience) access to *which*
workspace files (bucket + allow/deny patterns) under *what* mode and
*how long*. The runtime enforces every line of this payload.

## `GrantDelegationDenied`  *(class)*



Raised when a child grant would exceed its parent grant.

## `GrantInvalid`  *(class)*



Raised by :func:`verify_grant` when a grant is bad/expired/forged.

## `SourceGrant`  *(class)*

```python
SourceGrant(*, agent: str, scope: Literal['read', 'write'] = 'read') -> None
```

Source repository access carried by a workspace grant.

## `delegate_grant`  *(function)*

```python
delegate_grant(parent: 'Grant', *, issuer: 'str', audience: 'str', bucket: 'str | None' = None, mode: 'WorkspaceMode' = <WorkspaceMode.READ_ONLY: 'read_only'>, allow_patterns: 'tuple[str, ...]' = ('**',), deny_patterns: 'tuple[str, ...]' = (), outputs_prefix: 'str | None' = None, write_prefixes: 'tuple[str, ...]' = (), llm_models: 'tuple[str, ...]' = (), llm_max_budget_usd: 'float | None' = None, llm_rpm_limit: 'int | None' = None, llm_tpm_limit: 'int | None' = None, source_grants: 'tuple[SourceGrant | dict[str, Any], ...]' = (), ttl_seconds: 'int' = 300) -> 'tuple[Grant, str]'
```

Mint a child grant that is mechanically bounded by ``parent``.

## `mint_grant`  *(function)*

```python
mint_grant(*, issuer: 'str', audience: 'str', bucket: 'str', mode: 'WorkspaceMode' = <WorkspaceMode.READ_ONLY: 'read_only'>, allow_patterns: 'tuple[str, ...]' = ('**',), deny_patterns: 'tuple[str, ...]' = (), outputs_prefix: 'str | None' = None, write_prefixes: 'tuple[str, ...]' = (), llm_models: 'tuple[str, ...]' = (), llm_max_budget_usd: 'float | None' = None, llm_rpm_limit: 'int | None' = None, llm_tpm_limit: 'int | None' = None, source_grants: 'tuple[SourceGrant | dict[str, Any], ...]' = (), parent_grant_id: 'str | None' = None, delegation_depth: 'int' = 0, max_delegation_depth: 'int' = 40, ttl_seconds: 'int' = 300) -> 'tuple[Grant, str]'
```

Build a :class:`Grant` and return it together with its signed token.

## `sign_grant`  *(function)*

```python
sign_grant(grant: 'Grant') -> 'str'
```

*(no docstring)*

## `verify_grant`  *(function)*

```python
verify_grant(token: 'str') -> 'Grant'
```

Parse + verify ``token``. Raises :class:`GrantInvalid` on any failure.

Checks signature, expiry, and minimal structural shape. Caller-specific
audience checks are layered on top by the server adapter.


*Source: `sdk/a2a-pack/a2a_pack/grants.py`*