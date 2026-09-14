from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import deployments
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentDatabaseBinding,
    AgentDeployment,
    AgentDeploymentEvent,
    DatabaseBranch,
    DatabaseProject,
    DatabaseRole,
    User,
)


def test_resolved_digest_matches_requested_tagged_image():
    expected = "registry.example.com/agents/example:abc123"
    resolved = f"{expected}@sha256:" + "f" * 64
    assert deployments._image_matches(expected, [resolved], resolved) is True


def missing_argo_verification() -> dict[str, Any]:
    return {
        "live": False,
        "url": "https://invoice-bot.example.com",
        "argo": {"ok": False, "exists": False, "sync": None, "health": None},
        "runtime": {"ok": False, "error": "deployment not found"},
        "health": {"ok": False, "error": "service not found"},
        "agent_card": {"ok": False, "error": "service not found"},
        "skills": {"ok": False, "count": 0, "names": []},
    }


@pytest.mark.asyncio
async def test_sync_deployment_verification_fails_missing_argo_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(missing_argo_verification()),
    )
    monkeypatch.setattr(deployments, "MISSING_ARGO_GRACE_SECONDS", 30)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=31),
        )

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        assert out.status == "failed"
        assert out.error == (
            "ArgoCD application is missing; deployment infrastructure was not "
            "created or was deleted."
        )
        assert out.completed_at is not None
        assert agent.status == "failed"

        events = (
            await session.execute(
                select(AgentDeploymentEvent).where(
                    AgentDeploymentEvent.deployment_id == deploy.id,
                )
            )
        ).scalars().all()
        latest_by_stage = {event.stage: event for event in events}
        assert latest_by_stage["argo"].status == "failed"
        assert latest_by_stage["verify"].status == "failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_marks_transient_agent_failed_for_existing_failed_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(_agent: Agent) -> dict[str, Any]:
        raise AssertionError("failed deployments should not be re-verified")

    monkeypatch.setattr(deployments, "verify_agent_deployment", fail_if_called)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="failed",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        agent.status = "building"
        await session.commit()

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        assert out.status == "failed"
        assert agent.status == "failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_latest_deployment_outs_batches_stable_deployment_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.routes import agents as agents_route

    async def fail_deployment_events(*_args: Any, **_kwargs: Any) -> list[AgentDeploymentEvent]:
        raise AssertionError("stable summaries should use batched deployment events")

    monkeypatch.setattr(agents_route, "deployment_events", fail_deployment_events)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, old_deploy = await _seed_deployment(
            session,
            status="live",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        latest_deploy = AgentDeployment(
            deploy_id="dpl_latest",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="deploy",
            status="live",
            source_repo_url=old_deploy.source_repo_url,
            head_sha="new-head",
            image=agent.image,
            agent_url=agent.url,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        session.add(latest_deploy)
        await session.flush()
        session.add(
            AgentDeploymentEvent(
                deployment_id=latest_deploy.id,
                deploy_id=latest_deploy.deploy_id,
                agent_name=agent.name,
                stage="openapi",
                status="succeeded",
                message="captured source spec",
                data={"openapi_url": "https://example.test/openapi.json"},
            )
        )
        await session.commit()

        rows = await agents_route._latest_deployment_outs([agent], session)

        out = rows[agent.name]
        assert out.deploy_id == "dpl_latest"
        assert out.head_sha == "new-head"
        assert [event.stage for event in out.events] == ["openapi"]
        assert out.events[0].data["openapi_url"] == "https://example.test/openapi.json"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_keeps_missing_argo_running_during_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(missing_argo_verification()),
    )
    monkeypatch.setattr(deployments, "MISSING_ARGO_GRACE_SECONDS", 30)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        assert out.status == "building"
        assert out.error is None
        assert out.completed_at is None
        assert agent.status == "building"

        events = (
            await session.execute(
                select(AgentDeploymentEvent).where(
                    AgentDeploymentEvent.deployment_id == deploy.id,
                )
            )
        ).scalars().all()
        latest_by_stage = {event.stage: event for event in events}
        assert latest_by_stage["argo"].status == "running"
        assert "verify" not in latest_by_stage

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_reindexes_changed_live_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_card = {
        "name": "invoice-bot",
        "version": "0.1.1",
        "skills": [{"name": "run_swarm", "tags": ["research"]}],
    }
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(
            {
                "live": True,
                "url": "https://invoice-bot.example.com",
                "argo": {"ok": True},
                "runtime": {"ok": True},
                "health": {"ok": True},
                "agent_card": {"ok": True, "body": live_card},
                "skills": {"ok": True, "count": 1, "names": ["run_swarm"]},
            }
        ),
    )
    warmed_cards: list[tuple[str, dict[str, Any]]] = []

    async def capture_warm_agent_card(name: str, card: dict[str, Any] | None) -> None:
        if isinstance(card, dict):
            warmed_cards.append((name, card))

    monkeypatch.setattr(deployments, "warm_agent_card", capture_warm_agent_card)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    indexed: list[dict[str, Any]] = []

    async def on_agent_changed(agent: Agent) -> None:
        indexed.append(
            {
                "name": agent.name,
                "status": agent.status,
                "version": agent.version,
                "card": agent.card,
            }
        )

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc),
        )

        out = await deployments.sync_deployment_verification(
            session,
            agent,
            deploy,
            on_agent_changed=on_agent_changed,
        )

        assert out.status == "live"
        assert out.agent_url == "https://invoice-bot.example.com"
        assert agent.status == "running"
        assert agent.url == "https://invoice-bot.example.com"
        assert agent.version == "0.1.1"
        assert agent.card == live_card
        assert indexed == [
            {
                "name": "invoice-bot",
                "status": "running",
                "version": "0.1.1",
                "card": live_card,
            }
        ]
        assert warmed_cards == [("invoice-bot", live_card)]

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_waits_for_managed_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(_live_verification()),
    )
    monkeypatch.setattr(deployments, "warm_agent_card", _async_noop)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc),
        )
        await _seed_database_binding(session, agent=agent, status="provisioning")

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        assert out.status == "verifying"
        assert out.completed_at is None
        assert out.verification["database"] == {
            "ok": False,
            "required": True,
            "total": 1,
            "ready": 0,
            "pending": 0,
            "provisioning": 1,
            "failed": 0,
        }
        events = (
            await session.execute(
                select(AgentDeploymentEvent).where(
                    AgentDeploymentEvent.deployment_id == deploy.id,
                    AgentDeploymentEvent.stage == "database",
                )
            )
        ).scalars().all()
        assert events[-1].status == "running"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_retries_recent_failed_managed_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(_live_verification()),
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc),
        )
        await _seed_database_binding(session, agent=agent, status="failed")

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        assert out.status == "verifying"
        assert out.error is None
        assert out.completed_at is None
        events = (
            await session.execute(
                select(AgentDeploymentEvent).where(
                    AgentDeploymentEvent.deployment_id == deploy.id,
                    AgentDeploymentEvent.stage == "database",
                )
            )
        ).scalars().all()
        assert events[-1].status == "running"
        assert events[-1].message == (
            "Managed database provisioning is retrying after a failed attempt."
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_fails_database_after_retry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(_live_verification()),
    )
    monkeypatch.setattr(deployments, "DATABASE_FAILURE_GRACE_SECONDS", 30)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=31),
        )
        await _seed_database_binding(session, agent=agent, status="failed")

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        assert out.status == "failed"
        assert out.error == (
            "Managed database provisioning did not recover within the retry window."
        )
        assert agent.status == "failed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_preserves_consumer_setup_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_card = {
        "name": "invoice-bot",
        "version": "0.1.1",
        "skills": [{"name": "run_swarm", "tags": ["research"]}],
    }
    consumer_setup = {
        "fields": [
            {
                "name": "INVOICE_API_KEY",
                "kind": "secret",
                "required": True,
            }
        ]
    }
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(
            {
                "live": True,
                "url": "https://invoice-bot.example.com",
                "argo": {"ok": True},
                "runtime": {"ok": True},
                "health": {"ok": True},
                "agent_card": {"ok": True, "body": live_card},
                "skills": {"ok": True, "count": 1, "names": ["run_swarm"]},
            }
        ),
    )
    warmed_cards: list[tuple[str, dict[str, Any]]] = []

    async def capture_warm_agent_card(name: str, card: dict[str, Any] | None) -> None:
        if isinstance(card, dict):
            warmed_cards.append((name, card))

    monkeypatch.setattr(deployments, "warm_agent_card", capture_warm_agent_card)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc),
        )
        agent.card = {
            "name": "invoice-bot",
            "version": "0.1.0",
            "skills": [{"name": "old"}],
            "consumer_setup": consumer_setup,
        }
        await session.commit()

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        expected_card = {**live_card, "consumer_setup": consumer_setup}
        assert out.status == "live"
        assert agent.status == "running"
        assert agent.card == expected_card
        assert warmed_cards == [("invoice-bot", expected_card)]

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_sets_canonical_url_when_live_payload_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_card = {
        "name": "invoice-bot",
        "version": "0.1.1",
        "skills": [{"name": "run_swarm"}],
    }
    monkeypatch.setattr(
        deployments,
        "verify_agent_deployment",
        _async_verification(
            {
                "live": True,
                "url": None,
                "argo": {"ok": True},
                "runtime": {"ok": True},
                "health": {"ok": True},
                "agent_card": {"ok": True, "body": live_card},
                "skills": {"ok": True, "count": 1, "names": ["run_swarm"]},
            }
        ),
    )
    monkeypatch.setattr(deployments, "warm_agent_card", _async_noop)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc),
        )
        agent.url = None
        deploy.agent_url = None
        await session.commit()

        out = await deployments.sync_deployment_verification(session, agent, deploy)

        assert out.status == "live"
        assert out.agent_url == "https://invoice-bot.example.com"
        assert agent.url == "https://invoice-bot.example.com"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_deployment_verification_passes_expected_revision_and_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_verify(
        _agent: Agent,
        *,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured["expected"] = expected
        return {
            "live": False,
            "url": "https://invoice-bot.example.com",
            "argo": {"ok": False, "exists": True},
            "runtime": {"ok": False},
            "health": {"ok": False},
            "agent_card": {"ok": False},
            "skills": {"ok": False, "count": 0, "names": []},
        }

    monkeypatch.setattr(deployments, "verify_agent_deployment", fake_verify)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc),
            head_sha="a" * 40,
        )
        session.add(
            AgentDeploymentEvent(
                deployment_id=deploy.id,
                deploy_id=deploy.deploy_id,
                agent_name=deploy.agent_name,
                stage="argo",
                status="running",
                message="waiting",
                data={
                    "expected_revision": "b" * 40,
                    "expected_image": "registry.example.com/agents/invoice-bot:" + "a" * 40,
                },
            )
        )
        await session.commit()

        await deployments.sync_deployment_verification(session, agent, deploy)

    assert captured["expected"] == {
        "runtime_revision": "b" * 40,
        "image": "registry.example.com/agents/invoice-bot:" + "a" * 40,
        "source_sha": "a" * 40,
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_expected_deployment_signals_uses_pinned_runtime_upgrade_image() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        expected_image = (
            "registry.example.com/agents/invoice-bot:"
            + "a" * 40
            + "-runtime-0.1.57-testnonce"
        )
        agent, deploy = await _seed_deployment(
            session,
            status="building",
            started_at=datetime.now(timezone.utc),
            head_sha="a" * 40,
            trigger="runtime_upgrade",
            image=expected_image,
        )

        expected = await deployments._expected_deployment_signals(
            session,
            agent,
            deploy,
        )

    assert expected["image"] == expected_image
    assert expected["source_sha"] == "a" * 40
    await engine.dispose()


@pytest.mark.asyncio
async def test_verify_agent_deployment_waits_for_expected_runtime_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(
        owner_id=1,
        name="invoice-bot",
        description="Invoice bot",
        version="0.1.0",
        image="registry.example.com/agents/invoice-bot:latest",
        public=True,
        status="building",
        url="https://invoice-bot.example.com",
        card={},
    )
    monkeypatch.setattr(
        deployments,
        "_safe_argo",
        lambda _name, expected_revision=None: {"ok": True, "revision": expected_revision},
    )
    monkeypatch.setattr(
        deployments,
        "_safe_runtime",
        lambda _name, expected_image=None: {
            "ok": False,
            "image_matches": False,
            "expected_image": expected_image,
            "images": ["registry.example.com/agents/invoice-bot:old"],
        },
    )

    async def fake_http(_name: str) -> dict[str, dict[str, Any]]:
        return {
            "health": {"ok": True},
            "card": {"ok": True, "body": {"skills": [{"name": "run"}]}},
        }

    monkeypatch.setattr(deployments, "_safe_agent_http", fake_http)

    result = await deployments.verify_agent_deployment(
        agent,
        expected={"image": "registry.example.com/agents/invoice-bot:" + "a" * 40},
    )

    assert result["live"] is False
    assert result["runtime"]["image_matches"] is False


@pytest.mark.asyncio
async def test_verify_agent_deployment_accepts_expected_image_when_argo_revision_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(
        owner_id=1,
        name="invoice-bot",
        description="Invoice bot",
        version="0.1.0",
        image="registry.example.com/agents/invoice-bot:latest",
        public=True,
        status="building",
        url="https://invoice-bot.example.com",
        card={},
    )
    monkeypatch.setattr(
        deployments,
        "_safe_argo",
        lambda _name, expected_revision=None: {
            "ok": False,
            "revision": "old",
            "expected_revision": expected_revision,
            "revision_matches": False,
        },
    )
    monkeypatch.setattr(
        deployments,
        "_safe_runtime",
        lambda _name, expected_image=None: {
            "ok": True,
            "image_matches": True,
            "expected_image": expected_image,
            "images": [expected_image],
        },
    )

    async def fake_http(_name: str) -> dict[str, dict[str, Any]]:
        return {
            "health": {"ok": True},
            "card": {"ok": True, "body": {"skills": [{"name": "run"}]}},
        }

    monkeypatch.setattr(deployments, "_safe_agent_http", fake_http)

    result = await deployments.verify_agent_deployment(
        agent,
        expected={
            "runtime_revision": "new",
            "image": "registry.example.com/agents/invoice-bot:" + "a" * 40,
        },
    )

    assert result["live"] is True
    assert result["argo"]["ok"] is True
    assert result["argo"]["equivalent_image_match"] is True


def _knative_agent() -> Agent:
    return Agent(
        owner_id=1,
        name="invoice-bot",
        description="Invoice bot",
        version="0.1.0",
        image="registry.example.com/agents/invoice-bot:latest",
        public=True,
        status="building",
        url="https://invoice-bot.example.com",
        card={},
    )


@pytest.mark.asyncio
async def test_verify_agent_deployment_defers_card_probe_until_runtime_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cold-start while the image is still building/reconciling."""
    monkeypatch.setattr(
        deployments, "_safe_argo", lambda _name, expected_revision=None: {"ok": True}
    )
    monkeypatch.setattr(
        deployments,
        "_safe_runtime",
        lambda _name, expected_image=None: {
            "ok": False,
            "image_matches": None,
            "kind": "knative_service",
            "ready": "Unknown",
        },
    )
    calls = {"http": 0}

    async def fake_http(_name: str) -> dict[str, dict[str, Any]]:
        calls["http"] += 1
        return {"health": {"ok": True}, "card": {"ok": True, "body": {"skills": [{"name": "run"}]}}}

    monkeypatch.setattr(deployments, "_safe_agent_http", fake_http)

    result = await deployments.verify_agent_deployment(_knative_agent())

    assert calls["http"] == 0  # the scaled-to-zero agent was never woken
    assert result["live"] is False
    assert result["agent_card"]["skipped"] is True


@pytest.mark.asyncio
async def test_verify_agent_deployment_probes_ready_idle_knative_service_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ready Knative Service with zero idle pods gets the single validation."""
    monkeypatch.setattr(
        deployments, "_safe_argo", lambda _name, expected_revision=None: {"ok": True}
    )
    monkeypatch.setattr(
        deployments,
        "_safe_runtime",
        lambda _name, expected_image=None: {
            "ok": True,
            "image_matches": True,
            "kind": "knative_service",
            "ready": "True",
        },
    )
    calls = {"http": 0}

    async def fake_http(_name: str) -> dict[str, dict[str, Any]]:
        calls["http"] += 1
        return {"health": {"ok": True}, "card": {"ok": True, "body": {"skills": [{"name": "run"}]}}}

    monkeypatch.setattr(deployments, "_safe_agent_http", fake_http)

    result = await deployments.verify_agent_deployment(_knative_agent())

    assert calls["http"] == 1
    assert result["live"] is True


@pytest.mark.asyncio
async def test_verify_agent_deployment_ignores_argo_out_of_sync_for_runtime_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Argo can stay OutOfSync for Knative apps after a healthy rollout."""
    monkeypatch.setattr(
        deployments,
        "_safe_argo",
        lambda _name, expected_revision=None: {
            "ok": False,
            "exists": True,
            "sync": "OutOfSync",
            "health": "Healthy",
            "revision": expected_revision,
            "expected_revision": expected_revision,
            "revision_matches": True,
        },
    )
    monkeypatch.setattr(
        deployments,
        "_safe_runtime",
        lambda _name, expected_image=None: {
            "ok": True,
            "image_matches": True,
            "kind": "knative_service",
            "ready": "True",
        },
    )
    calls = {"http": 0}

    async def fake_http(_name: str) -> dict[str, dict[str, Any]]:
        calls["http"] += 1
        return {
            "health": {"ok": True},
            "card": {"ok": True, "body": {"skills": [{"name": "run"}]}},
        }

    monkeypatch.setattr(deployments, "_safe_agent_http", fake_http)

    result = await deployments.verify_agent_deployment(
        _knative_agent(),
        expected={"runtime_revision": "a" * 40},
    )

    assert calls["http"] == 1
    assert result["live"] is True


def _async_verification(payload: dict[str, Any]):
    async def fake_verify(
        _agent: Agent,
        *,
        expected: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return payload

    return fake_verify


def _live_verification() -> dict[str, Any]:
    card = {
        "name": "invoice-bot",
        "version": "0.1.1",
        "skills": [{"name": "run_swarm"}],
    }
    return {
        "live": True,
        "url": "https://invoice-bot.example.com",
        "argo": {"ok": True},
        "runtime": {"ok": True},
        "health": {"ok": True},
        "agent_card": {"ok": True, "body": card},
        "skills": {"ok": True, "count": 1, "names": ["run_swarm"]},
    }


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _seed_database_binding(
    session,
    *,
    agent: Agent,
    status: str,
) -> AgentDatabaseBinding:
    project = DatabaseProject(
        owner_id=agent.owner_id,
        created_by_id=agent.owner_id,
        name="app",
        display_name="app",
        status=status,
    )
    session.add(project)
    await session.flush()
    branch = DatabaseBranch(project_id=project.id, name="main", status=status)
    session.add(branch)
    await session.flush()
    role = DatabaseRole(
        project_id=project.id,
        branch_id=branch.id,
        name="agent-app-rw",
        username="agent_app_rw",
        database_name="app",
        access_mode="read_write",
        status=status,
    )
    session.add(role)
    await session.flush()
    binding = AgentDatabaseBinding(
        agent_id=agent.id,
        user_id=agent.owner_id,
        database_project_id=project.id,
        database_branch_id=branch.id,
        database_role_id=role.id,
        agent_name=agent.name,
        binding_name="app",
        env_var="DATABASE_URL",
        access_mode="read_write",
        status=status,
    )
    session.add(binding)
    await session.commit()
    return binding


async def _seed_deployment(
    session,
    *,
    status: str,
    started_at: datetime,
    head_sha: str | None = None,
    trigger: str = "from_openapi",
    image: str | None = None,
) -> tuple[Agent, AgentDeployment]:
    user = User(email="owner@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name="invoice-bot",
        description="Invoice bot",
        version="0.1.0",
        image="registry.example.com/agents/invoice-bot:latest",
        public=True,
        status=status,
        url="https://invoice-bot.example.com",
        card={},
    )
    session.add(agent)
    await session.flush()
    deploy = AgentDeployment(
        deploy_id="dpl_test",
        agent_id=agent.id,
        user_id=user.id,
        agent_name=agent.name,
        trigger=trigger,
        status=status,
        source_repo_url="https://gitea.example.com/a2a-personal-2/invoice-bot",
        head_sha=head_sha,
        image=image or agent.image,
        agent_url=agent.url,
        started_at=started_at,
    )
    session.add(deploy)
    await session.commit()
    return agent, deploy
