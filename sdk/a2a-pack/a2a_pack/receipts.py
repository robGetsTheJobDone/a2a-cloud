"""Signed execution receipts.

Every skill invocation produces an :class:`ExecutionReceipt` — a small,
self-contained, signed record describing *what* ran, *who* called it, *which*
authority it carried, *what* it touched, and *what* it produced. The runtime
emits one per invocation; the platform stores and serves them.

Deterministic replay
--------------------

Receipts pair with :mod:`a2a_pack.replay` sessions. A receipt records the
*outcome* of a run; a session records the *event log*. To re-execute a run
deterministically, skill code must use :meth:`RunContext.random` (seeded from
``ctx.random_seed``) for any randomness it would like the replay to reproduce.
Random draws made via :mod:`random` / :mod:`secrets` directly are *not*
reproducible across replays.

Wire format::

    "<base64url(json(payload))>.<base64url(signature)>"

Signing mirrors :mod:`a2a_pack.grants`: Ed25519 only, using
``A2A_RECEIPT_SIGNING_KEY`` for sealers and ``A2A_RECEIPT_VERIFYING_KEY`` for
downstream verifiers.

This module ships the schema + sign/verify primitives. Storage (object store,
control-plane DB) and retrieval endpoints are the responsibility of the server
adapter / control plane — receipts are designed to be inspectable on the wire
without a database round-trip.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, NonNegativeInt


class ReceiptInvalid(ValueError):
    """Raised by :func:`verify_receipt` when a receipt is bad/forged/malformed."""


class FileOps(BaseModel):
    """Aggregate file-system activity during a run.

    Counts are authoritative; previews are best-effort (truncated to keep
    receipts small enough to inline in API responses).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reads: NonNegativeInt = 0
    writes: NonNegativeInt = 0
    bytes_read: NonNegativeInt = 0
    bytes_written: NonNegativeInt = 0
    read_paths_preview: tuple[str, ...] = ()
    write_paths_preview: tuple[str, ...] = ()


class ToolCall(BaseModel):
    """One tool / MCP call inside a run. Stored by reference, not by payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    args_hash: str = ""
    status: str = "ok"
    elapsed_ms: NonNegativeInt = 0


class ArtifactRef(BaseModel):
    """Pointer to a workspace artifact the skill produced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    mime_type: str = ""
    bytes: NonNegativeInt = 0


