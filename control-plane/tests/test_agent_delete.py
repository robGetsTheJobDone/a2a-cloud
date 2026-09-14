from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentApiToken,
    AgentAuthConnection,
    AgentCodeEditorOptIn,
    AgentConsumerSetupValue,
    AgentCustomDomain,
    AgentDeployment,
    AgentDeploymentEvent,
    AgentInstall,
    AgentLineage,
    AgentMemoryEntry,
    AgentProofRun,
    AgentReceipt,
    AgentReviewRun,
    AgentSecret,
    AgentSession,
    Bounty,
    DatabaseProvisionEvent,
    MetaAgentRun,
    User,
)
from control_plane.routes import agents


@pytest.mark.asyncio
async def test_remove_agent_cleans_database_dependents_without_fk_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="owner@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            agent = Agent(
                owner_id=user.id,
                name="delete-me",
                description="",
                version="0.1.0",
                image="registry.example/delete-me:latest",
                public=True,
                status="running",
                url="https://delete-me.example.test",
                card={},
            )
            session.add(agent)
            await session.flush()
            deployment = AgentDeployment(
                deploy_id="deploy-delete-me",
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                status="live",
            )
            session.add(deployment)
            await session.flush()
            session.add_all(
                [
                    AgentApiToken(
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name=agent.name,
                        name="ci",
                        token_hash="h" * 64,
                        token_last4="last",
                    ),
                    AgentAuthConnection(
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name=agent.name,
                        scheme_name="apiKeyAuth",
                        scheme_type="api_key",
                    ),
                    AgentCodeEditorOptIn(agent_id=agent.id, user_id=user.id),
                    AgentConsumerSetupValue(
                        agent_id=agent.id,
                        agent_name=agent.name,
                        field_name="API_KEY",
                        scope="user",
                        user_id=user.id,
                    ),
                    AgentCustomDomain(
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name=agent.name,
                        hostname="delete-me.example.test",
                        verification_token="verify",
                    ),
                    AgentDeploymentEvent(
                        deployment_id=deployment.id,
                        deploy_id=deployment.deploy_id,
                        agent_name=agent.name,
                        stage="runtime",
                        status="live",
                    ),
                    AgentInstall(
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name=agent.name,
                    ),
                    AgentMemoryEntry(
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name=agent.name,
                        key="note",
                        value={"text": "keep"},
                    ),
                    AgentProofRun(
                        agent_id=agent.id,
                        agent_name=agent.name,
                        user_id=user.id,
                        skill_name="run",
                    ),
                    AgentReceipt(
                        receipt_id="receipt-delete-me",
                        agent_id=agent.id,
                        agent_name=agent.name,
                        skill_name="run",
                        signed_token="signed",
                    ),
                    AgentReviewRun(
                        review_id="review-delete-me",
                        agent_id=agent.id,
                        agent_name=agent.name,
                        user_id=user.id,
                    ),
                    AgentSecret(
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name=agent.name,
                        key="TOKEN",
                    ),
                    AgentSession(
                        session_id="session-delete-me",
                        agent_id=agent.id,
                        agent_name=agent.name,
                        skill_name="run",
                        signed_token="signed",
                        events_object_key="sessions/delete-me.jsonl",
                    ),
                    Bounty(
                        slug="delete-me-bounty",
                        title="Use delete-me",
                        description="test",
                        posted_by_id=user.id,
                        claimed_agent_id=agent.id,
                        claimed_by_id=user.id,
                    ),
                    DatabaseProvisionEvent(
                        agent_id=agent.id,
                        actor_user_id=user.id,
                        event_type="binding.created",
                    ),
                    MetaAgentRun(
                        run_id="meta-delete-me",
                        agent_id=agent.id,
                        user_id=user.id,
                        agent_name=agent.name,
                    ),
                ]
            )
            await session.commit()

            monkeypatch.setattr(agents, "_cleanup_agent_resources", lambda *a, **kw: [])

            await agents.remove_agent("delete-me", user=user, session=session)

            assert (await session.execute(select(Agent))).scalars().all() == []
            for model in (
                AgentApiToken,
                AgentAuthConnection,
                AgentCodeEditorOptIn,
                AgentConsumerSetupValue,
                AgentCustomDomain,
                AgentDeployment,
                AgentDeploymentEvent,
                AgentInstall,
                AgentMemoryEntry,
                AgentReviewRun,
                AgentSecret,
                DatabaseProvisionEvent,
                MetaAgentRun,
            ):
                assert (await session.execute(select(model))).scalars().all() == []
            assert (
                await session.execute(select(AgentProofRun.agent_id))
            ).scalar_one() is None
            assert (
                await session.execute(select(AgentReceipt.agent_id))
            ).scalar_one() is None
            assert (
                await session.execute(select(AgentSession.agent_id))
            ).scalar_one() is None
            assert (
                await session.execute(select(Bounty.claimed_agent_id))
            ).scalar_one() is None
    finally:
        await engine.dispose()


