from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import agent_studio_autopilot as autopilot, auth
from control_plane.agent_review import REVIEWER_CALL_TTL_SECONDS
from control_plane.auth import (
    AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS,
    AGENT_INVOKE_TOKEN_TYPE,
    decode_token,
)
from control_plane.config import settings
from control_plane.db import Base
from control_plane.k8s import KNATIVE_MAX_TIMEOUT_SECONDS
from control_plane.models import (
    Agent,
    AgentDeployment,
    AgentStudioAutopilotPolicy,
    AgentStudioUpgradeProposal,
    User,
)
from control_plane.routes import agent_studio as studio_route


def _claims(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_alg],
        options={"verify_aud": False},
    )


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(factory):
    async with factory() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            gitea_owner="owner-repos",
            name="invoice-helper",
            description="Invoices",
            version="0.1.0",
            image="img",
            status="running",
            card={},
        )
        session.add(agent)
        await session.flush()
        session.add(
            AgentDeployment(
                deploy_id="dep-base",
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                status="live",
                head_sha="base-sha",
            )
        )
        policy = AgentStudioAutopilotPolicy(
            user_id=user.id,
            enabled=True,
            timezone="UTC",
            daily_hour=9,
            next_run_at=datetime.now(UTC),
        )
        session.add(policy)
        await session.commit()
        await session.refresh(user)
        await session.refresh(agent)
        await session.refresh(policy)
        return user, agent, policy


def test_next_daily_time_respects_owner_timezone() -> None:
    after = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    result = autopilot.next_daily_time(
        "America/Sao_Paulo", 9, after=after
    )
    assert result == datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_daily_scan_creates_review_backed_proposal_and_emails(
    session_factory, monkeypatch
) -> None:
    user, _agent, policy = await _seed(session_factory)
    captured = {}

    async def fake_review(**kwargs):
        captured["review"] = kwargs
        return {
            "ok": True,
            "summary": "one useful improvement",
            "findings": [
                {
                    "severity": "warning",
                    "category": "correctness",
                    "message": "Transient API failures are not retried.",
                    "file": "agent.py",
                    "line": 42,
                    "suggestion": "Add a bounded retry with exponential backoff.",
                }
            ],
        }

    def fake_email(email_user, proposals):
        captured["email"] = (email_user, proposals)
        return True

    monkeypatch.setattr(autopilot, "SessionLocal", session_factory)
    monkeypatch.setattr(autopilot, "_reviewer_llm_creds", lambda **_kwargs: {"model": "test"})
    monkeypatch.setattr(autopilot, "_call_reviewer", fake_review)
    monkeypatch.setattr(autopilot, "_send_digest_email", fake_email)

    await autopilot.run_policy_scan(policy.id)

    async with session_factory() as session:
        proposal = (
            await session.execute(select(AgentStudioUpgradeProposal))
        ).scalar_one()
        saved_policy = await session.get(AgentStudioAutopilotPolicy, policy.id)
    assert proposal.agent_name == "invoice-helper"
    assert proposal.source_head_sha == "base-sha"
    assert proposal.status == "pending"
    assert proposal.idea == "Add a bounded retry with exponential backoff."
    assert proposal.evidence["finding"]["line"] == 42
    assert proposal.emailed_at is not None
    assert saved_policy and saved_policy.last_run_status == "complete"
    assert captured["review"]["owner"] == "owner-repos"
    assert captured["review"]["mode"] == "improvements"
    assert captured["email"][0].email == user.email
    # A scan may walk 100 agents; the identity it hands the reviewer is minted
    # per call and expires with that call, not a week later.
    scan_claims = _claims(captured["review"]["cp_jwt"])
    assert scan_claims["exp"] - scan_claims["iat"] == REVIEWER_CALL_TTL_SECONDS


@pytest.mark.asyncio
async def test_daily_scan_excludes_openapi_generated_agents(
    session_factory, monkeypatch
) -> None:
    user, _agent, policy = await _seed(session_factory)
    async with session_factory() as session:
        session.add(
            Agent(
                owner_id=user.id,
                gitea_owner="owner-repos",
                name="payments-openapi-agent",
                description="Payments API",
                version="0.1.0",
                image="img",
                status="running",
                card={
                    "capabilities": {"openapi_auto_agent": {"regenerable": True}},
                    "runtime": {"tools_used": ["openapi", "deepagents"]},
                },
            )
        )
        await session.commit()

    reviewed = []

    async def fake_review(**kwargs):
        reviewed.append(kwargs["agent_name"])
        return {"ok": True, "summary": "clean", "findings": []}

    monkeypatch.setattr(autopilot, "SessionLocal", session_factory)
    monkeypatch.setattr(autopilot, "_reviewer_llm_creds", lambda **_kwargs: {"model": "test"})
    monkeypatch.setattr(autopilot, "_call_reviewer", fake_review)

    await autopilot.run_policy_scan(policy.id)

    assert reviewed == ["invoice-helper"]


