"""Grant minting + verification, wire-compatible with the control plane.

Same payload shape as ``control_plane/grants.py`` and ``a2a_pack/grants.py``
so a grant minted here is accepted by deployed A2A agents. Production grants
use Ed25519 signing with ``A2A_GRANT_SIGNING_KEY``.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GrantClaims:
    issuer: str
    audience: str
    bucket: str
    mode: str = "read_only"
    allow_patterns: tuple[str, ...] = ("**",)
    deny_patterns: tuple[str, ...] = ()
    outputs_prefix: str | None = None
    write_prefixes: tuple[str, ...] = ()
    llm_models: tuple[str, ...] = ()
    llm_max_budget_usd: float | None = None
    llm_rpm_limit: int | None = None
    llm_tpm_limit: int | None = None
    source_grants: tuple[dict[str, Any], ...] = ()
    ttl_seconds: int = 300


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


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


def normalize_write_prefixes(
    outputs_prefix: str | None = None,
    write_prefixes: tuple[str, ...] = (),
) -> tuple[str, ...]:
    prefixes: list[str] = []
    values: list[str] = []
    if outputs_prefix:
        values.append(outputs_prefix)
    values.extend(write_prefixes)
    for value in values:
        clean = str(value).replace("\\", "/").strip("/")
        if not clean:
            continue
        normalized = clean + "/"
        if normalized not in prefixes:
            prefixes.append(normalized)
    return tuple(prefixes)


def normalize_source_grants(
    source_grants: tuple[dict[str, Any], ...] = (),
) -> tuple[dict[str, str], ...]:
    grants: list[dict[str, str]] = []
    for item in source_grants:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent") or item.get("repo") or item.get("name") or "").strip()
        scope = str(item.get("scope") or "read").strip().lower()
        if not agent or scope not in {"read", "write"}:
            continue
        grant = {"agent": agent, "scope": scope}
        if grant not in grants:
            grants.append(grant)
    return tuple(grants)


def grant_signing_key_configured() -> bool:
    """Return whether grants will be signed with the Ed25519 platform key."""
    return bool(_grant_signing_key_env())


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


def mint_grant_token(claims: GrantClaims) -> tuple[str, dict[str, Any]]:
    """Build a signed grant token. Returns ``(token, payload_dict)``."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "grant_id": secrets.token_hex(8),
        "issuer": claims.issuer,
        "audience": claims.audience,
        "bucket": claims.bucket,
        "mode": claims.mode,
        "allow_patterns": list(claims.allow_patterns),
        "deny_patterns": list(claims.deny_patterns),
        "outputs_prefix": claims.outputs_prefix,
        "write_prefixes": list(
            normalize_write_prefixes(claims.outputs_prefix, claims.write_prefixes)
        ),
        "llm_models": list(claims.llm_models),
        "llm_max_budget_usd": claims.llm_max_budget_usd,
        "llm_rpm_limit": claims.llm_rpm_limit,
        "llm_tpm_limit": claims.llm_tpm_limit,
        "expires_at": now + claims.ttl_seconds,
        "issued_at": now,
        "nonce": secrets.token_hex(8),
    }
    payload["source_grants"] = [
        dict(item) for item in normalize_source_grants(claims.source_grants)
    ]
    body = json.dumps(payload).encode("utf-8")
    sig = _required_signing_key().sign(body)
    return f"{_b64(body)}.{_b64(sig)}", payload


class GrantInvalid(PermissionError):
    """Raised by :func:`verify_grant` when a grant is bad/expired/forged."""


def verify_grant(token: str) -> dict[str, Any]:
    """Verify + decode a grant. Raises :class:`GrantInvalid` on any failure."""
    if not token or "." not in token:
        raise GrantInvalid("malformed grant token")
    payload_b64, sig_b64 = token.rsplit(".", 1)
    try:
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise GrantInvalid(f"grant decode failed: {exc}") from exc
    verifying_key = _required_verifying_key()
    try:
        verifying_key.verify(sig, payload)
    except Exception as exc:  # noqa: BLE001
        raise GrantInvalid("grant signature mismatch") from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise GrantInvalid(f"grant payload invalid: {exc}") from exc
    if data.get("expires_at") and data["expires_at"] < int(time.time()):
        raise GrantInvalid(f"grant expired at {data['expires_at']}")
    if not data.get("write_prefixes") and data.get("outputs_prefix"):
        data["write_prefixes"] = list(normalize_write_prefixes(data.get("outputs_prefix")))
    return data
