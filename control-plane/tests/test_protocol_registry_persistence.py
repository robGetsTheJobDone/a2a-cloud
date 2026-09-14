from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.db import Base
from control_plane.models import ProtocolRegistryPack
from control_plane.protocol_registry import (
    canonical_digest,
    get_persistent_protocol_registry_payload,
    get_protocol_registry_entry,
    list_persistent_protocol_registry,
    protocol_registry_pack_from_entry,
    sign_digest,
)


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            yield session
    finally:
        await engine.dispose()


async def _seeded_row(session: AsyncSession) -> ProtocolRegistryPack:
    await list_persistent_protocol_registry(session)
    row = (
        await session.execute(
            select(ProtocolRegistryPack).where(
                ProtocolRegistryPack.protocol_id == "graph_kernel"
            )
        )
    ).scalar_one()
    return row


@pytest.mark.asyncio
async def test_persistent_registry_seeds_signed_graph_kernel() -> None:
    async with _session() as session:
        registry = await list_persistent_protocol_registry(session)

        assert [entry["protocol_id"] for entry in registry] == ["graph_kernel"]
        entry = registry[0]
        assert entry["enabled"] is True
        assert entry["simulation_only"] is True
        assert entry["proposal_only"] is True
        assert entry["active_apply_enabled"] is False
        assert entry["scenario_count"] == 15
        assert entry["registry_integrity"]["storage"] == "persistent"
        assert entry["registry_integrity"]["digest_valid"] is True
        assert entry["registry_integrity"]["signature_valid"] is True
        assert entry["registry_integrity"]["metadata_valid"] is True
        assert entry["registry_integrity"]["integrity_status"] == "valid"


@pytest.mark.asyncio
async def test_persistent_registry_unknown_protocol_raises() -> None:
    async with _session() as session:
        with pytest.raises(KeyError, match="unknown protocol"):
            await get_persistent_protocol_registry_payload(session, "missing_pack")


@pytest.mark.asyncio
async def test_persistent_registry_duplicate_protocol_version_is_rejected() -> None:
    async with _session() as session:
        await list_persistent_protocol_registry(session)
        session.add(protocol_registry_pack_from_entry(get_protocol_registry_entry("graph_kernel")))

        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_persistent_registry_tampered_digest_disables_entry() -> None:
    async with _session() as session:
        row = await _seeded_row(session)
        row.protocol_class = {
            **row.protocol_class,
            "protocol_ref": {**row.protocol_class["protocol_ref"], "display_name": "Tampered"},
        }
        await session.commit()

        entry = (await list_persistent_protocol_registry(session))[0]
        assert entry["enabled"] is False
        assert entry["active_apply_enabled"] is False
        assert entry["registry_integrity"]["digest_valid"] is False
        assert entry["registry_integrity"]["signature_valid"] is True
        assert entry["registry_integrity"]["integrity_status"] == "digest_mismatch"


@pytest.mark.asyncio
async def test_persistent_registry_tampered_signature_disables_entry() -> None:
    async with _session() as session:
        row = await _seeded_row(session)
        row.signature = "0" * 64
        await session.commit()

        entry = (await list_persistent_protocol_registry(session))[0]
        assert entry["enabled"] is False
        assert entry["registry_integrity"]["digest_valid"] is True
        assert entry["registry_integrity"]["signature_valid"] is False
        assert entry["registry_integrity"]["integrity_status"] == "signature_mismatch"


@pytest.mark.asyncio
async def test_persistent_registry_missing_signature_disables_entry() -> None:
    async with _session() as session:
        row = await _seeded_row(session)
        row.signature = ""
        await session.commit()

        entry = (await list_persistent_protocol_registry(session))[0]
        assert entry["enabled"] is False
        assert entry["active_apply_enabled"] is False
        assert entry["registry_integrity"]["digest_valid"] is True
        assert entry["registry_integrity"]["signature_valid"] is False
        assert entry["registry_integrity"]["integrity_status"] == "signature_mismatch"


