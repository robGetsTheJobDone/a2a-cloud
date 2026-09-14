from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentReceipt,
    GrantAudit,
    Organization,
    OrganizationAuditLog,
    OrganizationCompliancePolicy,
    OrganizationMember,
    User,
)
from control_plane.routes.compliance import (
    AgentRiskProfileIn,
    CompliancePolicyIn,
    EvidenceExportIn,
    enforce_retention,
    export_evidence_pack,
    get_policy,
    get_status,
    list_decision_records,
    put_agent_profile,
    put_policy,
)


async def _seed_org(session: AsyncSession) -> tuple[User, User]:
    owner = User(id=1, email="owner@acme.com", password_hash="x")
    member = User(id=2, email="member@acme.com", password_hash="x")
    org = Organization(id=1, slug="acme", name="Acme", created_by_id=owner.id)
    session.add_all(
        [
            owner,
            member,
            org,
            OrganizationMember(
                id=1, organization_id=org.id, user_id=owner.id, role="owner"
            ),
            OrganizationMember(
                id=2, organization_id=org.id, user_id=member.id, role="member"
            ),
        ]
    )
    await session.commit()
    return owner, member


async def _seed_agent_with_records(session: AsyncSession, owner: User) -> Agent:
    agent = Agent(
        id=1,
        owner_id=owner.id,
        organization_id=1,
        name="triage-bot",
        version="1.0.0",
        image="registry/triage:1.0.0",
        card={},
    )
    old = datetime.now(timezone.utc) - timedelta(days=400)
    session.add_all(
        [
            agent,
            AgentReceipt(
                receipt_id="rcpt-recent",
                agent_id=1,
                agent_name="triage-bot",
                skill_name="triage",
                status="ok",
                signed_token="tok",
                payload={"grant_ids": ["g-1"]},
            ),
            AgentReceipt(
                receipt_id="rcpt-old",
                agent_id=1,
                agent_name="triage-bot",
                skill_name="triage",
                status="ok",
                signed_token="tok",
                payload={},
                created_at=old,
            ),
            GrantAudit(
                grant_id="g-1",
                issuer="control-plane",
                audience="triage-bot",
                bucket="workspace",
                mode="rw",
                ttl_seconds=600,
                user_id=owner.id,
                decision="auto_approve",
                decided_by="auto",
            ),
        ]
    )
    await session.commit()
    return agent


@pytest.mark.asyncio
async def test_policy_floor_enforced_while_eu_ai_act_active() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, member = await _seed_org(session)

            with pytest.raises(HTTPException) as exc_info:
                await put_policy(
                    "acme",
                    CompliancePolicyIn(
                        frameworks=["eu_ai_act"], retention_days=90
                    ),
                    user=owner,
                    session=session,
                )
            assert exc_info.value.status_code == 422

            out = await put_policy(
                "acme",
                CompliancePolicyIn(frameworks=["eu_ai_act"], retention_days=365),
                user=owner,
                session=session,
            )
            assert out.retention_days == 365
            assert out.retention_floor_days == 180
            assert out.persisted is True

            # Members can't administer the compliance policy.
            with pytest.raises(HTTPException) as exc_info:
                await put_policy(
                    "acme",
                    CompliancePolicyIn(),
                    user=member,
                    session=session,
                )
            assert exc_info.value.status_code == 403

            audit = (
                await session.execute(select(OrganizationAuditLog))
            ).scalars().all()
            assert any(a.action == "compliance.policy_updated" for a in audit)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_default_policy_returned_without_persisting() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, _ = await _seed_org(session)
            out = await get_policy("acme", user=owner, session=session)
            assert out.frameworks == ["eu_ai_act"]
            assert out.retention_days == 180
            assert out.persisted is False
            rows = (
                await session.execute(select(OrganizationCompliancePolicy))
            ).scalars().all()
            assert rows == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_decision_records_merge_receipts_grants_and_audit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, _ = await _seed_org(session)
            await _seed_agent_with_records(session, owner)

            records = await list_decision_records(
                "acme",
                kind=None,
                agent=None,
                outcome=None,
                from_at=None,
                to_at=None,
                limit=100,
                user=owner,
                session=session,
            )
            kinds = {r.kind for r in records}
            assert "skill_execution" in kinds
            assert "authorization" in kinds
            by_id = {r.record_id: r for r in records}
            assert by_id["rcpt-recent"].details["grant_ids"] == ["g-1"]
            assert by_id["g-1"].outcome == "auto_approve"

            only_grants = await list_decision_records(
                "acme",
                kind="authorization",
                agent=None,
                outcome=None,
                from_at=None,
                to_at=None,
                limit=100,
                user=owner,
                session=session,
            )
            assert {r.kind for r in only_grants} == {"authorization"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_purge_respects_floor_and_legal_hold() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, member = await _seed_org(session)
            await _seed_agent_with_records(session, owner)

            # Legal hold blocks the purge outright.
            await put_policy(
                "acme",
                CompliancePolicyIn(
                    frameworks=["eu_ai_act"],
                    retention_days=180,
                    legal_hold=True,
                    legal_hold_reason="litigation",
                ),
                user=owner,
                session=session,
            )
            with pytest.raises(HTTPException) as exc_info:
                await enforce_retention("acme", user=owner, session=session)
            assert exc_info.value.status_code == 409

            await put_policy(
                "acme",
                CompliancePolicyIn(frameworks=["eu_ai_act"], retention_days=180),
                user=owner,
                session=session,
            )

            # Non-owner admins can't purge.
            with pytest.raises(HTTPException) as exc_info:
                await enforce_retention("acme", user=member, session=session)
            assert exc_info.value.status_code == 403

            out = await enforce_retention("acme", user=owner, session=session)
            assert out.receipts_deleted == 1  # only the 400-day-old receipt

            remaining = (
                await session.execute(select(AgentReceipt.receipt_id))
            ).scalars().all()
            assert remaining == ["rcpt-recent"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_risk_profile_and_status_counts() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, _ = await _seed_org(session)
            await _seed_agent_with_records(session, owner)

            profile = await put_agent_profile(
                "acme",
                "triage-bot",
                AgentRiskProfileIn(
                    risk_tier="high",
                    intended_purpose="Support ticket triage",
                    human_oversight="Escalations reviewed by on-call",
                ),
                user=owner,
                session=session,
            )
            assert profile.risk_tier == "high"

            status = await get_status("acme", user=owner, session=session)
            assert status.agents_total == 1
            assert status.agents_classified == 1
            assert status.agents_high_risk == 1
            assert status.retention_ok is True
            assert status.record_counts["skill_execution"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_evidence_export_includes_controls_and_audits_itself() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, _ = await _seed_org(session)
            await _seed_agent_with_records(session, owner)

            pack = await export_evidence_pack(
                "acme",
                EvidenceExportIn(verify_signatures=False),
                user=owner,
                session=session,
            )
            assert pack["organization"]["slug"] == "acme"
            controls = pack["framework_controls"]["eu_ai_act"]
            assert any("Article 12" in c["control"] for c in controls)
            assert pack["record_counts"]["skill_execution"] == 2
            assert pack["agent_inventory"][0]["agent_name"] == "triage-bot"

            audit = (
                await session.execute(select(OrganizationAuditLog))
            ).scalars().all()
            assert any(
                a.action == "compliance.evidence_exported" for a in audit
            )
    finally:
        await engine.dispose()
