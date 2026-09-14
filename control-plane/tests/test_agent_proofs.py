from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI, HTTPException, Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.agent_proofs import (
    proof_badge,
    sample_args_from_schema,
    summarize_result,
)
from control_plane.db import Base, get_session
from control_plane.models import Agent, AgentProofRun, User
from control_plane.auth import decode_token
from control_plane.config import settings as cp_settings
from control_plane.routes import agent_proofs as proof_routes


def _invoke_claims(token: str) -> dict[str, object]:
    return jwt.decode(
        token,
        cp_settings.jwt_secret,
        algorithms=[cp_settings.jwt_alg],
        options={"verify_aud": False},
    )


@pytest.fixture
async def proof_drop_client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        user = User(email="proof-drop-owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        public_agent = Agent(
            owner_id=user.id,
            name="public-proof-agent",
            description="Turns source material into a concise brief.",
            version="2.3.4",
            image="registry.example/public-proof-agent:2.3.4",
            public=True,
            status="running",
            url="https://public-proof-agent.example",
            card={
                "skills": [
                    {
                        "name": "brief",
                        "description": "Produce a concise public brief.",
                    }
                ]
            },
        )
        private_agent = Agent(
            owner_id=user.id,
            name="private-proof-agent",
            description="Private agent",
            version="1.0.0",
            image="registry.example/private-proof-agent:1.0.0",
            public=False,
            status="running",
            url="https://private-proof-agent.example",
            card={"skills": [{"name": "brief"}]},
        )
        other_public_agent = Agent(
            owner_id=user.id,
            name="other-public-agent",
            description="Another public agent",
            version="1.0.0",
            image="registry.example/other-public-agent:1.0.0",
            public=True,
            status="running",
            url="https://other-public-agent.example",
            card={"skills": [{"name": "brief"}]},
        )
        session.add_all([public_agent, private_agent, other_public_agent])
        await session.flush()

        created_at = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
        started_at = datetime(2026, 7, 11, 12, 0, 1, tzinfo=timezone.utc)
        completed_at = datetime(2026, 7, 11, 12, 0, 2, tzinfo=timezone.utc)
        passed = AgentProofRun(
            agent_id=public_agent.id,
            agent_name=public_agent.name,
            user_id=user.id,
            skill_name="brief",
            grant_id="GRANT_ID_DO_NOT_LEAK",
            status="passed",
            summary="SUMMARY_DO_NOT_LEAK /private/result.txt",
            args_json='{"prompt":"ARGS_DO_NOT_LEAK"}',
            result={"message": "RESULT_DO_NOT_LEAK"},
            events=[
                {"event": "EVENT_DO_NOT_LEAK"},
                {"event": "completed"},
            ],
            file_ops=[
                {"op": "create", "path": "/private/FILE_PATH_DO_NOT_LEAK.txt"}
            ],
            card_hash="card-hash-123",
            repo_url="https://REPO_CREDENTIAL_DO_NOT_LEAK@example.com/repo.git",
            head_sha="deadbeef",
            image="registry.example/public-proof-agent@sha256:abc",
            agent_url="https://public-proof-agent.example",
            elapsed_ms=321,
            created_at=created_at,
            started_at=started_at,
            completed_at=completed_at,
        )
        failed = AgentProofRun(
            agent_id=public_agent.id,
            agent_name=public_agent.name,
            user_id=user.id,
            skill_name="brief",
            status="failed",
            summary="failed",
        )
        running = AgentProofRun(
            agent_id=public_agent.id,
            agent_name=public_agent.name,
            user_id=user.id,
            skill_name="brief",
            status="running",
        )
        private_passed = AgentProofRun(
            agent_id=private_agent.id,
            agent_name=private_agent.name,
            user_id=user.id,
            skill_name="brief",
            status="passed",
        )
        session.add_all([passed, failed, running, private_passed])
        await session.commit()
        proof_ids = {
            "passed": passed.id,
            "failed": failed.id,
            "running": running.id,
            "private": private_passed.id,
        }

    app = FastAPI()
    app.include_router(proof_routes.public_agent_router)

    async def override_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client, proof_ids
    finally:
        await engine.dispose()


def test_sample_args_from_schema_prefers_defaults_and_required_fields():
    schema = {
        "type": "object",
        "required": ["prompt", "count", "enabled", "mode"],
        "properties": {
            "prompt": {"type": "string", "default": "draw a chart"},
            "count": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "mode": {"type": "string", "enum": ["fast", "deep"]},
            "optional": {"type": "string"},
        },
    }

    assert sample_args_from_schema(schema) == {
        "prompt": "draw a chart",
        "count": 1,
        "enabled": True,
        "mode": "fast",
    }


def test_proof_badge_tracks_public_status_language():
    assert proof_badge("passed") == "verified"
    assert proof_badge("failed") == "degraded"
    assert proof_badge(None) == "unverified"