@pytest.mark.asyncio
async def test_accepted_proposal_runs_studio_upgrade_once(
    session_factory, monkeypatch
) -> None:
    user, agent, _policy = await _seed(session_factory)
    async with session_factory() as session:
        proposal = AgentStudioUpgradeProposal(
            proposal_id="aup_apply",
            user_id=user.id,
            agent_id=agent.id,
            agent_name=agent.name,
            source_head_sha="base-sha",
            status="accepted",
            severity="warning",
            category="correctness",
            title="Retry transient failures",
            idea="Add a bounded retry with exponential backoff.",
            rationale="Daily review",
            evidence={"finding": {"file": "agent.py"}},
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        session.add(proposal)
        await session.commit()

    captured = {}

    async def fake_upgrade(body):
        captured["body"] = body
        return {
            "ok": True,
            "status": "succeeded",
            "run_id": "agent-upgrade-1",
            "head_sha": "next-sha",
        }

    monkeypatch.setattr(autopilot, "SessionLocal", session_factory)
    monkeypatch.setattr(
        autopilot,
        "_studio_grant",
        lambda **_kwargs: ("grant", {"model": "test"}),
    )
    monkeypatch.setattr(autopilot, "_call_studio_upgrade", fake_upgrade)

    await autopilot.run_proposal_upgrade("aup_apply")
    await autopilot.run_proposal_upgrade("aup_apply")

    async with session_factory() as session:
        saved = (
            await session.execute(
                select(AgentStudioUpgradeProposal).where(
                    AgentStudioUpgradeProposal.proposal_id == "aup_apply"
                )
            )
        ).scalar_one()
    assert saved.status == "applied"
    assert saved.upgrade_run_id == "agent-upgrade-1"
    assert saved.completed_at is not None
    assert captured["body"]["arguments"]["expected_head_sha"] == "base-sha"
    # ``agent-studio`` is first-party build code from this repo and its upgrade
    # skill drives the owner's source and deploy surface for them, so it still
    # receives an ordinary platform credential. What it must not receive is one
    # that outlives the upgrade: the TTL is the outer bound the platform
    # actually enforces on the call. ``UPGRADE_TIMEOUT_SECONDS`` is only the
    # httpx client timeout; agent-studio's own Knative revision is capped at
    # ``KNATIVE_MAX_TIMEOUT_SECONDS``, which is what the clamp encodes.
    upgrade_claims = _claims(captured["body"]["cp_jwt"])
    assert "typ" not in upgrade_claims
    lifetime = upgrade_claims["exp"] - upgrade_claims["iat"]
    assert lifetime == AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS
    assert lifetime > KNATIVE_MAX_TIMEOUT_SECONDS
    assert lifetime < settings.jwt_ttl_seconds


@pytest.mark.asyncio
async def test_studio_upgrade_credential_obeys_the_toolchain_set(
    session_factory, monkeypatch
) -> None:
    """The carve-out is read from one place, not re-implemented at this sink.

    ``run_proposal_upgrade`` writes a credential into the invoke body of a
    deployed agent process. It gets a full platform session only because
    ``PLATFORM_TOOLCHAIN_AGENTS`` lists ``agent-studio``; drop it from that set
    and this sink has to follow, or the platform's single statement of "which
    agents may hold a session" stops governing the sink that matters most.
    """
    user, agent, _policy = await _seed(session_factory)
    async with session_factory() as session:
        session.add(
            AgentStudioUpgradeProposal(
                proposal_id="aup_scoped",
                user_id=user.id,
                agent_id=agent.id,
                agent_name=agent.name,
                source_head_sha="base-sha",
                status="accepted",
                severity="warning",
                category="correctness",
                title="Retry transient failures",
                idea="Add a bounded retry.",
                rationale="Daily review",
                evidence={},
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await session.commit()

    captured: dict[str, Any] = {}

    async def fake_upgrade(body):
        captured["body"] = body
        return {"ok": True, "status": "succeeded", "run_id": "r1"}

    monkeypatch.setattr(autopilot, "SessionLocal", session_factory)
    monkeypatch.setattr(
        autopilot, "_studio_grant", lambda **_kwargs: ("grant", {"model": "test"})
    )
    monkeypatch.setattr(autopilot, "_call_studio_upgrade", fake_upgrade)
    monkeypatch.setattr(
        auth,
        "PLATFORM_TOOLCHAIN_AGENTS",
        auth.PLATFORM_TOOLCHAIN_AGENTS - {autopilot.STUDIO_AGENT_NAME},
    )

    await autopilot.run_proposal_upgrade("aup_scoped")

    claims = _claims(captured["body"]["cp_jwt"])
    assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE
    assert claims["aud"] == "agent:agent-studio"
    with pytest.raises(HTTPException):
        decode_token(captured["body"]["cp_jwt"])


@pytest.mark.asyncio
async def test_reject_decision_is_terminal_and_never_enqueues(
    session_factory, monkeypatch
) -> None:
    user, agent, _policy = await _seed(session_factory)
    async with session_factory() as session:
        proposal = AgentStudioUpgradeProposal(
            proposal_id="aup_reject",
            user_id=user.id,
            agent_id=agent.id,
            agent_name=agent.name,
            status="pending",
            severity="info",
            category="ergonomics",
            title="Rename a tool",
            idea="Use a clearer tool name for invoice lookup.",
            rationale="Daily review",
            evidence={},
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        session.add(proposal)
        await session.commit()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        enqueued = []
        monkeypatch.setattr(
            studio_route,
            "enqueue_proposal_upgrade",
            lambda *_args, **kwargs: enqueued.append(kwargs),
        )
        result = await studio_route.decide_studio_upgrade_proposal(
            "aup_reject",
            studio_route.StudioProposalDecisionIn(decision="reject"),
            request,
            user,
            session,
        )

    assert result.status == "rejected"
    assert result.decided_at is not None
    assert enqueued == []


def test_digest_email_links_to_confirmation_not_mutating_api(monkeypatch) -> None:
    sent = {}

    class FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            pass

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setenv("A2A_CP_STUDIO_SMTP_HOST", "smtp.test")
    monkeypatch.delenv("A2A_CP_STUDIO_SMTP_USER", raising=False)
    monkeypatch.delenv("A2A_CP_STUDIO_SMTP_PASSWORD", raising=False)
    monkeypatch.setattr(autopilot.smtplib, "SMTP", FakeSMTP)
    user = SimpleNamespace(id=7, email="owner@example.com")
    proposal = SimpleNamespace(
        proposal_id="aup_email",
        agent_name="invoice-helper",
        severity="warning",
        title="Retry transient failures",
        rationale="Found in agent.py:42.",
    )

    assert autopilot._send_digest_email(user, [proposal]) is True

    content = "\n".join(part.get_content() for part in sent["message"].iter_parts())
    assert "/studio?proposal=aup_email&amp;decision=accept" in content
    assert "/studio?proposal=aup_email&amp;decision=reject" in content
    assert "/decision" not in content


@pytest.mark.asyncio
async def test_accept_decision_durably_enqueues_upgrade(
    session_factory, monkeypatch
) -> None:
    user, agent, _policy = await _seed(session_factory)
    async with session_factory() as session:
        session.add(
            AgentStudioUpgradeProposal(
                proposal_id="aup_accept",
                user_id=user.id,
                agent_id=agent.id,
                agent_name=agent.name,
                source_head_sha="base-sha",
                status="pending",
                severity="warning",
                category="correctness",
                title="Retry transient failures",
                idea="Add a bounded retry with exponential backoff.",
                rationale="Daily review",
                evidence={},
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        await session.commit()
        enqueued = []
        monkeypatch.setattr(
            studio_route,
            "enqueue_proposal_upgrade",
            lambda *_args, **kwargs: enqueued.append(kwargs),
        )
        result = await studio_route.decide_studio_upgrade_proposal(
            "aup_accept",
            studio_route.StudioProposalDecisionIn(decision="accept"),
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            user,
            session,
        )

    assert result.status == "accepted"
    assert result.decided_at is not None
    assert enqueued == [{"proposal_id": "aup_accept"}]