def _fork_test_agent(
    owner_id: int,
    name: str,
    *,
    source_agent_id: int | None = None,
) -> Agent:
    return Agent(
        owner_id=owner_id,
        name=name,
        description="",
        version="0.1.0",
        image=f"registry.example/{name}:latest",
        public=False,
        status="running",
        url=f"https://{name}.example.test",
        card={},
        source_agent_id=source_agent_id,
    )


@asynccontextmanager
async def _fork_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fork_owner: str = "owner@example.com",
) -> AsyncIterator[tuple[AsyncSession, User, Agent, Agent]]:
    """parent + one fork of it, plus stubs for the out-of-process cleanup."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            session.add(owner)
            if fork_owner != owner.email:
                session.add(User(email=fork_owner, password_hash="x"))
            await session.flush()
            fork_owner_id = (
                await session.execute(select(User.id).where(User.email == fork_owner))
            ).scalar_one()

            parent = _fork_test_agent(owner.id, "parent-agent")
            session.add(parent)
            await session.flush()
            fork = _fork_test_agent(
                fork_owner_id, "forked-agent", source_agent_id=parent.id
            )
            session.add(fork)
            await session.flush()
            session.add(
                AgentLineage(
                    parent_agent_id=parent.id,
                    child_agent_id=fork.id,
                    actor_user_id=fork_owner_id,
                    action="fork",
                )
            )
            await session.commit()

            cleaned: list[str] = []

            def _record_cleanup(agent_name: str, **_kw: object) -> list[str]:
                cleaned.append(agent_name)
                return []

            monkeypatch.setattr(agents, "_cleanup_agent_resources", _record_cleanup)
            yield session, owner, parent, fork
    finally:
        await engine.dispose()


@asynccontextmanager
async def _three_generation_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncSession, User, Agent, Agent, Agent]]:
    """parent -> fork -> grandfork, every one owned by the same caller."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            session.add(owner)
            await session.flush()
            parent = _fork_test_agent(owner.id, "parent-agent")
            session.add(parent)
            await session.flush()
            fork = _fork_test_agent(owner.id, "forked-agent", source_agent_id=parent.id)
            session.add(fork)
            await session.flush()
            grandfork = _fork_test_agent(
                owner.id, "grandforked-agent", source_agent_id=fork.id
            )
            session.add(grandfork)
            await session.commit()

            monkeypatch.setattr(agents, "_cleanup_agent_resources", lambda *a, **kw: [])
            yield session, owner, parent, fork, grandfork
    finally:
        await engine.dispose()


async def _agent_names(session: AsyncSession) -> list[str]:
    return sorted((await session.execute(select(Agent.name))).scalars().all())


async def _source_agent_id(session: AsyncSession, name: str) -> int | None:
    return (
        await session.execute(select(Agent.source_agent_id).where(Agent.name == name))
    ).scalar_one()


def _delete_audit_line(caplog: pytest.LogCaptureFixture) -> str:
    """The one log line that names what the delete deleted and what it detached."""
    lines = [
        record.getMessage()
        for record in caplog.records
        if "forks detached" in record.getMessage()
    ]
    assert len(lines) == 1, f"expected exactly one audit line, got {lines!r}"
    return lines[0]


@pytest.mark.asyncio
async def test_remove_agent_refuses_while_forks_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _fork_fixture(monkeypatch) as (session, owner, _parent, _fork):
        with pytest.raises(HTTPException) as excinfo:
            await agents.remove_agent("parent-agent", user=owner, session=session)

        assert excinfo.value.status_code == 409
        detail = excinfo.value.detail
        # The refusal is a plain string so every client renders it, and it
        # names exactly what a cascade would destroy.
        assert isinstance(detail, str)
        assert "forked-agent" in detail
        assert "?cascade=true" in detail
        # Nothing was destroyed, not even the agent the caller did name.
        assert await _agent_names(session) == ["forked-agent", "parent-agent"]


