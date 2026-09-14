"""Signed grant tokens for cross-agent workspace handoff.

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
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import time
from fnmatch import fnmatch
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
)

from .workspace import WorkspaceMode, _matches_write_prefix, _normalize_write_prefixes

DEFAULT_TTL_SECONDS = 5 * 60
DEFAULT_MAX_DELEGATION_DEPTH = 40


_MODE_RANK = {
    WorkspaceMode.READ_ONLY: 0,
    WorkspaceMode.READ_WRITE_OVERLAY: 1,
    WorkspaceMode.READ_WRITE_DIRECT: 2,
}


class GrantInvalid(PermissionError):
    """Raised by :func:`verify_grant` when a grant is bad/expired/forged."""


class GrantDelegationDenied(PermissionError):
    """Raised when a child grant would exceed its parent grant."""


class SourceGrant(BaseModel):
    """Source repository access carried by a workspace grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: str
    scope: Literal["read", "write"] = "read"


class Grant(BaseModel):
    """The payload of a signed grant token.

    A grant binds *who* (issuer) gave *whom* (audience) access to *which*
    workspace files (bucket + allow/deny patterns) under *what* mode and
    *how long*. The runtime enforces every line of this payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    issuer: str  # caller agent name or URL
    audience: str  # callee agent name or URL
    bucket: str  # workspace bucket the grant covers
    mode: WorkspaceMode = WorkspaceMode.READ_ONLY
    allow_patterns: tuple[str, ...] = ("**",)
    deny_patterns: tuple[str, ...] = ()
    outputs_prefix: str | None = None  # if set, callee writes only here
    write_prefixes: tuple[str, ...] = ()
    llm_models: tuple[str, ...] = ()
    llm_max_budget_usd: NonNegativeFloat | None = None
    llm_rpm_limit: PositiveInt | None = None
    llm_tpm_limit: PositiveInt | None = None
    source_grants: tuple[SourceGrant, ...] = ()
    parent_grant_id: str | None = None
    delegation_depth: NonNegativeInt = 0
    max_delegation_depth: PositiveInt = DEFAULT_MAX_DELEGATION_DEPTH
    expires_at: NonNegativeInt = 0
    issued_at: NonNegativeInt = 0
    nonce: str = Field(default_factory=lambda: secrets.token_hex(8))


def _b64encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    clean = s.strip()
    pad = "=" * (-len(clean) % 4)
    decoded = base64.urlsafe_b64decode(clean + pad)
    if _b64encode(decoded) != clean.rstrip("="):
        raise ValueError("non-canonical base64url encoding")
    return decoded


def _grant_signing_key_env() -> str:
    return os.environ.get("A2A_GRANT_SIGNING_KEY", "").strip()


def _grant_verifying_key_env() -> str:
    return os.environ.get("A2A_GRANT_VERIFYING_KEY", "").strip()


def _decode_key_material(value: str) -> bytes:
    clean = value.strip()
    if clean.startswith("base64:"):
        clean = clean[len("base64:") :].strip()
    try:
        return base64.b64decode(clean, validate=True)
    except binascii.Error:
        pad = "=" * (-len(clean) % 4)
        return base64.urlsafe_b64decode(clean + pad)


def _ed25519_private_key(value: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if "BEGIN" in value:
        return serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    return Ed25519PrivateKey.from_private_bytes(_decode_key_material(value))


def _ed25519_public_key(value: str) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if "BEGIN" in value:
        return serialization.load_pem_public_key(value.encode("utf-8"))
    return Ed25519PublicKey.from_public_bytes(_decode_key_material(value))


def _ed25519_verifying_key() -> Any | None:
    verifying_key = _grant_verifying_key_env()
    if verifying_key:
        return _ed25519_public_key(verifying_key)
    signing_key = _grant_signing_key_env()
    if signing_key:
        return _ed25519_private_key(signing_key).public_key()
    return None


def _required_signing_key() -> Any:
    signing_key = _grant_signing_key_env()
    if not signing_key:
        raise RuntimeError("A2A_GRANT_SIGNING_KEY is required")
    return _ed25519_private_key(signing_key)


def _required_verifying_key() -> Any:
    verifying_key = _ed25519_verifying_key()
    if verifying_key is None:
        raise GrantInvalid("A2A_GRANT_VERIFYING_KEY is required")
    return verifying_key


def mint_grant(
    *,
    issuer: str,
    audience: str,
    bucket: str,
    mode: WorkspaceMode = WorkspaceMode.READ_ONLY,
    allow_patterns: tuple[str, ...] = ("**",),
    deny_patterns: tuple[str, ...] = (),
    outputs_prefix: str | None = None,
    write_prefixes: tuple[str, ...] = (),
    llm_models: tuple[str, ...] = (),
    llm_max_budget_usd: float | None = None,
    llm_rpm_limit: int | None = None,
    llm_tpm_limit: int | None = None,
    source_grants: tuple[SourceGrant | dict[str, Any], ...] = (),
    parent_grant_id: str | None = None,
    delegation_depth: int = 0,
    max_delegation_depth: int = DEFAULT_MAX_DELEGATION_DEPTH,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[Grant, str]:
    """Build a :class:`Grant` and return it together with its signed token."""
    now = int(time.time())
    grant = Grant(
        grant_id=secrets.token_hex(8),
        issuer=issuer,
        audience=audience,
        bucket=bucket,
        mode=mode,
        allow_patterns=tuple(allow_patterns),
        deny_patterns=tuple(deny_patterns),
        outputs_prefix=outputs_prefix,
        write_prefixes=_normalize_write_prefixes(outputs_prefix, write_prefixes),
        llm_models=tuple(llm_models),
        llm_max_budget_usd=llm_max_budget_usd,
        llm_rpm_limit=llm_rpm_limit,
        llm_tpm_limit=llm_tpm_limit,
        source_grants=tuple(
            item if isinstance(item, SourceGrant) else SourceGrant.model_validate(item)
            for item in source_grants
        ),
        parent_grant_id=parent_grant_id,
        delegation_depth=delegation_depth,
        max_delegation_depth=max_delegation_depth,
        expires_at=now + ttl_seconds,
        issued_at=now,
    )
    return grant, sign_grant(grant)


def delegate_grant(
    parent: Grant,
    *,
    issuer: str,
    audience: str,
    bucket: str | None = None,
    mode: WorkspaceMode = WorkspaceMode.READ_ONLY,
    allow_patterns: tuple[str, ...] = ("**",),
    deny_patterns: tuple[str, ...] = (),
    outputs_prefix: str | None = None,
    write_prefixes: tuple[str, ...] = (),
    llm_models: tuple[str, ...] = (),
    llm_max_budget_usd: float | None = None,
    llm_rpm_limit: int | None = None,
    llm_tpm_limit: int | None = None,
    source_grants: tuple[SourceGrant | dict[str, Any], ...] = (),
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[Grant, str]:
    """Mint a child grant that is mechanically bounded by ``parent``."""

    child_bucket = bucket or parent.bucket
    if child_bucket != parent.bucket:
        raise GrantDelegationDenied("child grant bucket must match parent grant")
    if parent.delegation_depth >= parent.max_delegation_depth:
        raise GrantDelegationDenied("maximum grant delegation depth reached")
    if _MODE_RANK[mode] > _MODE_RANK[parent.mode]:
        raise GrantDelegationDenied(
            f"child grant mode {mode.value!r} exceeds parent mode {parent.mode.value!r}"
        )

    child_allow = tuple(allow_patterns or ("**",))
    for pattern in child_allow:
        if not _pattern_covered(pattern, parent.allow_patterns):
            raise GrantDelegationDenied(
                f"child read pattern {pattern!r} is outside parent grant"
            )

    child_write_prefixes = _normalize_write_prefixes(outputs_prefix, write_prefixes)
    _assert_write_scope_within_parent(parent, child_write_prefixes)
    _assert_llm_scope_within_parent(
        parent,
        llm_models=llm_models,
        llm_max_budget_usd=llm_max_budget_usd,
        llm_rpm_limit=llm_rpm_limit,
        llm_tpm_limit=llm_tpm_limit,
    )
    child_source_grants = tuple(
        item if isinstance(item, SourceGrant) else SourceGrant.model_validate(item)
        for item in source_grants
    )
    _assert_source_scope_within_parent(parent, child_source_grants)

    ttl = _bounded_ttl(parent, ttl_seconds)
    child_deny = _merge_patterns(parent.deny_patterns, deny_patterns)
    return mint_grant(
        issuer=issuer,
        audience=audience,
        bucket=child_bucket,
        mode=mode,
        allow_patterns=child_allow,
        deny_patterns=child_deny,
        outputs_prefix=outputs_prefix,
        write_prefixes=child_write_prefixes,
        llm_models=tuple(llm_models),
        llm_max_budget_usd=llm_max_budget_usd,
        llm_rpm_limit=llm_rpm_limit,
        llm_tpm_limit=llm_tpm_limit,
        source_grants=child_source_grants,
        parent_grant_id=parent.grant_id,
        delegation_depth=parent.delegation_depth + 1,
        max_delegation_depth=parent.max_delegation_depth,
        ttl_seconds=ttl,
    )


def sign_grant(grant: Grant) -> str:
    normalized_write_prefixes = _normalize_write_prefixes(
        grant.outputs_prefix,
        grant.write_prefixes,
    )
    if grant.write_prefixes != normalized_write_prefixes:
        grant = grant.model_copy(update={"write_prefixes": normalized_write_prefixes})
    payload = json.dumps(
        grant.model_dump(mode="json"),
        separators=(",", ":"),
    ).encode("utf-8")
    sig = _required_signing_key().sign(payload)
    return f"{_b64encode(payload)}.{_b64encode(sig)}"


def verify_grant(token: str) -> Grant:
    """Parse + verify ``token``. Raises :class:`GrantInvalid` on any failure.

    Checks signature, expiry, and minimal structural shape. Caller-specific
    audience checks are layered on top by the server adapter.
    """
    if not token or "." not in token:
        raise GrantInvalid("malformed grant token")
    payload_b64, sig_b64 = token.rsplit(".", 1)
    try:
        payload = _b64decode(payload_b64)
        sig = _b64decode(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise GrantInvalid(f"grant decode failed: {exc}") from exc

    verifying_key = _required_verifying_key()
    try:
        verifying_key.verify(sig, payload)
    except Exception as exc:  # noqa: BLE001
        raise GrantInvalid("grant signature mismatch") from exc

    try:
        data = json.loads(payload)
        grant = Grant.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise GrantInvalid(f"grant payload invalid: {exc}") from exc

    if not grant.write_prefixes and grant.outputs_prefix:
        grant = grant.model_copy(
            update={
                "write_prefixes": _normalize_write_prefixes(grant.outputs_prefix)
            }
        )

    if grant.expires_at and grant.expires_at < int(time.time()):
        raise GrantInvalid(f"grant expired at {grant.expires_at}")
    return grant


def _pattern_covered(pattern: str, parent_patterns: tuple[str, ...]) -> bool:
    return any(
        parent == "**" or parent == pattern or fnmatch(pattern, parent)
        for parent in parent_patterns
    )


def _merge_patterns(
    inherited: tuple[str, ...],
    requested: tuple[str, ...],
) -> tuple[str, ...]:
    out: list[str] = []
    for pattern in (*inherited, *requested):
        if pattern and pattern not in out:
            out.append(pattern)
    return tuple(out)


def _assert_write_scope_within_parent(
    parent: Grant,
    child_write_prefixes: tuple[str, ...],
) -> None:
    if not child_write_prefixes:
        return
    if parent.mode is WorkspaceMode.READ_ONLY:
        raise GrantDelegationDenied("read-only parent grant cannot delegate writes")
    parent_write_prefixes = _normalize_write_prefixes(
        parent.outputs_prefix,
        parent.write_prefixes,
    )
    if parent_write_prefixes:
        for prefix in child_write_prefixes:
            if not _matches_write_prefix(prefix, parent_write_prefixes):
                raise GrantDelegationDenied(
                    f"child write prefix {prefix!r} is outside parent grant"
                )
        return
    for prefix in child_write_prefixes:
        probe = f"{prefix.rstrip('/')}/__a2a_probe__"
        if not _pattern_covered(probe, parent.allow_patterns):
            raise GrantDelegationDenied(
                f"child write prefix {prefix!r} is outside parent grant"
            )


def _assert_llm_scope_within_parent(
    parent: Grant,
    *,
    llm_models: tuple[str, ...],
    llm_max_budget_usd: float | None,
    llm_rpm_limit: int | None,
    llm_tpm_limit: int | None,
) -> None:
    if llm_models:
        if not parent.llm_models:
            raise GrantDelegationDenied("parent grant has no LLM model allowance")
        missing = sorted(set(llm_models) - set(parent.llm_models))
        if missing:
            raise GrantDelegationDenied(
                f"child LLM models outside parent grant: {missing}"
            )
    if llm_max_budget_usd is not None:
        if parent.llm_max_budget_usd is None:
            raise GrantDelegationDenied("parent grant has no LLM budget allowance")
        if llm_max_budget_usd > parent.llm_max_budget_usd:
            raise GrantDelegationDenied("child LLM budget exceeds parent grant")
    if llm_rpm_limit is not None:
        if parent.llm_rpm_limit is None:
            raise GrantDelegationDenied("parent grant has no LLM RPM allowance")
        if llm_rpm_limit > parent.llm_rpm_limit:
            raise GrantDelegationDenied("child LLM RPM exceeds parent grant")
    if llm_tpm_limit is not None:
        if parent.llm_tpm_limit is None:
            raise GrantDelegationDenied("parent grant has no LLM TPM allowance")
        if llm_tpm_limit > parent.llm_tpm_limit:
            raise GrantDelegationDenied("child LLM TPM exceeds parent grant")


def _assert_source_scope_within_parent(
    parent: Grant,
    child_source_grants: tuple[SourceGrant, ...],
) -> None:
    if not child_source_grants:
        return
    parent_scopes = {item.agent: item.scope for item in parent.source_grants}
    for child in child_source_grants:
        parent_scope = parent_scopes.get(child.agent)
        if parent_scope is None:
            raise GrantDelegationDenied(
                f"child source grant {child.agent!r} is outside parent grant"
            )
        if parent_scope == "read" and child.scope == "write":
            raise GrantDelegationDenied(
                f"child source grant {child.agent!r} exceeds parent source scope"
            )


def _bounded_ttl(parent: Grant, requested_ttl_seconds: int) -> int:
    if not parent.expires_at:
        return requested_ttl_seconds
    remaining = parent.expires_at - int(time.time())
    if remaining <= 0:
        raise GrantDelegationDenied("parent grant is expired")
    return max(0, min(requested_ttl_seconds, remaining))


__all__ = [
    "Grant",
    "GrantDelegationDenied",
    "GrantInvalid",
    "SourceGrant",
    "delegate_grant",
    "mint_grant",
    "sign_grant",
    "verify_grant",
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_MAX_DELEGATION_DEPTH",
]