def test_public_proof_summary_exposes_run_counts():
    out = proof_routes._summary_out(
        "website-scraper",
        latest=None,
        total_runs=4,
        passed_runs=3,
        failed_runs=1,
    )

    assert out.agent_name == "website-scraper"
    assert out.badge == "unverified"
    assert out.total_runs == 4
    assert out.passed_runs == 3
    assert out.failed_runs == 1


async def test_list_my_agent_proofs_compact_omits_heavy_payloads() -> None:
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
                name="proofy",
                description="",
                version="1.0.0",
                image="example/proofy",
                public=True,
                status="running",
                url="https://proofy.example",
                card={"skills": [{"name": "run"}]},
            )
            session.add(agent)
            await session.flush()
            session.add(
                AgentProofRun(
                    agent_id=agent.id,
                    agent_name=agent.name,
                    user_id=user.id,
                    skill_name="run",
                    grant_id="grant-1",
                    status="passed",
                    summary="ok",
                    args_json='{"prompt":"hello"}',
                    result={"large": "x" * 1024},
                    events=[{"event": "one"}, {"event": "two"}],
                    file_ops=[{"op": "create", "path": "out.txt"}],
                )
            )
            await session.commit()

            rows = await proof_routes.list_my_agent_proofs(
                user=user,
                session=session,
                agent=None,
                limit=10,
                compact=True,
            )

            assert len(rows) == 1
            assert rows[0].args_preview == {"prompt": "hello"}
            assert rows[0].result == {}
            assert rows[0].events == []
            assert rows[0].file_ops == []
            assert rows[0].events_count == 2
            assert rows[0].file_ops_count == 1

            summaries = await proof_routes.list_public_agent_proofs(
                Response(),
                session=session,
                user=None,
                limit=10,
                compact=True,
            )

            assert len(summaries) == 1
            assert summaries[0].latest is not None
            # Anonymous: the allowlisted projection, which has no payload
            # fields at all to be empty.
            assert isinstance(
                summaries[0].latest, proof_routes.PublicAgentProofOut
            )
            assert summaries[0].latest.events_count == 2
            assert summaries[0].latest.file_ops_count == 1
    finally:
        await engine.dispose()


async def test_public_proof_drop_returns_only_allowlisted_metadata(
    proof_drop_client,
) -> None:
    client, proof_ids = proof_drop_client
    response = await client.get(
        f"/v1/public/agents/public-proof-agent/proofs/{proof_ids['passed']}"
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "proof_id",
        "agent_name",
        "agent_description",
        "agent_version",
        "skill_name",
        "skill_description",
        "status",
        "badge",
        "summary",
        "events_count",
        "file_ops_count",
        "card_hash",
        "head_sha",
        "image",
        "agent_url",
        "elapsed_ms",
        "created_at",
        "started_at",
        "completed_at",
    }
    assert payload == {
        "proof_id": proof_ids["passed"],
        "agent_name": "public-proof-agent",
        "agent_description": "Turns source material into a concise brief.",
        "agent_version": "2.3.4",
        "skill_name": "brief",
        "skill_description": "Produce a concise public brief.",
        "status": "passed",
        "badge": "verified",
        # Derived from the run's own counts by ``public_proof_summary``. The
        # stored ``AgentProofRun.summary`` ("SUMMARY_DO_NOT_LEAK
        # /private/result.txt") is a result excerpt plus a workspace path and
        # is never published; the secret sweep below pins that.
        "summary": "completed successfully with 1 recorded file operation",
        "events_count": 2,
        "file_ops_count": 1,
        "card_hash": "card-hash-123",
        "head_sha": "deadbeef",
        "image": "registry.example/public-proof-agent@sha256:abc",
        "agent_url": "https://public-proof-agent.example",
        "elapsed_ms": 321,
        "created_at": "2026-07-11T12:00:00",
        "started_at": "2026-07-11T12:00:01",
        "completed_at": "2026-07-11T12:00:02",
    }

    forbidden_keys = {
        "args",
        "args_json",
        "args_preview",
        "result",
        "events",
        "file_ops",
        "grant",
        "grant_id",
        "repo_url",
        "error",
        "user_id",
    }
    assert forbidden_keys.isdisjoint(payload)
    serialized = response.text
    for secret in (
        "SUMMARY_DO_NOT_LEAK",
        "ARGS_DO_NOT_LEAK",
        "RESULT_DO_NOT_LEAK",
        "EVENT_DO_NOT_LEAK",
        "FILE_PATH_DO_NOT_LEAK",
        "GRANT_ID_DO_NOT_LEAK",
        "REPO_CREDENTIAL_DO_NOT_LEAK",
    ):
        assert secret not in serialized