@pytest.mark.asyncio
async def test_remove_agent_cascade_opt_in_deletes_forks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _fork_fixture(monkeypatch) as (session, owner, _parent, _fork):
        await agents.remove_agent(
            "parent-agent", cascade=True, user=owner, session=session
        )

        assert await _agent_names(session) == []


@pytest.mark.asyncio
async def test_remove_agent_without_forks_needs_no_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _fork_fixture(monkeypatch) as (session, owner, _parent, fork):
        # Delete the fork first; the parent then has no dependents left.
        await agents.remove_agent("forked-agent", user=owner, session=session)
        assert await _agent_names(session) == ["parent-agent"]

        await agents.remove_agent("parent-agent", user=owner, session=session)
        assert await _agent_names(session) == []


@pytest.mark.asyncio
async def test_remove_agent_never_deletes_a_fork_owned_by_someone_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _fork_fixture(monkeypatch, fork_owner="other@example.com") as (
        session,
        owner,
        _parent,
        fork,
    ):
        # No 409: another user's fork is not ours to destroy, so it never
        # blocks. ``cascade=true`` must not reach it either.
        await agents.remove_agent(
            "parent-agent", cascade=True, user=owner, session=session
        )

        assert await _agent_names(session) == ["forked-agent"]
        survivor = (
            await session.execute(select(Agent).where(Agent.name == "forked-agent"))
        ).scalar_one()
        await session.refresh(survivor)
        # Detached from the deleted parent, not deleted.
        assert survivor.source_agent_id is None


@pytest.mark.asyncio
async def test_remove_agent_rejects_non_owner_even_with_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _fork_fixture(monkeypatch) as (session, _owner, _parent, _fork):
        intruder = User(email="intruder@example.com", password_hash="x")
        session.add(intruder)
        await session.commit()

        for target in ("parent-agent", "forked-agent"):
            with pytest.raises(HTTPException) as excinfo:
                await agents.remove_agent(
                    target, cascade=True, user=intruder, session=session
                )
            assert excinfo.value.status_code == 403

        assert await _agent_names(session) == ["forked-agent", "parent-agent"]


@pytest.mark.asyncio
async def test_remove_agent_cascade_is_a_real_query_param_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the wire, not just the handler.

    ``cascade`` must arrive as a parsed bool. A ``Query(False)`` default would
    make the in-process default truthy and silently skip the guard.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from control_plane.auth import current_user
    from control_plane.db import get_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as setup:
            owner = User(email="owner@example.com", password_hash="x")
            setup.add(owner)
            await setup.flush()
            parent = _fork_test_agent(owner.id, "parent-agent")
            setup.add(parent)
            await setup.flush()
            setup.add(
                _fork_test_agent(owner.id, "forked-agent", source_agent_id=parent.id)
            )
            await setup.commit()

        monkeypatch.setattr(agents, "_cleanup_agent_resources", lambda *a, **kw: [])

        app = FastAPI()
        app.include_router(agents.router)

        async def _override_session():
            async with Session() as session:
                yield session

        async def _override_current_user() -> User:
            return owner

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[current_user] = _override_current_user

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            blocked = await client.delete("/v1/agents/parent-agent")
            assert blocked.status_code == 409
            detail = blocked.json()["detail"]
            assert isinstance(detail, str)
            assert "forked-agent" in detail

            async with Session() as check:
                assert await _agent_names(check) == ["forked-agent", "parent-agent"]

            allowed = await client.delete("/v1/agents/parent-agent?cascade=true")
            assert allowed.status_code == 204

            async with Session() as check:
                assert await _agent_names(check) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_remove_agent_409_names_only_direct_forks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must list exactly the set a cascade would destroy.

    A fork of a fork is *not* destroyed by ``?cascade=true`` (the cascade is
    one level deep), so naming it in the 409 would make the message a lie.
    """
    async with _three_generation_fixture(monkeypatch) as (session, owner, *_rest):
        with pytest.raises(HTTPException) as excinfo:
            await agents.remove_agent("parent-agent", user=owner, session=session)

        detail = excinfo.value.detail
        assert excinfo.value.status_code == 409
        assert isinstance(detail, str)
        assert "forked-agent" in detail
        assert "grandforked-agent" not in detail


@pytest.mark.asyncio
async def test_remove_agent_cascade_detaches_grandchild_forks(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cascade is one level: a fork of a fork survives, detached.

    Nothing about that is inferable from a 204, so the names have to reach the
    server log or they reach nobody.
    """
    async with _three_generation_fixture(monkeypatch) as (session, owner, *_rest):
        with caplog.at_level(logging.INFO, logger="control_plane.routes.agents"):
            await agents.remove_agent(
                "parent-agent", cascade=True, user=owner, session=session
            )

        assert await _agent_names(session) == ["grandforked-agent"]
        # Alive, and no longer claiming a parent that no longer exists.
        assert await _source_agent_id(session, "grandforked-agent") is None

        line = _delete_audit_line(caplog)
        assert "forks deleted: forked-agent" in line
        assert "grandforked-agent" in line.split("forks detached")[1]


