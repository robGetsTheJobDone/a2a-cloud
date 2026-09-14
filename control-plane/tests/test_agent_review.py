"""Tests for the deploy-time advisory reviewer wiring."""
from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)
os.environ.setdefault("A2A_CP_JWT_SECRET", "test-secret")

import asyncio
import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import agent_review
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentDeployment,
    AgentDeploymentEvent,
    AgentReviewRun,
    User,
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _make_user_and_agent(factory) -> tuple[User, Agent, AgentDeployment]:
    async with factory() as session:
        user = User(email="dev@example.com", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        agent = Agent(
            owner_id=user.id,
            name="hello",
            description="",
            version="0.1.0",
            image="img",
            card={},
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        deploy = AgentDeployment(
            deploy_id="dep-1",
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            status="building",
            head_sha="abc",
        )
        session.add(deploy)
        await session.commit()
        await session.refresh(deploy)
        return user, agent, deploy


# ---- _classify_status ----

def test_classify_status_clean_report() -> None:
    status, c, w, i = agent_review._classify_status(
        {"ok": True, "findings": []}
    )
    assert (status, c, w, i) == ("passed", 0, 0, 0)


def test_classify_status_warning_only() -> None:
    status, c, w, i = agent_review._classify_status(
        {
            "findings": [
                {"severity": "warning", "category": "ergonomics", "message": "x"},
                {"severity": "info", "category": "ergonomics", "message": "y"},
            ]
        }
    )
    assert (status, c, w, i) == ("warning", 0, 1, 1)


def test_classify_status_critical_present() -> None:
    status, c, w, i = agent_review._classify_status(
        {
            "findings": [
                {"severity": "critical", "category": "security", "message": "key"},
                {"severity": "warning", "category": "ergonomics", "message": "z"},
            ]
        }
    )
    assert (status, c, w, i) == ("failed", 1, 1, 0)


def test_classify_status_errored_when_no_findings_and_error_key() -> None:
    status, *_ = agent_review._classify_status({"error": "boom"})
    assert status == "errored"


# ---- run_deploy_review happy path ----

@pytest.mark.asyncio
async def test_run_deploy_review_persists_findings(
    session_factory, monkeypatch
) -> None:
    user, agent, deploy = await _make_user_and_agent(session_factory)

    async def fake_call_reviewer(**_kwargs):
        return {
            "ok": False,
            "agent_name": "hello",
            "ref": "abc",
            "summary": "one critical",
            "findings": [
                {
                    "severity": "critical",
                    "category": "security",
                    "message": "hardcoded key",
                    "file": "agent.py",
                    "line": 12,
                }
            ],
        }

    monkeypatch.setattr(agent_review, "_call_reviewer", fake_call_reviewer)
    monkeypatch.setattr(agent_review, "SessionLocal", session_factory)

    await agent_review.run_deploy_review(
        review_id="rev-1",
        agent_id=agent.id,
        agent_name=agent.name,
        ref="abc",
        owner=None,
        user_id=user.id,
        user_jwt="dummy",
        cp_url="http://cp",
        deploy_id=deploy.deploy_id,
    )

    async with session_factory() as session:
        row = (
            await session.execute(
                select(AgentReviewRun).where(AgentReviewRun.review_id == "rev-1")
            )
        ).scalar_one()
        events = (
            await session.execute(
                select(AgentDeploymentEvent).where(
                    AgentDeploymentEvent.deploy_id == deploy.deploy_id,
                    AgentDeploymentEvent.stage == "review",
                )
            )
        ).scalars().all()
    assert row.status == "failed"
    assert row.critical_count == 1
    assert row.findings[0]["file"] == "agent.py"
    assert row.completed_at is not None
    assert row.elapsed_ms is not None
    stages = [(e.stage, e.status) for e in events]
    assert ("review", "running") in stages
    assert ("review", "failed") in stages


@pytest.mark.asyncio
async def test_run_deploy_review_clean_report_emits_passed(
    session_factory, monkeypatch
) -> None:
    user, agent, deploy = await _make_user_and_agent(session_factory)

    async def fake_call_reviewer(**_kwargs):
        return {
            "ok": True,
            "agent_name": "hello",
            "ref": "abc",
            "summary": "clean",
            "findings": [],
        }

    monkeypatch.setattr(agent_review, "_call_reviewer", fake_call_reviewer)
    monkeypatch.setattr(agent_review, "SessionLocal", session_factory)

    await agent_review.run_deploy_review(
        review_id="rev-2",
        agent_id=agent.id,
        agent_name=agent.name,
        ref="abc",
        owner=None,
        user_id=user.id,
        user_jwt="dummy",
        cp_url="http://cp",
        deploy_id=deploy.deploy_id,
    )

    async with session_factory() as session:
        row = (
            await session.execute(
                select(AgentReviewRun).where(AgentReviewRun.review_id == "rev-2")
            )
        ).scalar_one()
        events = (
            await session.execute(
                select(AgentDeploymentEvent).where(
                    AgentDeploymentEvent.deploy_id == deploy.deploy_id,
                    AgentDeploymentEvent.stage == "review",
                    AgentDeploymentEvent.status == "passed",
                )
            )
        ).scalars().all()
    assert row.status == "passed"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_run_deploy_review_errored_path(session_factory, monkeypatch) -> None:
    user, agent, deploy = await _make_user_and_agent(session_factory)

    async def fake_call_reviewer(**_kwargs):
        raise RuntimeError("reviewer 503: down")

    monkeypatch.setattr(agent_review, "_call_reviewer", fake_call_reviewer)
    monkeypatch.setattr(agent_review, "SessionLocal", session_factory)

    await agent_review.run_deploy_review(
        review_id="rev-3",
        agent_id=agent.id,
        agent_name=agent.name,
        ref="abc",
        owner=None,
        user_id=user.id,
        user_jwt="dummy",
        cp_url="http://cp",
        deploy_id=deploy.deploy_id,
    )

    async with session_factory() as session:
        row = (
            await session.execute(
                select(AgentReviewRun).where(AgentReviewRun.review_id == "rev-3")
            )
        ).scalar_one()
    assert row.status == "errored"
    assert row.error and "503" in row.error


@pytest.mark.asyncio
async def test_run_deploy_review_skipped_when_disabled_in_db(
    session_factory, monkeypatch
) -> None:
    """DB-backed killswitch: setting reviewer_enabled=False short-circuits."""
    from control_plane.platform_settings import set_setting

    user, agent, deploy = await _make_user_and_agent(session_factory)
    async with session_factory() as session:
        await set_setting(session, "reviewer_enabled", False, actor="test")

    monkeypatch.setattr(agent_review, "SessionLocal", session_factory)

    called = {"hit": False}

    async def fake_call_reviewer(**_kwargs):
        called["hit"] = True
        return {}

    monkeypatch.setattr(agent_review, "_call_reviewer", fake_call_reviewer)

    await agent_review.run_deploy_review(
        review_id="rev-4",
        agent_id=agent.id,
        agent_name=agent.name,
        ref="abc",
        owner=None,
        user_id=user.id,
        user_jwt="dummy",
        cp_url="http://cp",
        deploy_id=deploy.deploy_id,
    )
    assert called["hit"] is False
    async with session_factory() as session:
        row = (
            await session.execute(
                select(AgentReviewRun).where(AgentReviewRun.review_id == "rev-4")
            )
        ).scalar_one()
    assert row.status == "skipped"


# ---- enqueue_deploy_review wiring ----

@pytest.mark.asyncio
async def test_enqueue_deploy_review_creates_task(monkeypatch) -> None:
    async def noop_run(**_kwargs):
        return None

    monkeypatch.setattr(agent_review, "run_deploy_review", noop_run)
    fake_app = type("App", (), {"state": type("S", (), {})()})()
    review_id = agent_review.enqueue_deploy_review(
        fake_app,
        agent_id=1,
        agent_name="hello",
        ref="abc",
        owner=None,
        user_id=1,
    )
    assert review_id and len(review_id) == 32
    tasks = fake_app.state.agent_review_tasks
    assert isinstance(tasks, set) and len(tasks) == 1
    await asyncio.gather(*tasks, return_exceptions=True)


# ---- _call_reviewer SSE parsing ----

class _SSETransport(httpx.AsyncBaseTransport):
    def __init__(self, body: str, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code
        self.requests: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            self.status_code,
            content=self.body,
            headers={"content-type": "text/event-stream"},
        )


@pytest.mark.asyncio
async def test_call_reviewer_parses_result_frame(monkeypatch) -> None:
    body = (
        "data: {\"type\": \"event\", \"kind\": \"progress\", "
        "\"payload\": {\"message\": \"hi\"}}\n\n"
        "data: {\"type\": \"result\", \"result\": "
        "{\"ok\": true, \"agent_name\": \"hello\", \"ref\": \"abc\", "
        "\"summary\": \"clean\", \"findings\": []}}\n\n"
        "data: [DONE]\n\n"
    )
    transport = _SSETransport(body)
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent_review.httpx, "AsyncClient", _client)
    out = await agent_review._call_reviewer(
        agent_name="hello",
        ref="abc",
        owner=None,
        cp_jwt="x",
        cp_url="http://cp",
        llm_creds={"model": "platform-model"},
    )
    assert out["ok"] is True
    assert out["summary"] == "clean"
    assert transport.requests[0]["llm_creds"] == {"model": "platform-model"}


@pytest.mark.asyncio
async def test_call_reviewer_raises_on_http_error(monkeypatch) -> None:
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = _SSETransport("nope", status_code=503)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent_review.httpx, "AsyncClient", _client)
    with pytest.raises(RuntimeError) as exc:
        await agent_review._call_reviewer(
            agent_name="hello",
            ref="abc",
            owner=None,
            cp_jwt="x",
            cp_url="http://cp",
        )
    assert "503" in str(exc.value)


@pytest.mark.asyncio
async def test_call_reviewer_raises_when_no_result(monkeypatch) -> None:
    body = "data: {\"type\": \"event\", \"kind\": \"progress\"}\n\ndata: [DONE]\n\n"
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = _SSETransport(body)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(agent_review.httpx, "AsyncClient", _client)
    with pytest.raises(RuntimeError) as exc:
        await agent_review._call_reviewer(
            agent_name="hello",
            ref="abc",
            owner=None,
            cp_jwt="x",
            cp_url="http://cp",
        )
    assert "without a result" in str(exc.value)