async def test_public_proof_drop_returns_404_without_revealing_existence(
    proof_drop_client,
) -> None:
    client, proof_ids = proof_drop_client
    hidden_urls = (
        f"/v1/public/agents/private-proof-agent/proofs/{proof_ids['private']}",
        f"/v1/public/agents/public-proof-agent/proofs/{proof_ids['failed']}",
        f"/v1/public/agents/public-proof-agent/proofs/{proof_ids['running']}",
        f"/v1/public/agents/other-public-agent/proofs/{proof_ids['passed']}",
        "/v1/public/agents/public-proof-agent/proofs/999999",
    )

    for url in hidden_urls:
        response = await client.get(url)
        assert response.status_code == 404
        assert response.json() == {"detail": "proof not found"}


def test_summarize_result_uses_file_ops_when_result_is_empty():
    assert summarize_result({}, [{"op": "create", "path": "out.txt"}]) == (
        "ok, 1 file change"
    )


def test_summarize_result_ignores_null_error_for_successful_artifact_run():
    assert summarize_result(
        {"ok": True, "error": None, "video_path": "/tmp/video.mp4"},
        [
            {"op": "create", "path": "video.mp4"},
            {"op": "create", "path": "storyboard.json"},
            {"op": "create", "path": "render-manifest.json"},
            {"op": "create", "path": "video.srt"},
        ],
    ) == "ok, 4 file changes"


async def test_invoke_agent_posts_proof_run_with_httpx(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {"result": {"ok": True}, "events": []}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
            captured["url"] = url
            captured["body"] = json
            return FakeResponse()

    async def fake_get_creds_for_user(
        user_id: int,
        session: object,
    ) -> dict[str, object]:
        captured["creds_user_id"] = user_id
        captured["creds_session"] = session
        return {"provider": "test"}

    monkeypatch.setattr(proof_routes.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(proof_routes, "get_creds_for_user", fake_get_creds_for_user)
    session = object()

    result = await proof_routes._invoke_agent(
        agent=SimpleNamespace(name="website-scraper"),
        skill_name="scrape_website",
        args={"url": "https://example.com"},
        grant="grant-token",
        session=session,
        user=SimpleNamespace(id=7),
    )

    assert result == {"result": {"ok": True}, "events": []}
    assert captured["url"] == (
        "http://website-scraper.agents.svc.cluster.local/invoke/scrape_website"
    )
    assert captured["timeout"] == float(
        proof_routes.settings.agents_default_timeout_seconds
    )
    assert captured["creds_user_id"] == 7
    assert captured["creds_session"] is session
    body = dict(captured["body"])  # type: ignore[arg-type]
    # The callback credential is minted here for this one callee — never the
    # caller's own session, which used to be forwarded verbatim.
    forwarded = body.pop("cp_jwt")
    assert body == {
        "arguments": {"url": "https://example.com"},
        "grant": "grant-token",
        "llm_creds": {"provider": "test"},
        "cp_url": proof_routes.settings.public_cp_url,
    }
    with pytest.raises(HTTPException):
        decode_token(forwarded)
    assert _invoke_claims(forwarded)["aud"] == "agent:website-scraper"


async def test_public_proof_feed_never_leaves_the_public_agent_set() -> None:
    """The feed only ever speaks for agents flagged public.

    ``/v1/public/agent-proofs`` no longer hands out ``AgentProofRun.events``
    (see ``test_public_proof_index_is_allowlisted_for_anonymous_callers`` in
    tests/test_agent_receipts_authz.py) — an event stream carries the sealed
    replay token and through it the run's complete arguments. This pins the
    other half of the boundary, which held before that change and still holds:
    a private agent's run never appears here at all, whatever the projection.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="proof-boundary@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            agents = {}
            for name, public in (("open-agent", True), ("closed-agent", False)):
                agent = Agent(
                    owner_id=user.id,
                    name=name,
                    description="",
                    version="1.0.0",
                    image=f"example/{name}",
                    public=public,
                    status="running",
                    url=f"https://{name}.example",
                    card={"skills": [{"name": "run"}]},
                )
                session.add(agent)
                await session.flush()
                agents[name] = agent
                session.add(
                    AgentProofRun(
                        agent_id=agent.id,
                        agent_name=name,
                        user_id=user.id,
                        skill_name="run",
                        status="passed",
                        summary="ok",
                        args_json='{"prompt":"hello"}',
                        result={"ok": True},
                        events=[
                            {
                                "kind": "replay_sealed",
                                "payload": {"token": f"sealed-{name}"},
                            }
                        ],
                        file_ops=[],
                    )
                )
            await session.commit()

            for compact in (False, True):
                summaries = await proof_routes.list_public_agent_proofs(
                    Response(), session=session, user=None, limit=50, compact=compact
                )
                names = {summary.agent_name for summary in summaries}
                assert names == {"open-agent"}, (compact, names)
                serialized = repr([s.model_dump() for s in summaries])
                assert "sealed-closed-agent" not in serialized

            # And flipping the agent private removes it from the feed.
            agents["open-agent"].public = False
            session.add(agents["open-agent"])
            await session.commit()
            assert (
                await proof_routes.list_public_agent_proofs(
                    Response(), session=session, user=None, limit=50, compact=False
                )
                == []
            )
    finally:
        await engine.dispose()
