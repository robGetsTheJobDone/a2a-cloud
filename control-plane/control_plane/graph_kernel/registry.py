"""Safe protocol-pack registry.

Registry entries are descriptive and simulation-only. They are not authority,
do not enable live apply, and cannot override kernel policy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ProtocolRegistryPack
from .protocols import ProtocolClass
from .scenarios import graph_kernel_protocol_class


SIGNATURE_ALGORITHM = "hmac-sha256"
REGISTRY_SIGNER = "control-plane:protocol-registry"


@dataclass(frozen=True)
class ProtocolRegistryEntry:
    protocol_class: ProtocolClass
    enabled_for: tuple[str, ...] = ("platform_private",)
    policy_refs: tuple[str, ...] = ("policy:simulation-only",)
    simulation_refs: tuple[str, ...] = ("graph_kernel_scenarios@v1",)
    deprecated_at: str | None = None
    retired_at: str | None = None

    @property
    def protocol_id(self) -> str:
        return self.protocol_class.protocol_ref.id

    @property
    def enabled(self) -> bool:
        return self.retired_at is None

    def to_payload(self) -> dict[str, Any]:
        payload = self.protocol_class.to_payload()
        out = {
            "protocol_id": self.protocol_id,
            "protocol_ref": payload["protocol_ref"],
            "display_name": self.protocol_class.protocol_ref.display_name,
            "risk_class": self.protocol_class.risk_class,
            "enabled": self.enabled,
            "enabled_for": list(self.enabled_for),
            "policy_refs": list(self.policy_refs),
            "simulation_refs": list(self.simulation_refs),
            "deprecated_at": self.deprecated_at,
            "retired_at": self.retired_at,
            "scenario_count": len(self.protocol_class.scenarios),
            "invariant_count": len(self.protocol_class.invariants),
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
            "protocol_class": payload,
        }
        digest = canonical_digest(out)
        out["registry_integrity"] = {
            "storage": "static",
            "canonical_digest": digest,
            "signature_algorithm": SIGNATURE_ALGORITHM,
            "signature": sign_digest(digest),
            "signed_by": REGISTRY_SIGNER,
            "digest_valid": True,
            "signature_valid": True,
            "integrity_status": "valid",
        }
        return out


_REGISTRY: dict[str, ProtocolRegistryEntry] = {
    "graph_kernel": ProtocolRegistryEntry(protocol_class=graph_kernel_protocol_class())
}


def list_protocol_registry() -> list[dict[str, Any]]:
    return [_REGISTRY[key].to_payload() for key in sorted(_REGISTRY)]


def get_protocol_registry_entry(protocol_id: str) -> ProtocolRegistryEntry:
    key = str(protocol_id or "").strip()
    if key not in _REGISTRY:
        raise KeyError(f"unknown protocol: {protocol_id}")
    return _REGISTRY[key]


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sign_digest(digest: str) -> str:
    secret = settings.protocol_registry_signing_secret or settings.jwt_secret
    return hmac.new(secret.encode("utf-8"), digest.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(digest: str, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign_digest(digest), str(signature))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _seed_payload(entry: ProtocolRegistryEntry) -> dict[str, Any]:
    payload = entry.protocol_class.to_payload()
    return {
        "protocol_id": entry.protocol_id,
        "protocol_ref": payload["protocol_ref"],
        "display_name": entry.protocol_class.protocol_ref.display_name,
        "risk_class": entry.protocol_class.risk_class,
        "enabled": entry.enabled,
        "enabled_for": list(entry.enabled_for),
        "policy_refs": list(entry.policy_refs),
        "simulation_refs": list(entry.simulation_refs),
        "deprecated_at": entry.deprecated_at,
        "retired_at": entry.retired_at,
        "scenario_count": len(entry.protocol_class.scenarios),
        "invariant_count": len(entry.protocol_class.invariants),
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
        "protocol_class": payload,
    }


def _row_payload(row: ProtocolRegistryPack) -> dict[str, Any]:
    protocol_class = dict(row.protocol_class or {})
    scenarios = protocol_class.get("scenarios") if isinstance(protocol_class, dict) else []
    invariants = protocol_class.get("invariants") if isinstance(protocol_class, dict) else []
    protocol_ref = protocol_class.get("protocol_ref") if isinstance(protocol_class, dict) else {}
    return {
        "protocol_id": row.protocol_id,
        "protocol_ref": protocol_ref if isinstance(protocol_ref, dict) else {},
        "display_name": row.display_name,
        "risk_class": row.risk_class,
        "enabled": bool(row.enabled),
        "enabled_for": list(row.enabled_for or []),
        "policy_refs": list(row.policy_refs or []),
        "simulation_refs": list(row.simulation_refs or []),
        "deprecated_at": _iso(row.deprecated_at),
        "retired_at": _iso(row.retired_at),
        "scenario_count": len(scenarios) if isinstance(scenarios, list) else 0,
        "invariant_count": len(invariants) if isinstance(invariants, list) else 0,
        "simulation_only": bool(row.simulation_only),
        "proposal_only": bool(row.proposal_only),
        "active_apply_enabled": bool(row.active_apply_enabled),
        "protocol_class": protocol_class,
    }


def protocol_registry_row_payload(row: ProtocolRegistryPack) -> dict[str, Any]:
    payload = _row_payload(row)
    computed_digest = canonical_digest(payload)
    digest_valid = hmac.compare_digest(computed_digest, str(row.canonical_digest or ""))
    signature_valid = verify_signature(str(row.canonical_digest or ""), row.signature)
    raw_protocol_class = row.protocol_class if isinstance(row.protocol_class, dict) else {}
    raw_protocol_ref = raw_protocol_class.get("protocol_ref")
    metadata_valid = (
        isinstance(raw_protocol_ref, dict)
        and str(raw_protocol_ref.get("id") or "") == row.protocol_id
    )
    safe_contract = (
        metadata_valid
        and bool(row.simulation_only)
        and bool(row.proposal_only)
        and not bool(row.active_apply_enabled)
    )
    integrity_valid = digest_valid and signature_valid and safe_contract
    enabled = bool(row.enabled) and row.retired_at is None and integrity_valid
    if not metadata_valid:
        integrity_status = "malformed_metadata"
    elif not safe_contract:
        integrity_status = "unsafe_active_apply_requested"
    elif not digest_valid:
        integrity_status = "digest_mismatch"
    elif not signature_valid:
        integrity_status = "signature_mismatch"
    elif not row.enabled or row.retired_at is not None:
        integrity_status = "disabled"
    else:
        integrity_status = "valid"
    payload["enabled"] = enabled
    payload["simulation_only"] = True
    payload["proposal_only"] = True
    payload["active_apply_enabled"] = False
    payload["registry_integrity"] = {
        "storage": "persistent",
        "canonical_digest": row.canonical_digest,
        "computed_digest": computed_digest,
        "signature_algorithm": row.signature_algorithm,
        "signature": row.signature,
        "signed_by": row.signed_by,
        "digest_valid": digest_valid,
        "signature_valid": signature_valid,
        "metadata_valid": metadata_valid,
        "safe_contract": safe_contract,
        "integrity_status": integrity_status,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    return payload


def protocol_registry_pack_from_entry(entry: ProtocolRegistryEntry) -> ProtocolRegistryPack:
    payload = _seed_payload(entry)
    digest = canonical_digest(payload)
    return ProtocolRegistryPack(
        protocol_id=entry.protocol_id,
        protocol_version=int(entry.protocol_class.protocol_ref.version),
        display_name=entry.protocol_class.protocol_ref.display_name,
        risk_class=entry.protocol_class.risk_class,
        enabled=entry.enabled,
        simulation_only=True,
        proposal_only=True,
        active_apply_enabled=False,
        enabled_for=list(entry.enabled_for),
        policy_refs=list(entry.policy_refs),
        simulation_refs=list(entry.simulation_refs),
        protocol_class=entry.protocol_class.to_payload(),
        metadata_json={"source": "platform_seed"},
        canonical_digest=digest,
        signature=sign_digest(digest),
        signature_algorithm=SIGNATURE_ALGORITHM,
        signed_by=REGISTRY_SIGNER,
    )


async def ensure_seed_protocol_registry(session: AsyncSession) -> bool:
    inserted = False
    for entry in _REGISTRY.values():
        existing = (
            await session.execute(
                select(ProtocolRegistryPack).where(
                    ProtocolRegistryPack.protocol_id == entry.protocol_id,
                    ProtocolRegistryPack.protocol_version == int(entry.protocol_class.protocol_ref.version),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(protocol_registry_pack_from_entry(entry))
            inserted = True
    await session.flush()
    return inserted


async def list_persistent_protocol_registry(session: AsyncSession) -> list[dict[str, Any]]:
    inserted = await ensure_seed_protocol_registry(session)
    if inserted:
        await session.commit()
    rows = (
        await session.execute(
            select(ProtocolRegistryPack).order_by(
                ProtocolRegistryPack.protocol_id.asc(),
                ProtocolRegistryPack.protocol_version.asc(),
            )
        )
    ).scalars().all()
    return [protocol_registry_row_payload(row) for row in rows]


async def get_persistent_protocol_registry_payload(
    session: AsyncSession,
    protocol_id: str,
    protocol_version: int | None = None,
) -> dict[str, Any]:
    inserted = await ensure_seed_protocol_registry(session)
    if inserted:
        await session.commit()
    filters = [ProtocolRegistryPack.protocol_id == str(protocol_id or "").strip()]
    if protocol_version is not None:
        filters.append(ProtocolRegistryPack.protocol_version == int(protocol_version))
    stmt = select(ProtocolRegistryPack).where(*filters).order_by(ProtocolRegistryPack.protocol_version.desc())
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        raise KeyError(f"unknown protocol: {protocol_id}")
    return protocol_registry_row_payload(row)