@pytest.mark.asyncio
async def test_remove_agent_logs_the_foreign_fork_it_detaches(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 204 has no body, so silence would make a detach unobservable."""
    async with _fork_fixture(monkeypatch, fork_owner="other@example.com") as (
        session,
        owner,
        _parent,
        _fork,
    ):
        with caplog.at_level(logging.INFO, logger="control_plane.routes.agents"):
            await agents.remove_agent("parent-agent", user=owner, session=session)

        assert await _agent_names(session) == ["forked-agent"]
        line = _delete_audit_line(caplog)
        assert "forks deleted: none" in line
        assert "forked-agent" in line.split("forks detached")[1]


@pytest.mark.asyncio
async def test_remove_agent_without_forks_logs_no_audit_line(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The line only appears when a fork was involved, so it stays meaningful."""
    async with _fork_fixture(monkeypatch) as (session, owner, _parent, _fork):
        with caplog.at_level(logging.INFO, logger="control_plane.routes.agents"):
            await agents.remove_agent("forked-agent", user=owner, session=session)

        assert await _agent_names(session) == ["parent-agent"]
        assert [
            record.getMessage()
            for record in caplog.records
            if "forks detached" in record.getMessage()
        ] == []


@pytest.mark.asyncio
async def test_remove_agent_mixed_owner_forks_blocks_on_mine_and_logs_theirs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One parent, one fork of mine, one fork of theirs.

    The 409 must name only mine, because only mine would be destroyed. Theirs
    is never in a response body at all, so the log is the only place it is
    accounted for.
    """
    async with _fork_fixture(monkeypatch) as (session, owner, parent, _mine):
        other = User(email="other@example.com", password_hash="x")
        session.add(other)
        await session.flush()
        session.add(
            _fork_test_agent(other.id, "their-fork", source_agent_id=parent.id)
        )
        await session.commit()

        with pytest.raises(HTTPException) as excinfo:
            await agents.remove_agent("parent-agent", user=owner, session=session)
        assert excinfo.value.status_code == 409
        assert "forked-agent" in str(excinfo.value.detail)
        assert "their-fork" not in str(excinfo.value.detail)

        with caplog.at_level(logging.INFO, logger="control_plane.routes.agents"):
            await agents.remove_agent(
                "parent-agent", cascade=True, user=owner, session=session
            )

        assert await _agent_names(session) == ["their-fork"]
        assert await _source_agent_id(session, "their-fork") is None
        line = _delete_audit_line(caplog)
        deleted, detached = line.split("forks detached")
        assert "forked-agent" in deleted and "their-fork" not in deleted
        assert "their-fork" in detached


@pytest.mark.asyncio
async def test_remove_agent_cascade_still_502s_when_a_fork_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Following the 409's ``?cascade=true`` hint can still land on a 502.

    Only the *foreign-owner* 502 was removed. A real Argo/k8s/Gitea failure
    while deleting a fork still rolls the whole request back, so no caller may
    assume cascade is 502-free.
    """
    async with _fork_fixture(monkeypatch) as (session, owner, _parent, _fork):

        def _fail_on_the_fork(agent_name: str, **_kw: object) -> list[str]:
            return ["argo app delete timed out"] if agent_name == "forked-agent" else []

        monkeypatch.setattr(agents, "_cleanup_agent_resources", _fail_on_the_fork)

        with caplog.at_level(logging.INFO, logger="control_plane.routes.agents"):
            with pytest.raises(HTTPException) as excinfo:
                await agents.remove_agent(
                    "parent-agent", cascade=True, user=owner, session=session
                )

        assert excinfo.value.status_code == 502
        assert "forked-agent" in str(excinfo.value.detail)
        # Rolled back whole: the named agent survives too, and nothing claims
        # a detach that did not happen.
        assert await _agent_names(session) == ["forked-agent", "parent-agent"]
        assert [
            record.getMessage()
            for record in caplog.records
            if "forks detached" in record.getMessage()
        ] == []