@pytest.mark.asyncio
async def test_persistent_registry_disabled_pack_stays_visible_but_inactive() -> None:
    async with _session() as session:
        row = await _seeded_row(session)
        row.enabled = False
        row.canonical_digest = canonical_digest(entry_payload_for_row(row))
        row.signature = sign_digest(row.canonical_digest)
        await session.commit()

        entry = (await list_persistent_protocol_registry(session))[0]
        assert entry["enabled"] is False
        assert entry["active_apply_enabled"] is False
        assert entry["registry_integrity"]["integrity_status"] == "disabled"


@pytest.mark.asyncio
async def test_persistent_registry_signed_active_apply_request_is_blocked() -> None:
    async with _session() as session:
        row = await _seeded_row(session)
        row.active_apply_enabled = True
        payload = {
            **entry_payload_for_row(row),
            "active_apply_enabled": True,
        }
        row.canonical_digest = canonical_digest(payload)
        row.signature = sign_digest(row.canonical_digest)
        await session.commit()

        entry = (await list_persistent_protocol_registry(session))[0]
        assert entry["enabled"] is False
        assert entry["active_apply_enabled"] is False
        assert entry["registry_integrity"]["digest_valid"] is True
        assert entry["registry_integrity"]["signature_valid"] is True
        assert entry["registry_integrity"]["integrity_status"] == "unsafe_active_apply_requested"


@pytest.mark.asyncio
async def test_persistent_registry_malformed_signed_metadata_is_blocked() -> None:
    async with _session() as session:
        row = await _seeded_row(session)
        row.protocol_class = {"protocol_ref": {"id": "different"}}
        payload = entry_payload_for_row(row)
        row.canonical_digest = canonical_digest(payload)
        row.signature = sign_digest(row.canonical_digest)
        await session.commit()

        entry = (await list_persistent_protocol_registry(session))[0]
        assert entry["enabled"] is False
        assert entry["scenario_count"] == 0
        assert entry["active_apply_enabled"] is False
        assert entry["registry_integrity"]["digest_valid"] is True
        assert entry["registry_integrity"]["signature_valid"] is True
        assert entry["registry_integrity"]["metadata_valid"] is False
        assert entry["registry_integrity"]["integrity_status"] == "malformed_metadata"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol_ref",
    [
        {},
        {"id": ""},
        {"id": "other_protocol"},
        "not-a-ref",
    ],
)
async def test_persistent_registry_fuzzes_malformed_protocol_ref(protocol_ref: object) -> None:
    async with _session() as session:
        row = await _seeded_row(session)
        row.protocol_class = {
            **row.protocol_class,
            "protocol_ref": protocol_ref,
        }
        payload = entry_payload_for_row(row)
        row.canonical_digest = canonical_digest(payload)
        row.signature = sign_digest(row.canonical_digest)
        await session.commit()

        entry = (await list_persistent_protocol_registry(session))[0]
        assert entry["enabled"] is False
        assert entry["active_apply_enabled"] is False
        assert entry["registry_integrity"]["digest_valid"] is True
        assert entry["registry_integrity"]["signature_valid"] is True
        assert entry["registry_integrity"]["metadata_valid"] is False
        assert entry["registry_integrity"]["integrity_status"] == "malformed_metadata"


def entry_payload_for_row(row: ProtocolRegistryPack) -> dict[str, object]:
    protocol_class = dict(row.protocol_class or {})
    scenarios = protocol_class.get("scenarios")
    invariants = protocol_class.get("invariants")
    protocol_ref = protocol_class.get("protocol_ref")
    return {
        "protocol_id": row.protocol_id,
        "protocol_ref": protocol_ref if isinstance(protocol_ref, dict) else {},
        "display_name": row.display_name,
        "risk_class": row.risk_class,
        "enabled": bool(row.enabled),
        "enabled_for": list(row.enabled_for or []),
        "policy_refs": list(row.policy_refs or []),
        "simulation_refs": list(row.simulation_refs or []),
        "deprecated_at": None,
        "retired_at": None,
        "scenario_count": len(scenarios) if isinstance(scenarios, list) else 0,
        "invariant_count": len(invariants) if isinstance(invariants, list) else 0,
        "simulation_only": bool(row.simulation_only),
        "proposal_only": bool(row.proposal_only),
        "active_apply_enabled": bool(row.active_apply_enabled),
        "protocol_class": protocol_class,
    }