class Handoff(BaseModel):
    """One agent-to-agent handoff captured in the trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    callee: str
    skill: str
    grant_id: str = ""
    elapsed_ms: NonNegativeInt = 0
    status: str = "ok"


class ExecutionReceipt(BaseModel):
    """The payload of a signed execution receipt.

    Every field is plain JSON-serializable; the receipt is canonicalized via
    :meth:`BaseModel.model_dump_json` (stable key order from the field
    declaration above) before signing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str
    schema_version: int = 1

    # Identity
    agent_name: str
    agent_version: str = ""
    caller: str = ""  # caller agent name, user id, or empty
    task_id: str = ""

    # What ran
    skill_name: str
    input_hash: str = ""
    input_preview: str = ""

    # Authority used
    grant_ids: tuple[str, ...] = ()

    # What it touched / produced
    file_ops: FileOps = Field(default_factory=FileOps)
    tool_calls: tuple[ToolCall, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    handoffs: tuple[Handoff, ...] = ()

    # Outcome
    status: str = "ok"  # "ok" | "error" | "cancelled" | "partial"
    error_type: str = ""
    result_preview: str = ""

    # Evaluation
    eval_score: NonNegativeFloat | None = None
    reviewer: str = ""

    # Timing (unix seconds for stamps, milliseconds for duration)
    started_at: NonNegativeInt = 0
    ended_at: NonNegativeInt = 0
    elapsed_ms: NonNegativeInt = 0

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


def _receipt_signing_key_env() -> str:
    return os.environ.get("A2A_RECEIPT_SIGNING_KEY", "").strip()


def _receipt_verifying_key_env() -> str:
    return os.environ.get("A2A_RECEIPT_VERIFYING_KEY", "").strip()


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
    verifying_key = _receipt_verifying_key_env()
    if verifying_key:
        return _ed25519_public_key(verifying_key)
    signing_key = _receipt_signing_key_env()
    if signing_key:
        return _ed25519_private_key(signing_key).public_key()
    return None


def receipt_public_key_b64() -> str | None:
    """Base64 of the raw 32-byte Ed25519 receipt *public* key, if configured.

    Prefers ``A2A_RECEIPT_VERIFYING_KEY`` and falls back to the public half of
    ``A2A_RECEIPT_SIGNING_KEY``. Returns ``None`` when neither is set. Private
    key material is never derivable from the returned value — this is what the
    platform publishes so third parties can verify receipts offline.
    """
    verifying_key = _ed25519_verifying_key()
    if verifying_key is None:
        return None
    return base64.b64encode(verifying_key.public_bytes_raw()).decode("ascii")


def _required_signing_key() -> Any:
    signing_key = _receipt_signing_key_env()
    if not signing_key:
        raise RuntimeError("A2A_RECEIPT_SIGNING_KEY is required")
    return _ed25519_private_key(signing_key)


def _required_verifying_key() -> Any:
    verifying_key = _ed25519_verifying_key()
    if verifying_key is None:
        raise ReceiptInvalid("A2A_RECEIPT_VERIFYING_KEY is required")
    return verifying_key


def hash_input(payload: Any) -> str:
    """Stable hex sha256 of ``payload`` after JSON canonicalization.

    Used for the ``input_hash`` field so two receipts for the same call have
    matching hashes regardless of dict ordering.
    """
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _preview(value: Any, limit: int = 240) -> str:
    """Truncate ``value`` to a short, JSON-string preview for inline display."""
    try:
        text = (
            value if isinstance(value, str) else json.dumps(value, default=str)
        )
    except Exception:  # noqa: BLE001
        text = repr(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def seal_receipt(
    *,
    agent_name: str,
    skill_name: str,
    started_at: int,
    ended_at: int | None = None,
    agent_version: str = "",
    caller: str = "",
    task_id: str = "",
    inputs: Any = None,
    result: Any = None,
    status: str = "ok",
    error_type: str = "",
    grant_ids: tuple[str, ...] = (),
    file_ops: FileOps | None = None,
    tool_calls: tuple[ToolCall, ...] = (),
    artifacts: tuple[ArtifactRef, ...] = (),
    handoffs: tuple[Handoff, ...] = (),
    eval_score: float | None = None,
    reviewer: str = "",
) -> tuple[ExecutionReceipt, str]:
    """Build an :class:`ExecutionReceipt` and return it with its signed token.

    The call site supplies what it observed (``inputs``, ``result``, file ops,
    handoffs); this function fills in the hash, preview, timing math, and
    signature. Receipts are immutable once sealed.
    """
    ended = ended_at if ended_at is not None else int(time.time())
    elapsed_ms = max(0, (ended - started_at) * 1000)
    input_hash = hash_input(inputs) if inputs is not None else ""
    receipt = ExecutionReceipt(
        receipt_id=secrets.token_hex(8),
        agent_name=agent_name,
        agent_version=agent_version,
        caller=caller,
        task_id=task_id,
        skill_name=skill_name,
        input_hash=input_hash,
        input_preview=_preview(inputs) if inputs is not None else "",
        grant_ids=tuple(grant_ids),
        file_ops=file_ops or FileOps(),
        tool_calls=tuple(tool_calls),
        artifacts=tuple(artifacts),
        handoffs=tuple(handoffs),
        status=status,
        error_type=error_type,
        result_preview=_preview(result) if result is not None else "",
        eval_score=eval_score,
        reviewer=reviewer,
        started_at=started_at,
        ended_at=ended,
        elapsed_ms=elapsed_ms,
    )
    return receipt, sign_receipt(receipt)


def sign_receipt(receipt: ExecutionReceipt) -> str:
    """Serialize + sign ``receipt`` into the wire token."""
    payload = receipt.model_dump_json(exclude_none=False).encode("utf-8")
    sig = _required_signing_key().sign(payload)
    return f"{_b64encode(payload)}.{_b64encode(sig)}"


def verify_receipt(token: str) -> ExecutionReceipt:
    """Parse + verify ``token``. Raises :class:`ReceiptInvalid` on any failure.

    Checks signature + structural shape. Does *not* check freshness — receipts
    are historical artifacts, expected to verify long after the run.
    """
    if not token or "." not in token:
        raise ReceiptInvalid("malformed receipt token")
    payload_b64, sig_b64 = token.rsplit(".", 1)
    try:
        payload = _b64decode(payload_b64)
        sig = _b64decode(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise ReceiptInvalid(f"receipt decode failed: {exc}") from exc

    verifying_key = _required_verifying_key()
    try:
        verifying_key.verify(sig, payload)
    except Exception as exc:  # noqa: BLE001
        raise ReceiptInvalid("receipt signature mismatch") from exc

    try:
        data = json.loads(payload)
        return ExecutionReceipt.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        raise ReceiptInvalid(f"receipt payload invalid: {exc}") from exc


__all__ = [
    "ArtifactRef",
    "ExecutionReceipt",
    "FileOps",
    "Handoff",
    "ReceiptInvalid",
    "ToolCall",
    "hash_input",
    "receipt_public_key_b64",
    "seal_receipt",
    "sign_receipt",
    "verify_receipt",
]
